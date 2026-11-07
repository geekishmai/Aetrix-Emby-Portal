# -*- coding: utf-8 -*-
"""坏客户端播放地址还原口的 nginx 侧接线（2026-11-04）。

生产实证：`/embyhttps://v2.fluxquata.com/emby/Videos/<id>/stream...` 返回 200
（index.html）——aetrix-web 的 nginx 只把 `/api/` 与 `/emby/` 前缀转给后端，
该形态不匹配 `/emby/`（`embyh...` 后没有斜杠），落到 SPA 兜底。

本文件做**静态配置断言**（nginx 不在单测里跑）：
- 存在 `location /embyhttp` 前缀块（覆盖 http/https 两种 scheme）；
- 该块的 `proxy_pass` 用 `$request_uri` 透传原始 URI（nginx 默认
  merge_slashes=on 会把 `://` 并成 `:/`，归一化 URI 到后端匹配不上）；
- 块内带齐转发头与长读超时（中继流）；
- 该块出现在 SPA 兜底 `location /` 之前（可读性；前缀匹配本身与顺序无关）。
"""
from __future__ import annotations

import io
import re

CONF = "docker/nginx.conf"


def _conf() -> str:
    return io.open(CONF, encoding="utf-8").read()


def test_mangled_location_exists():
    s = _conf()
    assert re.search(r"location\s+(?:\^~\s+)?/embyhttp\s*\{", s), \
        "缺少 /embyhttp 前缀 location：/embyhttps://... 会落 SPA 兜底返回 200"


def test_mangled_location_uses_raw_request_uri():
    s = _conf()
    m = re.search(r"location\s+(?:\^~\s+)?/embyhttp\s*\{(.*?)\n    \}", s, re.S)
    assert m, "找不到 /embyhttp 块"
    block = m.group(1)
    assert "proxy_pass http://aetrix_backend$request_uri;" in block, \
        "必须用 $request_uri 透传原始 URI（否则 merge_slashes 把 :// 并成 :/）"


def test_mangled_location_forwards_headers_and_timeout():
    s = _conf()
    m = re.search(r"location\s+(?:\^~\s+)?/embyhttp\s*\{(.*?)\n    \}", s, re.S)
    block = m.group(1)
    for needle in (
        'proxy_http_version 1.1;',
        'proxy_set_header Connection "";',
        'proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;',
        'proxy_read_timeout 300s;',
        'proxy_buffering off;',
    ):
        assert needle in block, f"/embyhttp 缺少：{needle}"


def test_mangled_location_not_swallowed_by_spa_fallback():
    s = _conf()
    assert re.search(r"location\s+(?:\^~\s+)?/embyhttp", s).start() < s.index("location / {"), \
        "把 /embyhttp 写在 SPA 兜底之后可读性差，且后续维护容易误删"
