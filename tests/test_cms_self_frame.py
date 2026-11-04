"""CMS 片头印制广告「自帧处理」产品化回归（2026-11-04）。

锁住的口径（试点 self-frame-pilot-20261104 帧图亲验定版）：

- 检测：行段合并几何（间隙 ≤6 行）＋逐秒 2D 梯度匹配定带尾；
  摸底 hint 只作先验、全区扫描，结论冲突记 conflict。
- 三门控 fail-closed：跨场景持续性（单卡片提示文字不许当带）、
  hint 先验（标注错位不许只在错区找）、字幕行隔离（条带吃进
  底部字幕区时拆不开就整条放弃）。
- 修后复检口径与带尾外扩 2 秒（不越口子）；熔断试点收紧版
  （内存 1.2GB / load1 6）。
- 记账 method=self_fill/self_blur 与 graft 并列共用修复表；
  下发仍只认 verified（cms_graft.lookup_verified 不分 method）。
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid

import pytest
from PIL import Image, ImageDraw

from backend.database import SessionLocal, init_db
from backend.emby_server import cms_graft as graft
from backend.emby_server import cms_self_frame as sf
from backend.emby_server import models as em

init_db()

W, H = 480, 270          # det 坐标系（DET_W=480，16:9）
FPS2 = 2                 # 检测帧率 2fps


def _guid():
    return uuid.uuid4().hex


def _frame(scene, strip=None, text_seed=0, subtitle=None, frame_idx=0):
    """合成一帧灰度图：背景是逐帧流动的纹理（只有烧录字静止——
    这正是检测器利用的性质：中位帧里背景被抹平、静态字最清楚）；
    scene 决定背景基调；strip=(y0,y1) 时在条带内画静态字形块
    （同一 text_seed = 同字形）；subtitle=(y0,y1,seed) 画逐秒
    换位的字幕块。"""
    img = Image.new("L", (W, H), 0)
    px = img.load()
    for y in range(H):
        base = scene * 61 + 30
        for x in range(W):
            px[x, y] = (base + (x * 3 + y * 5 + frame_idx * 11)
                        % 13) % 190 + 10
    draw = ImageDraw.Draw(img)

    def _text(y0, y1, seed):
        rnd = seed * 7919
        for i in range(14):
            x0 = (rnd + i * 61) % (W - 60)
            draw.rectangle([x0, y0 + 1, x0 + 26, y1 - 1], fill=245)
            draw.rectangle([x0 + 5, y0 + 3, x0 + 12, y1 - 3], fill=10)

    if strip is not None:
        _text(strip[0], strip[1], text_seed)
    if subtitle is not None:
        # 真实对白字幕形态：居中稀疏几块、逐秒移位（与赞助整行
        # 铺满的形态分家，持续性才分得开）
        y0, y1, seed = subtitle
        off = (seed * 37) % 90 - 45
        for i in range(3):
            x0 = W // 2 - 70 + i * 55 + off
            draw.rectangle([x0, y0 + 1, x0 + 30, y1 - 1], fill=245)
            draw.rectangle([x0 + 6, y0 + 3, x0 + 14, y1 - 3],
                           fill=10)
    return img


def _series(seconds, scene_at, strip_at):
    """按秒生成 2fps 帧列：scene_at/strip_at 是 t(秒)→值的函数。"""
    frames = []
    for s in range(seconds):
        for k in range(FPS2):
            frames.append(_frame(scene_at(s), strip=strip_at(s),
                                 text_seed=7,
                                 frame_idx=s * FPS2 + k))
    return frames


# ---------------------------------------------------------- 检测与门控


def test_detect_persistent_band_across_scenes():
    # 带 0–15s、横跨两个场景（0–14s 场景1、15s 起场景2 且带消失）
    strip = (200, 230)  # bottom 区，未吃进字幕区（248 起）
    frames = _series(
        30, scene_at=lambda t: 1 if t < 8 else 2,
        strip_at=lambda t: strip if t < 15 else None)
    det = sf.detect_band(frames, H, hint_zone="bottom")
    assert det["ok"] is True
    assert det["zone"] == "bottom"
    assert det["conflict"] is False
    y0, y1 = det["strip_det"]
    assert abs(y0 - 200) <= 3 and abs(y1 - 230) <= 3
    assert det["band_end_sec"] is not None
    assert 13.0 <= det["band_end_sec"] <= 17.0


def test_gate_persistence_rejects_single_card_notice():
    # 试点事故形态：提示文字只活在一张卡里（背景全程同一场景，
    # 文字 0–10s）——命中块虽有 2 个但场景不互异 → 拒绝。
    strip = (200, 230)
    frames = _series(
        30, scene_at=lambda t: 1,
        strip_at=lambda t: strip if t < 10 else None)
    det = sf.detect_band(frames, H)
    assert det["ok"] is False
    assert det["reason"] == "gate_persistence"


def test_hint_is_prior_only_conflict_recorded():
    # 真带在底部、摸底 hint 错标 top → 全区扫描以真带为准、记冲突
    strip = (200, 230)
    frames = _series(
        30, scene_at=lambda t: 1 if t < 8 else 2,
        strip_at=lambda t: strip if t < 15 else None)
    det = sf.detect_band(frames, H, hint_zone="top")
    assert det["ok"] is True
    assert det["zone"] == "bottom"
    assert det["conflict"] is True


def test_gate_subtitle_conflict_when_unsplittable():
    # 条带整个落在底部字幕区内且全行静态持续（无法证明不含字幕
    # 行）→ fail-closed 整条放弃
    strip = (252, 266)
    frames = _series(
        30, scene_at=lambda t: 1 if t < 10 else 2,
        strip_at=lambda t: strip if t < 20 else None)
    det = sf.detect_band(frames, H)
    assert det["ok"] is False
    assert det["reason"] == "gate_subtitle_conflict"


def test_subtitle_rows_narrowed_not_repaired_over():
    # 赞助行在上（静态持续）、字幕行在下（逐秒换位）且间隙 ≤6 行
    # 被合并进同一行段 → 隔离门控收窄到赞助行、字幕行不许动
    frames = []
    for s in range(30):
        for k in range(FPS2):
            strip = (200, 240) if s < 20 else None
            # 字幕：3–9s 同一句长对白（位置不动、会进峰值块行段），
            # 10s 后逐秒换句——持续性与赞助行天然分家
            if 3 <= s < 10:
                sub = (242, 262, 3)
            elif 10 <= s < 20:
                sub = (242, 262, s % 5)
            else:
                sub = None
            frames.append(_frame(1 if s < 10 else 2, strip=strip,
                                 text_seed=7, subtitle=sub,
                                 frame_idx=s * FPS2 + k))
    det = sf.detect_band(frames, H)
    assert det["ok"] is True
    assert det["narrowed"] is True
    y0, y1 = det["strip_det"]
    assert abs(y0 - 200) <= 3
    assert y1 <= 246  # 字幕行（244 起）不许留在处理条带内


def test_detect_no_band():
    frames = _series(20, scene_at=lambda t: 1,
                     strip_at=lambda t: None)
    det = sf.detect_band(frames, H)
    assert det["ok"] is False
    assert det["reason"] in ("no_band_found", "gate_persistence")


def test_best_run_merges_two_text_lines():
    # 两行字（间隙 4 行）必须合并成一段——旧版只圈最强一行、
    # 第二行整行残留的回归
    profile = [1.0] * 100
    for r in range(20, 30):
        profile[r] = 30.0
    for r in range(34, 44):
        profile[r] = 25.0
    run = sf.best_run(profile, 0, 100, 5.0)
    assert run is not None
    assert run[0] <= 20 and run[1] >= 44


def test_band_end_from_ratios():
    ratios = [0.9] * 15 + [0.1] * 10
    assert sf._band_end_from_ratios(ratios) == 15.0
    assert sf._band_end_from_ratios([0.9] * 10) is None  # 持续到窗尾
    assert sf._band_end_from_ratios([0.0] * 5) == 0.0


def test_repair_end_expand_capped_by_mouth():
    assert sf.repair_end_sec(15.0, 60.0) == 17.0   # 外扩 2 秒
    assert sf.repair_end_sec(59.5, 60.0) == 60.0   # 不越口子
    assert sf.repair_end_sec(62.0, 60.0) == 60.0


# ---------------------------------------------------------- 分类与处理


def test_classify_background_uniform_vs_complex():
    dark = Image.new("RGB", (320, 180), (8, 8, 8))
    draw = ImageDraw.Draw(dark)
    draw.rectangle([40, 100, 200, 112], fill=(240, 240, 240))
    uniform, metrics = sf.classify_background(dark, (96, 120))
    assert uniform is True
    assert metrics["std_clamped"] < 15.0

    import random

    rnd = random.Random(42)
    noisy = Image.new("RGB", (320, 180))
    px = noisy.load()
    for y in range(180):
        for x in range(320):
            px[x, y] = (rnd.randrange(256),) * 3
    uniform2, _ = sf.classify_background(noisy, (96, 120))
    assert uniform2 is False


def test_process_frame_only_touches_strip():
    import random

    rnd = random.Random(7)
    img = Image.new("RGB", (160, 90))
    px = img.load()
    for y in range(90):
        for x in range(160):
            px[x, y] = (rnd.randrange(256),) * 3
    strip = (40, 55)
    for method in ("fill", "blur"):
        out = sf.process_frame(img, strip, method)
        assert out.size == img.size
        orig = img.load()
        new = out.load()
        for y in list(range(0, 30)) + list(range(65, 90)):
            for x in range(0, 160, 7):
                assert new[x, y] == orig[x, y]  # 带外零改动
        changed = any(new[x, 47] != orig[x, 47] for x in range(160))
        assert changed  # 条带内确实被处理


# ---------------------------------------------------------- 熔断


def test_host_ok_strict_tightened_breaker():
    assert sf.host_ok_strict(4_000_000, 3.0) is True
    assert sf.host_ok_strict(1_000_000, 3.0) is False   # <1.2GB
    assert sf.host_ok_strict(4_000_000, 6.5) is False   # load1 >6
    # 与借帧线宽松版对照：同输入在 graft 口径下是放行的——自帧线
    # 不许悄悄退回宽松阈值
    assert graft.host_ok(1_300_000, 20.0) is False
    assert graft.host_ok(4_000_000, 20.0) is True
    assert sf.host_ok_strict(4_000_000, 20.0) is False


# ---------------------------------------------------------- 记账（DB）

SRC_SELF = 98911
LIB_SELF = "自帧测试库"


def _cleanup_self(db):
    db.query(em.CmsGraftRepair).filter(
        em.CmsGraftRepair.source_id == SRC_SELF).delete(
        synchronize_session=False)
    db.query(em.UpstreamItemMapping).filter(
        em.UpstreamItemMapping.source_id == SRC_SELF).delete(
        synchronize_session=False)
    db.query(em.MediaItem).filter(
        em.MediaItem.name.like("自帧%")).delete(
        synchronize_session=False)
    db.query(em.UpstreamSource).filter(
        em.UpstreamSource.id == SRC_SELF).delete(
        synchronize_session=False)
    db.query(em.Library).filter(
        em.Library.name == LIB_SELF).delete(synchronize_session=False)
    db.commit()


@pytest.fixture(scope="module")
def seed_self():
    db = SessionLocal()
    _cleanup_self(db)
    db.add(em.UpstreamSource(
        id=SRC_SELF, name="自帧毒源", source_type="applecms",
        base_url="https://cms.example.com", auth_method="none",
        enabled=True, credential_scope="source",
        health_status="unknown"))
    lib = em.Library(guid=_guid(), name=LIB_SELF,
                     collection_type="movies", is_enabled=True)
    db.add(lib)
    db.flush()
    item = em.MediaItem(
        guid=_guid(), library_id=lib.id, item_type="movie",
        name="自帧样本片", file_path="mount://3/自帧样本片.mp4",
        container="mp4", size=1_000_000, duration_ticks=600_000_000,
        probe_status="ok", tmdb_id="999")
    db.add(item)
    db.flush()
    mapping = em.UpstreamItemMapping(
        local_item_id=item.id, source_id=SRC_SELF,
        upstream_item_id=f"vod-{item.id}-{SRC_SELF}", confidence=0.9,
        evidence="tmdb")
    db.add(mapping)
    db.commit()
    data = {"item": item.id, "mapping": mapping.id}
    db.close()
    yield data
    db = SessionLocal()
    try:
        _cleanup_self(db)
    finally:
        db.close()


def test_upsert_method_roundtrip(seed_self):
    db = SessionLocal()
    try:
        graft.upsert_repair(
            db, mapping_id=seed_self["mapping"],
            local_item_id=seed_self["item"], source_id=SRC_SELF,
            status="repaired", method="self_blur",
            segments=[{"idx": 0, "dur": 6.0, "file": "seg_000.ts"}])
        db.commit()
        row = graft.get_repair(db, seed_self["mapping"])
        assert row.method == "self_blur"
        # 未验不下发（与 method 无关，只认 verified）
        assert graft.lookup_verified(
            db, seed_self["item"], seed_self["mapping"],
            SRC_SELF) is None
        graft.upsert_repair(
            db, mapping_id=seed_self["mapping"],
            local_item_id=seed_self["item"], source_id=SRC_SELF,
            status="verified")
        db.commit()
        assert graft.lookup_verified(
            db, seed_self["item"], seed_self["mapping"],
            SRC_SELF) is not None
    finally:
        db.close()


def test_process_candidate_self_flow(seed_self, monkeypatch):
    # 管线判定与验证都打桩（类/模块属性口径）：验记账全流程——
    # running→repaired 落 method、verify 后 verified、复跑短路。
    db = SessionLocal()
    try:
        db.query(em.CmsGraftRepair).filter(
            em.CmsGraftRepair.mapping_id == seed_self["mapping"]).delete(
            synchronize_session=False)
        db.commit()
    finally:
        db.close()
    def _fake_process(db, mapping, *, mouth, stats=None, verdict=None):
        return {"status": "repaired", "reason": "",
                "segments": [{"idx": 0, "dur": 6.0,
                              "file": "seg_000.ts", "frames": 150}],
                "band_end_sec": 15.0, "band_family": "",
                "strip": {"y0": 200, "y1": 230, "h": 270},
                "method": "self_fill", "detail": "{}"}

    monkeypatch.setattr(sf, "process_self", _fake_process)

    def _fake_verify(db, mid, stats=None):
        # 与真 verify_repair 同口径：verified 由验证环节自己落库
        row = graft.get_repair(db, mid)
        graft.upsert_repair(
            db, mapping_id=mid, local_item_id=row.local_item_id,
            source_id=row.source_id, status="verified", reason="")
        db.commit()
        return {"status": "verified", "reason": ""}

    monkeypatch.setattr(graft, "verify_repair", _fake_verify)
    db = SessionLocal()
    try:
        out = sf.process_candidate_self(
            db, {"mapping_id": seed_self["mapping"], "heat": 3},
            mouth=60.0)
        assert out["status"] == "verified"
        assert out["method"] == "self_fill"
        row = graft.get_repair(db, seed_self["mapping"])
        assert row.status == "verified"
        assert row.method == "self_fill"
        assert row.band_end_sec == 15.0
        out2 = sf.process_candidate_self(
            db, {"mapping_id": seed_self["mapping"], "heat": 3},
            mouth=60.0)
        assert out2["reason"] == "already"
    finally:
        db.close()


def test_process_candidate_self_skip_keeps_method(seed_self,
                                                  monkeypatch):
    db = SessionLocal()
    try:
        db.query(em.CmsGraftRepair).filter(
            em.CmsGraftRepair.mapping_id == seed_self["mapping"]).delete(
            synchronize_session=False)
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(
        sf, "process_self",
        lambda db, mapping, *, mouth, stats=None, verdict=None: {
            "status": "skipped", "reason": "gate_persistence",
            "detail": ""})
    db = SessionLocal()
    try:
        out = sf.process_candidate_self(
            db, {"mapping_id": seed_self["mapping"], "heat": 1},
            mouth=60.0)
        assert out["status"] == "skipped"
        assert out["reason"] == "gate_persistence"
        row = graft.get_repair(db, seed_self["mapping"])
        assert row.status == "skipped"
        assert row.method == "self"
    finally:
        db.close()
