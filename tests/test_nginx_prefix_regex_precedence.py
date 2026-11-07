# -*- coding: utf-8 -*-
"""回归测试：docker/nginx.conf 的 /api/、/emby/、/sso/ 必须使用 `location ^~` 前缀。

背景（2026-11-07 线上实证）：
`location ~* \\.(js|css|png|jpg|jpeg|gif|ico|svg|woff2?)$` 正则优先级高于普通前缀
`location /api/`，导致 `/api/site/boot.js` 被 nginx 当成前端静态文件查找而返回 404
（直连 api 容器为 200）。改为 `location ^~` 后，最长前缀命中即停止正则匹配，转发给后端。
"""
from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
NGINX_CONF = REPO / "docker" / "nginx.conf"

_LOC_HEADER_RE = re.compile(
    r"^\s*location\s+(?:(?P<mod>=\s*|\^~\s*|~\*\s*|~\s*))?(?P<pat>[^\s{]+)\s*\{",
    re.MULTILINE,
)


def _parse_locations(conf_text: str) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for m in _LOC_HEADER_RE.finditer(conf_text):
        mod = (m.group("mod") or "").strip()
        pat = m.group("pat").strip()
        brace_start = conf_text.find("{", m.start())
        depth = 0
        body = ""
        for idx in range(brace_start, len(conf_text)):
            ch = conf_text[idx]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    body = conf_text[brace_start + 1 : idx]
                    break
        out.append((mod, pat, body))
    return out


def _nginx_select_location(locations: list[tuple[str, str, str]], uri: str) -> tuple[str, str, str]:
    for mod, pat, body in locations:
        if mod == "=" and uri == pat:
            return (mod, pat, body)

    best_prefix: tuple[str, str, str] | None = None
    for mod, pat, body in locations:
        if mod in ("", "^~") and uri.startswith(pat):
            if best_prefix is None or len(pat) > len(best_prefix[1]):
                best_prefix = (mod, pat, body)

    if best_prefix is not None and best_prefix[0] == "^~":
        return best_prefix

    for mod, pat, body in locations:
        if mod in ("~", "~*"):
            flags = re.IGNORECASE if mod == "~*" else 0
            if re.search(pat, uri, flags):
                return (mod, pat, body)

    assert best_prefix is not None, f"URI {uri!r} 未命中任何 location"
    return best_prefix


def test_backend_prefixes_use_caret_tilde_and_beat_static_regex():
    conf = NGINX_CONF.read_text(encoding="utf-8")
    locs = _parse_locations(conf)

    by_mod_pat = {(mod, pat): body for mod, pat, body in locs}
    for prefix in ("/api/", "/emby/", "/sso/"):
        assert ("^~", prefix) in by_mod_pat, f"{prefix} 必须声明为 `location ^~ {prefix}`"
        assert "proxy_pass http://aetrix_backend" in by_mod_pat[("^~", prefix)]

    for backend_uri, expected_prefix in (
        ("/api/site/boot.js", "/api/"),
        ("/api/admin/export.json", "/api/"),
        ("/emby/Items/abc123/Images/Primary.jpg", "/emby/"),
        ("/emby/Items/abc123/Images/Logo.png", "/emby/"),
        ("/emby/web/icon.svg", "/emby/"),
        ("/sso/fanqiao/init.js", "/sso/"),
    ):
        mod, pat, body = _nginx_select_location(locs, backend_uri)
        assert (mod, pat) == ("^~", expected_prefix), (backend_uri, mod, pat)
        assert "proxy_pass http://aetrix_backend" in body

    mod_js, _, body_js = _nginx_select_location(locs, "/assets/js/index-BdUZNlEB.js")
    assert mod_js == "~*" and "immutable" in body_js

    mod_svg, _, body_svg = _nginx_select_location(locs, "/favicon.svg")
    assert mod_svg == "~*" and "immutable" in body_svg

    mod_html, pat_html, body_html = _nginx_select_location(locs, "/index.html")
    assert (mod_html, pat_html) == ("=", "/index.html") and "no-store" in body_html


def test_negative_probe_plain_prefix_gets_hijacked_by_static_regex():
    conf = NGINX_CONF.read_text(encoding="utf-8")
    broken_conf = conf.replace("location ^~ /api/", "location /api/", 1)
    locs = _parse_locations(broken_conf)
    mod, _, body = _nginx_select_location(locs, "/api/site/boot.js")
    assert mod == "~*" and "proxy_pass" not in body
