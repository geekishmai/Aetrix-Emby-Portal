# -*- coding: utf-8 -*-
"""转存暂存 TTL 清扫护栏（完美审计 PA-075）。

缺陷：_acquire_source_stage 每次新建暂存先跑 _sweep_stages，
旧实现只看 st-*.bin 的 mtime 是否超 TTL——长上传在下载完成
后整份读盘上传可持续数小时、期间不再写盘，mtime 冻结在下
载完成时刻，TTL 一到就被别的单新建暂存触发的清扫误删（字
节靠回落源重读兜底不错位，但源侧断点续传与在途合并全作
废、服主源被白重读一遍）。

本文件钉死两层护栏：
1. 本进程注册表（_STAGE_REGISTRY）持有中的暂存，TTL 清扫
   绝不删——执行器在整个任务尝试期间经注册表持有 stage，
   注册表在即是本进程口径的活引用；
2. _SourceStage.read 读盘刷新 .bin 的 mtime：活读中的暂存
   永远不会「看起来过期」，兜住注册表之外的口径。

同时钉死清扫本身没有被废掉：注册表外、mtime 超 TTL 的残
留照删；TTL 内的新文件不动。权威回收仍是按任务引用判定
的 sweep_stage_orphans（见 test_transfer_stage_disk_guard）。

全部离线：tmp_path 暂存目录，不碰 DB、不碰生产数据。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import json
import time

from backend.emby_server import transfer_variant as tv


def _age(path, age_sec):
    t = time.time() - age_sec
    os.utime(path, (t, t))


def _stage_pair(stage_dir, key, size, nbytes=300, age_sec=7200):
    """在暂存目录落一对 st-<key>.bin/.json 并回拨 mtime。"""
    stage_dir.mkdir(parents=True, exist_ok=True)
    bin_path = stage_dir / f"st-{key}.bin"
    meta_path = stage_dir / f"st-{key}.json"
    bin_path.write_bytes(b"x" * nbytes)
    meta_path.write_text(json.dumps(
        {"key": key, "size": size, "prefix": nbytes}), encoding="utf-8")
    _age(str(bin_path), age_sec)
    _age(str(meta_path), age_sec)
    return bin_path, meta_path


def _hold_in_registry(stage_dir, key, size):
    """把一个 stage 放进本进程注册表，模拟执行器正在持有它
    做长上传（整个任务尝试期间 stage 对象经注册表存活）。"""
    stage = tv._SourceStage(str(stage_dir), key, size)
    with tv._STAGE_REGISTRY_LOCK:
        tv._STAGE_REGISTRY[key] = stage
    return stage


# ---------- ① 注册表护栏：活单长上传暂存不被 TTL 清扫误删 ----------

def test_ttl_sweep_keeps_stage_held_in_registry(tmp_path):
    """注册表持有中的暂存对：mtime 远超 TTL 也不删。

    复现 PA-075：暂存下载完成后 mtime 冻结，整份读盘上传数
    小时；旧 _sweep_stages 只看 mtime → 本用例在旧码下文件
    被删（红），护栏后存活。"""
    stage_dir = tmp_path / "stage"
    key = tv._stage_key("pan115", "", "livefid", 2000)
    bin_path, meta_path = _stage_pair(stage_dir, key, 2000,
                                      age_sec=7200)
    _hold_in_registry(stage_dir, key, 2000)

    tv._sweep_stages(str(stage_dir), 3600)

    assert bin_path.exists() and meta_path.exists()


def test_acquire_triggered_sweep_keeps_live_stage(tmp_path, monkeypatch):
    """走真实触发路径：另一个内容新建暂存（_acquire_source_
    stage 内部先清扫）不得删掉注册表持有中的长上传暂存。"""
    stage_dir = tmp_path / "stage"
    monkeypatch.setenv("TRANSFER_STAGE_DIR", str(stage_dir))
    monkeypatch.setenv("TRANSFER_STAGE_TTL_SEC", "3600")
    key = tv._stage_key("pan115", "", "livefid", 2000)
    bin_path, meta_path = _stage_pair(stage_dir, key, 2000,
                                      age_sec=7200)
    _hold_in_registry(stage_dir, key, 2000)

    # 盘余量/总量帽可能让本次 acquire 回 None（暂存 best-
    # effort 语义），但内部清扫必定先跑过——断言只看活单
    # 文件在清扫后仍在。
    tv._acquire_source_stage("pan115", "", "otherfid", 1000)

    assert bin_path.exists() and meta_path.exists()


# ---------- ② 读盘刷新：活读中的暂存 mtime 不冻结 ----------

def test_stage_read_refreshes_bin_mtime(tmp_path):
    """stage.read 读盘后 .bin 的 mtime 刷新为近现在时刻，
    随后 TTL 清扫（注册表外口径）也不会把它当过期残留删。

    旧码 read 不刷新 mtime：mtime 仍停在回拨时刻，清扫删
    文件（红）。"""
    stage_dir = tmp_path / "stage"
    key = tv._stage_key("pan115", "", "readfid", 2000)
    bin_path, meta_path = _stage_pair(stage_dir, key, 2000,
                                      nbytes=300, age_sec=7200)
    # 直接构造（不进注册表），隔离验证读盘刷新这一层。
    stage = tv._SourceStage(str(stage_dir), key, 2000)
    assert stage.read(0, 299) == b"x" * 300
    assert time.time() - os.path.getmtime(str(bin_path)) < 60

    tv._sweep_stages(str(stage_dir), 3600)
    assert bin_path.exists() and meta_path.exists()


# ---------- ③ 清扫本身没被废：残留照删、新文件不动 ----------

def test_ttl_sweep_removes_unheld_expired_stage(tmp_path):
    """注册表外、mtime 超 TTL 的无主残留：照删（防把 TTL
    清扫整体废掉来凑绿）。"""
    stage_dir = tmp_path / "stage"
    key = tv._stage_key("pan115", "", "ghostfid", 2000)
    bin_path, meta_path = _stage_pair(stage_dir, key, 2000,
                                      age_sec=7200)

    tv._sweep_stages(str(stage_dir), 3600)

    assert not bin_path.exists() and not meta_path.exists()


def test_ttl_sweep_keeps_unheld_fresh_stage(tmp_path):
    """注册表外但 mtime 在 TTL 内的新暂存：不动。"""
    stage_dir = tmp_path / "stage"
    key = tv._stage_key("pan115", "", "youngfid", 2000)
    bin_path, meta_path = _stage_pair(stage_dir, key, 2000,
                                      age_sec=60)

    tv._sweep_stages(str(stage_dir), 3600)

    assert bin_path.exists() and meta_path.exists()
