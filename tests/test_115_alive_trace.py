"""115 出流逐跳留痕 + byos115_direct 探测对齐（2026-11-02 取证 §6 后续）。

取证定案：#1729 后凡走到 115 取链的起播服务端全成功，失败全在
302 之后（播放器↔115 CDN），但证据链断——探测结果没有结构化
落账，逐跳日志还会被部署重启销毁。本文件钉住三件事：

1. check_115_download_alive 的定性只有一处（detailed 版），且
   结论带回探测状态码；UA 留痕只记 SHA-256 指纹、绝不记原文；
2. serve_115_copy 两条出流（主路径 + quota 回落）与 byos115_direct
   每跳落一条结构化日志（byos115_alive_probe）+ 一条同口径台账行
   （error_kind=alive_probe_<结论>），探测 UA 用实际签名 UA；
3. byos115_direct 此前取链成功即 302、死链零感知——补同口径
   探测：dead 不发 302、409 明示（挂载原件无副本可重建，只明示）。

签名 UA 口径（本单核查结论，见 PR 说明）：BYOS 两条 resolve 只用
播放器请求 UA 签名（为空回服务器级 PAN115_UA），不查账号配置档
UA；返回 Signed115Url 把实际签名 UA 透传给出流留痕。

全部离线：探测 HTTP、取链客户端、门禁链路全用桩。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import asyncio
import hashlib
import logging
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer115
from backend.emby_server import transfer_variant as tv
from backend.emby_server import user_bindings as ub

REQ_UA = "TracePlayer/1.0 (iPhone; iOS 17)"


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _binding115(db, user_id):
    cred = em.MountCredential(scope="user", owner_user_id=user_id,
                              status="active")
    db.add(cred)
    db.commit()
    row = em.UserStorageBinding(user_id=user_id, provider="pan115",
                                account_label="test115",
                                credential_id=cred.id, status="active")
    db.add(row)
    db.commit()
    return row


def _run(coro):
    return asyncio.run(coro)


# ---------- UA 指纹 ----------


def test_ua_fingerprint_is_hash_only():
    fp = ub.ua_fingerprint(REQ_UA)
    assert fp == hashlib.sha256(REQ_UA.encode("utf-8")).hexdigest()[:12]
    assert len(fp) == 12
    assert REQ_UA not in fp
    assert ub.ua_fingerprint("") == ""
    assert ub.ua_fingerprint("   ") == ""
    assert ub.ua_fingerprint("OtherPlayer/2.0") != fp


# ---------- 探测：定性 + 状态码 ----------


def test_alive_detailed_returns_status(monkeypatch):
    monkeypatch.setattr(ub, "_pan115_probe_http", lambda url, ua="": 206)
    assert ub.check_115_download_alive_detailed(
        "https://115.example/dl") == ("alive", 206)
    monkeypatch.setattr(ub, "_pan115_probe_http", lambda url, ua="": 404)
    assert ub.check_115_download_alive_detailed(
        "https://115.example/dl") == ("dead", 404)
    monkeypatch.setattr(ub, "_pan115_probe_http", lambda url, ua="": 410)
    assert ub.check_115_download_alive_detailed(
        "https://115.example/dl") == ("dead", 410)
    monkeypatch.setattr(ub, "_pan115_probe_http", lambda url, ua="": 403)
    assert ub.check_115_download_alive_detailed(
        "https://115.example/dl") == ("unknown", 403)

    def _boom(url, ua=""):
        raise RuntimeError("probe down")

    monkeypatch.setattr(ub, "_pan115_probe_http", _boom)
    assert ub.check_115_download_alive_detailed(
        "https://115.example/dl") == ("unknown", None)
    assert ub.check_115_download_alive_detailed("") == ("unknown", None)


def test_alive_result_is_str_with_status(monkeypatch):
    """旧口径不许破：返回值与裸字符串相等比较成立，同时带 .status。"""
    monkeypatch.setattr(ub, "_pan115_probe_http", lambda url, ua="": 200)
    result = ub.check_115_download_alive("https://115.example/dl")
    assert result == "alive"
    assert isinstance(result, str)
    assert result.status == 200


# ---------- 结构化日志：字段齐全 + 不带原文 ----------


def test_log_115_alive_probe_fields_and_no_raw_ua(caplog):
    sign_ua = "SignAgent/3.0 build-xyz"
    with caplog.at_level(logging.INFO):
        ub.log_115_alive_probe(
            item_id=123, user_id=7, size=5368709120, verdict="dead",
            status=404, req_ua=REQ_UA, sign_ua=sign_ua,
            path="transfer115_copy")
    line = next(
        text for text in caplog.messages if "byos115_alive_probe" in text)
    assert "item=123" in line
    assert "user=7" in line
    assert "size=5368709120" in line
    assert "verdict=dead" in line
    assert "status=404" in line
    assert "path=transfer115_copy" in line
    assert f"req_ua_fp={ub.ua_fingerprint(REQ_UA)}" in line
    assert f"sign_ua_fp={ub.ua_fingerprint(sign_ua)}" in line
    # 原文绝不入日志（含指纹之外的任何片段）
    assert REQ_UA not in caplog.text
    assert sign_ua not in caplog.text
    assert "build-xyz" not in caplog.text


# ---------- resolve：签名 UA 透传 ----------


class _Fake115:
    """与 Pan115Client 同 UA 口径的桩：传入 UA 非空用它，否则默认。"""

    def __init__(self, cookie, ua=""):
        self.ua = (ua or "").strip() or transfer115.PAN115_UA

    def download_url(self, pickcode, **kw):
        return "https://115.example/dl"


class _Fake115NoUaAttr:
    """不暴露 .ua 的桩（仓内既有测试桩形态）：resolver 须镜像推导。"""

    def __init__(self, cookie, ua=""):
        pass

    def download_url(self, pickcode, **kw):
        return "https://115.example/dl2"


def _patch_client(monkeypatch, client_cls):
    monkeypatch.setattr(ub, "unseal_secret", lambda sealed: "UID=1_A1_1; CID=x")
    monkeypatch.setattr(transfer115, "Pan115Client", client_cls)


def test_copy_url_carries_player_sign_ua(db, monkeypatch):
    _binding115(db, 7)
    _patch_client(monkeypatch, _Fake115)
    url = ub.resolve_user115_copy_url(db, 7, "PC1", ua="Lavf/60.16.100")
    assert url == "https://115.example/dl"
    assert isinstance(url, str)
    assert url.sign_ua == "Lavf/60.16.100"


def test_copy_url_without_ua_signs_with_server_default(db, monkeypatch):
    _binding115(db, 7)
    _patch_client(monkeypatch, _Fake115)
    url = ub.resolve_user115_copy_url(db, 7, "PC1")
    assert url.sign_ua == transfer115.PAN115_UA


def test_copy_url_client_without_ua_attr_still_traced(db, monkeypatch):
    _binding115(db, 7)
    _patch_client(monkeypatch, _Fake115NoUaAttr)
    url = ub.resolve_user115_copy_url(db, 7, "PC1", ua="Lavf/60.16.100")
    assert url == "https://115.example/dl2"
    assert url.sign_ua == "Lavf/60.16.100"


# ---------- video_stream：探测跳留痕与 byos 探测 ----------


class _FakeQuery:
    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return None

    def all(self):
        return []


class _FakeDB:
    def query(self, *args, **kwargs):
        return _FakeQuery()


def _base_stream_stubs(monkeypatch, item, notes):
    from backend.emby_server import api
    from backend.emby_server import playback_strategy as _strategy

    monkeypatch.setattr(api, "_require_visible_item",
                        lambda db, user, item_id: item)
    monkeypatch.setattr(api, "ensure_playback_allowed",
                        lambda db, user: None)
    monkeypatch.setattr(
        "backend.emby_server.playback_gate.ensure_binding_playback_allowed",
        lambda db, u, r=None: None)
    monkeypatch.setattr(
        "backend.emby_server.playback_gate._request_uses_issued_play_sign",
        lambda request, db: False)
    monkeypatch.setattr(api, "_observe_traffic", lambda resp: resp)
    monkeypatch.setattr(api.play_sign, "check_referer",
                        lambda request, db: None)
    monkeypatch.setattr(
        api, "_note_play_start_safe",
        lambda item, **kwargs: notes.append(kwargs))

    def fake_get(db, key, default="", **kwargs):
        return default

    monkeypatch.setattr(_strategy.store, "get_value", fake_get)
    return api


def _request(query_string=b"", ua=REQ_UA):
    headers = [(b"user-agent", ua.encode())] if ua else []
    scope = {"type": "http", "method": "GET",
             "path": "/Videos/item/stream", "raw_path": b"/Videos/item/stream",
             "query_string": query_string, "headers": headers,
             "scheme": "http", "server": ("testserver", 80),
             "client": ("testclient", 50000), "root_path": ""}
    return Request(scope)


def _probe_lines(caplog):
    return [t for t in caplog.messages if "byos115_alive_probe" in t]


def test_stream_115_copy_alive_probe_hop_traced(monkeypatch, caplog):
    notes = []
    item = SimpleNamespace(id=123, drive_file_id="origfid", container="mkv",
                           file_path="115:/电影/a.mkv", size=1000)
    api = _base_stream_stubs(monkeypatch, item, notes)
    monkeypatch.setattr(tv, "resolve_play_file_id",
                        lambda db, item, user: ("origfid", None))
    monkeypatch.setattr(
        tv, "ensure_user_copy_gate",
        lambda db, item, user, **kwargs: {
            "verdict": "serve_115_copy",
            "variant": SimpleNamespace(pickcode="PC123", id=99,
                                       size_bytes=5368709120)})
    monkeypatch.setattr(
        ub, "resolve_user115_copy_url",
        lambda db, user_id, pickcode, ua="": ub.Signed115Url(
            "https://115.example/dl", sign_ua="SignAgent/3.0"))
    seen = {}

    def _alive(url, ua=""):
        seen["probe_ua"] = ua
        return ub.AliveProbeResult("alive", 206)

    monkeypatch.setattr(ub, "check_115_download_alive", _alive)
    with caplog.at_level(logging.INFO):
        resp = _run(api.video_stream(
            "item", _request(), SimpleNamespace(id=7), _FakeDB()))
    assert resp.status_code == 302
    # 探测必须用实际签名 UA（与请求 UA 不同时以签名 UA 为准）
    assert seen["probe_ua"] == "SignAgent/3.0"
    # 台账探测行：与解析行同 method，error_kind 记本跳结论
    hop = [n for n in notes
           if (n.get("error_kind") or "").startswith("alive_probe_")]
    assert len(hop) == 1
    assert hop[0]["method"] == "transfer115_copy"
    assert hop[0]["result"] == "ok"
    assert hop[0]["error_kind"] == "alive_probe_alive"
    # 结构化日志：双指纹都在、原文都不在
    lines = _probe_lines(caplog)
    assert len(lines) == 1
    line = lines[0]
    assert "path=transfer115_copy" in line
    assert "status=206" in line
    assert "size=5368709120" in line
    assert f"req_ua_fp={ub.ua_fingerprint(REQ_UA)}" in line
    assert f"sign_ua_fp={ub.ua_fingerprint('SignAgent/3.0')}" in line
    assert REQ_UA not in line
    assert "SignAgent/3.0" not in line


def test_stream_115_copy_dead_probe_hop_traced(monkeypatch, caplog):
    notes = []
    item = SimpleNamespace(id=123, drive_file_id="origfid", container="mkv",
                           file_path="115:/电影/a.mkv", size=1000)
    api = _base_stream_stubs(monkeypatch, item, notes)
    monkeypatch.setattr(tv, "resolve_play_file_id",
                        lambda db, item, user: ("origfid", None))
    monkeypatch.setattr(
        tv, "ensure_user_copy_gate",
        lambda db, item, user, **kwargs: {
            "verdict": "serve_115_copy",
            "variant": SimpleNamespace(pickcode="PC123", id=99,
                                       size_bytes=1000)})
    monkeypatch.setattr(tv, "mark_variant_stale_once",
                        lambda db, vid: None)
    monkeypatch.setattr(
        tv, "rebuild_dead_user115_copy",
        lambda db, item, uid: {"verdict": "preparing",
                               "reason": "job_rebuilt", "job_id": 5})
    monkeypatch.setattr(
        ub, "resolve_user115_copy_url",
        lambda db, user_id, pickcode, ua="": "https://115.example/dl")
    monkeypatch.setattr(
        ub, "check_115_download_alive",
        lambda url, ua="": ub.AliveProbeResult("dead", 404))
    with caplog.at_level(logging.INFO):
        with pytest.raises(HTTPException) as excinfo:
            _run(api.video_stream(
                "item", _request(), SimpleNamespace(id=7), _FakeDB()))
    assert excinfo.value.status_code == 409
    kinds = [n.get("error_kind") for n in notes]
    assert "alive_probe_dead" in kinds
    assert "115_copy_dead" in kinds
    # 探测行排在副本判死行之前（时间线顺序）
    assert kinds.index("alive_probe_dead") < kinds.index("115_copy_dead")
    lines = _probe_lines(caplog)
    assert len(lines) == 1
    assert "verdict=dead" in lines[0]
    assert "status=404" in lines[0]


def _byos_setup(monkeypatch, alive_result, notes):
    item = SimpleNamespace(id=124, drive_file_id="", container="mkv",
                           file_path="mount://3/电影/a.mkv",
                           size=2147483648)
    api = _base_stream_stubs(monkeypatch, item, notes)
    monkeypatch.setattr(api, "_available_variant_names",
                        lambda db, item, user: ["byos115_direct"])
    monkeypatch.setattr(
        ub, "resolve_user115_direct_url",
        lambda db, user_id, item, ua="": ub.Signed115Url(
            "https://115.example/direct", sign_ua=REQ_UA,
            size=2147483648))
    seen = {}

    def _alive(url, ua=""):
        seen["probe_ua"] = ua
        return alive_result

    monkeypatch.setattr(ub, "check_115_download_alive", _alive)
    return api, seen


def test_stream_byos115_direct_alive_probes_then_302(monkeypatch, caplog):
    notes = []
    api, seen = _byos_setup(
        monkeypatch, ub.AliveProbeResult("alive", 206), notes)
    with caplog.at_level(logging.INFO):
        resp = _run(api.video_stream(
            "item", _request(query_string=b"variant=byos115_direct"),
            SimpleNamespace(id=7), _FakeDB()))
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://115.example/direct"
    assert seen["probe_ua"] == REQ_UA
    hop = [n for n in notes
           if (n.get("error_kind") or "").startswith("alive_probe_")]
    assert len(hop) == 1
    assert hop[0]["method"] == "byos115_direct"
    assert hop[0]["error_kind"] == "alive_probe_alive"
    lines = _probe_lines(caplog)
    assert len(lines) == 1
    assert "path=byos115_direct" in lines[0]


def test_stream_byos115_direct_dead_409_no_302(monkeypatch):
    notes = []
    api, _seen = _byos_setup(
        monkeypatch, ub.AliveProbeResult("dead", 410), notes)
    with pytest.raises(HTTPException) as excinfo:
        _run(api.video_stream(
            "item", _request(query_string=b"variant=byos115_direct"),
            SimpleNamespace(id=7), _FakeDB()))
    assert excinfo.value.status_code == 409
    kinds = [n.get("error_kind") for n in notes]
    assert "alive_probe_dead" in kinds
    assert "115_direct_dead" in kinds


def test_stream_byos115_direct_unknown_still_302(monkeypatch):
    notes = []
    api, _seen = _byos_setup(
        monkeypatch, ub.AliveProbeResult("unknown", 403), notes)
    resp = _run(api.video_stream(
        "item", _request(query_string=b"variant=byos115_direct"),
        SimpleNamespace(id=7), _FakeDB()))
    assert resp.status_code == 302
