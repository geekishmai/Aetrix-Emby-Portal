# -*- coding: utf-8 -*-
"""115 大文件取链回落（2026-10-31 生产实证）。

根因：webapi ``/files/download`` 对大文件回 state=False、msg_code=50028
「文件大小超出限制，请使用115电脑端下载」，转存副本播不了、巡检还把
副本记 strike 标 stale。本文件锁两件事（transfer115 客户端本身保留，
供管理员侧 115 共享挂载与 115→Drive 方向使用）：

1. 50028 必须被分类成通道限制错误（Pan115ChannelLimitError），不是
   普通业务错误；
2. download_url 在 50028 时回落 app chrome 加密通道取到地址；
   播放器场景（带 ua）走 app 通道优先（直链绑 UA，网页端用播放器
   UA 签的地址拉流会被 CDN 拒，生产探针实证）。

原第 3 件（用户 115 副本巡检对通道限制不记 strike）随用户绑盘线
二期删除：巡检不再处理 pan115: 落点行（整行跳过口径由
test_pan115_stale_rescue.py 的新用例钉住），本文件不再保留该用例。

全部离线：115 API 一律用 _http_json 桩 / 客户端桩。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer115
from backend.integrations import store
from backend.emby_server import stream_limits as sl


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    sl.clear_table_policy_cache()
    store.invalidate()
    yield session
    session.close()
    sl.clear_table_policy_cache()
    store.invalidate()


AUTH = "UID=123456_A1_1700000000; CID=x; SEID=y"
APP_URL = "https://cdnfhnfile.115cdn.net/fake/app-file.mkv"
WEB_URL = "https://cdnfhnfile.115.com/fake/web-file.mkv"
SIZE_LIMIT_BODY = {"state": False, "msg_code": 50028,
                   "msg": "文件大小超出限制，请使用115电脑端下载"}


# app 通道的响应 data 是服务端用私钥签出的加密串，离线无法自造
# （公开算法里 encrypt/decrypt 不是互逆对；与真服务端的互通已由
# 2026-10-31 生产探针实证）。离线测试打桩 rsa_decrypt，锁路由、
# 解析与分类；请求侧用确定性密文（随机前缀全零）逐字节断言。
_APP_INNER = {"3551001": {"file_name": "big.mkv", "file_size": 843844273,
                          "pick_code": "PC-BIG",
                          "url": {"url": APP_URL, "client": 2}}}


def _app_ok_body():
    return {"state": True, "errno": 0, "data": "STUB-CIPHER"}


def _stub_decrypt(monkeypatch):
    from backend.emby_server import pan115_cipher
    monkeypatch.setattr(pan115_cipher, "rsa_decrypt",
                        lambda b64: json.dumps(_APP_INNER).encode())


def _install_http(monkeypatch, web_body, app_body, seen):
    def _recorder(method, url, **kw):
        seen.append({"method": method, "url": url,
                     "params": kw.get("params"), "data": kw.get("data"),
                     "ua": kw.get("ua")})
        if "proapi.115.com" in url:
            return app_body() if callable(app_body) else app_body
        return web_body() if callable(web_body) else web_body

    monkeypatch.setattr(transfer115, "_http_json", _recorder)


# ---------- 分类 ----------

def test_50028_classified_as_channel_limit(monkeypatch):
    seen = []
    _install_http(monkeypatch, SIZE_LIMIT_BODY, {"state": True, "data": ""}, seen)
    client = transfer115.Pan115Client(AUTH)
    with pytest.raises(transfer115.Pan115Error) as excinfo:
        client._download_url_web("PC-BIG")
    assert isinstance(excinfo.value, transfer115.Pan115ChannelLimitError)
    assert excinfo.value.kind == "size_limit"


def test_cipher_encrypt_deterministic_single_block():
    from base64 import b64decode

    from backend.emby_server import pan115_cipher
    c1 = pan115_cipher.rsa_encrypt(b'{"pickcode":"X"}')
    c2 = pan115_cipher.rsa_encrypt(b'{"pickcode":"X"}')
    assert c1 == c2  # 随机前缀全零 → 确定性（与开源实现默认一致）
    assert len(b64decode(c1)) == 128  # RSA 1024 单块


# ---------- download_url 回落 ----------

def test_download_url_falls_back_to_app_on_50028(monkeypatch):
    seen = []
    _stub_decrypt(monkeypatch)
    _install_http(monkeypatch, SIZE_LIMIT_BODY, _app_ok_body, seen)
    client = transfer115.Pan115Client(AUTH)
    url = client.download_url("PC-BIG")
    assert url == APP_URL
    urls = [s["url"] for s in seen]
    assert any("webapi.115.com" in u for u in urls)
    assert any("proapi.115.com" in u and u.endswith("/app/chrome/downurl")
               for u in urls)
    # app 通道请求体是加密 data：与独立构造的期望密文逐字节一致
    # （随机前缀全零 → 确定性），期望明文含 pickcode 与 user_id
    from backend.emby_server import pan115_cipher
    app_call = [s for s in seen if "proapi.115.com" in s["url"]][0]
    expected = pan115_cipher.rsa_encrypt(json.dumps(
        {"pickcode": "PC-BIG", "user_id": 123456}).encode())
    assert app_call["data"] == {"data": expected}


def test_download_url_web_ok_no_app_call(monkeypatch):
    seen = []
    _install_http(monkeypatch, {"state": True, "file_url": WEB_URL},
                  _app_ok_body, seen)
    client = transfer115.Pan115Client(AUTH)
    assert client.download_url("PC-SMALL") == WEB_URL
    assert not any("proapi.115.com" in s["url"] for s in seen)


def test_download_url_app_first_skips_web(monkeypatch):
    seen = []
    _stub_decrypt(monkeypatch)
    _install_http(monkeypatch, {"state": True, "file_url": WEB_URL},
                  _app_ok_body, seen)
    client = transfer115.Pan115Client(AUTH, ua="Lavf/60.16.100")
    url = client.download_url("PC-BIG", app_first=True)
    assert url == APP_URL
    assert not any("webapi.115.com" in s["url"] for s in seen)


def test_download_url_both_channels_limited_raises_channel_limit(monkeypatch):
    seen = []
    app_limit = {"state": False, "errno": 50028, "msg": "文件大小超出限制"}
    _install_http(monkeypatch, SIZE_LIMIT_BODY, app_limit, seen)
    client = transfer115.Pan115Client(AUTH)
    with pytest.raises(transfer115.Pan115ChannelLimitError):
        client.download_url("PC-BIG")


def test_download_url_app_first_app_error_no_web_fallback(monkeypatch):
    """播放器场景 app 通道失败：不许回落网页端。

    网页端用播放器 UA 签出的地址拉流会被 CDN 拒（2026-10-31 探针
    实证 403「no cookie」），回落等于把死地址当成功发出去。必须
    如实抛错，让上层按 transient 定性。
    """
    seen = []
    app_err = {"state": False, "errno": 9999, "msg": "app 通道故障"}
    _install_http(monkeypatch, {"state": True, "file_url": WEB_URL},
                  app_err, seen)
    client = transfer115.Pan115Client(AUTH, ua="Lavf/60.16.100")
    with pytest.raises(transfer115.Pan115Error):
        client.download_url("PC-BIG", app_first=True)
    assert not any("webapi.115.com" in s["url"] for s in seen)


def test_download_url_app_first_app_limit_no_web_fallback(monkeypatch):
    """播放器场景 app 通道回 50028：直接按 size_limit 抛，不绕网页端
    （网页端同文件必然同样 50028，绕一圈只多一次上游请求）。"""
    seen = []
    app_limit = {"state": False, "errno": 50028, "msg": "文件大小超出限制"}
    _install_http(monkeypatch, {"state": True, "file_url": WEB_URL},
                  app_limit, seen)
    client = transfer115.Pan115Client(AUTH, ua="Lavf/60.16.100")
    with pytest.raises(transfer115.Pan115ChannelLimitError):
        client.download_url("PC-BIG", app_first=True)
    assert not any("webapi.115.com" in s["url"] for s in seen)
