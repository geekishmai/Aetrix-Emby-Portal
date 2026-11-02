"""MetaTube（番号）元数据源：本地 metatube-server HTTP API 适配。

## 启用与边界

- **环境变量启用**：``METATUBE_BASE_URL``（例如 ``http://a-metatube:8080``）
  非空才启用；未配置时配置快照里本源整体不启用（``config.read_config``），
  刮削链路一个请求都不会发（见 :func:`configured`）。
- **只对番号形态标题发请求**：:func:`extract_code` 正则不命中立即返回
  ``None``（零网络请求）——正常电影/剧集标题不会打到 metatube。
- **单次搜索**：``GET {base}/v1/movies/search?q=<番号>``，取 ``number``
  与番号精确匹配的首条（多 provider 结果里不精确匹配一律放弃，宁可漏配
  也不错配，与 metasources 其它源同一口径）。
- 上游错误统一抛 ``sources.SourceError``（带 HTTP status）：引擎按既有
  口径喂 ``metasources.breaker`` 熔断，不新造一套失败处理。

## 字段口径

metatube 搜索返回 title / score（5 分制）/ cover_url / release_date /
actors。映射到 ``Hit`` 时**如实留空**：overview 与 genres 搜索接口不给
（详情接口路由在 v1.4.0 未开放通配，不为此多造一次请求）；rating 因
5 分制与 Aetrix 10 分制口径不一致，同样留空（防把 4.5 当 4.5/10 用）。
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from typing import Optional

REQUEST_TIMEOUT = 12
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

BASE_URL_ENV = "METATUBE_BASE_URL"


def base_url() -> str:
    """本源基址（env 读取，每次现读：容器改地址不必重启进程即生效）"""
    return (os.environ.get(BASE_URL_ENV, "") or "").strip().rstrip("/")


def configured() -> bool:
    return bool(base_url())


# 番号形态两种：
# - 字母 2-7 + 可选分隔符 + 2-5 位数字（SSIS-001 / hsoda-058 / abc123）
# - 数字前缀 1-4 + 字母 2-7 + 可选分隔符 + 2-5 位数字（300MIUM-1461）
_FORM_LETTERS = re.compile(r"^([A-Za-z]{2,7})[-_ ]?(\d{2,5})$")
_FORM_DIGITS = re.compile(r"^(\d{1,4})([A-Za-z]{2,7})[-_ ]?(\d{2,5})$")

# 誤爆黑名单：技术token（S01E01 / 1080p / x264 / WEB-DL 等）在形态上可能命中，
# 按字母段排除。只列实测会误报的，不做穷举。
_NOISE = {
    "s", "e", "ep", "eps", "episode", "part", "pt", "cd", "disc", "disk",
    "vol", "ver", "v", "h", "x", "hd", "fhd", "uhd", "sd", "web", "webdl",
    "webrip", "dl", "bluray", "bdrip", "brrip", "remux", "mp", "mkv", "mp4",
    "avi", "mov", "flv", "wmv", "mpeg", "hevc", "avc", "aac", "ac", "dts",
    "hdr", "dovi", "dv", "bit", "k", "p", "i", "sample", "trailer",
}


def _token_code(token: str) -> Optional[str]:
    token = token.strip()
    if not token:
        return None
    m = _FORM_LETTERS.match(token)
    if m:
        letters = m.group(1)
        if letters.lower() not in _NOISE:
            return f"{letters.upper()}-{m.group(2)}"
        return None
    m = _FORM_DIGITS.match(token)
    if m:
        letters = m.group(2)
        if letters.lower() not in _NOISE:
            return f"{m.group(1)}{letters.upper()}-{m.group(3)}"
    return None
    return None


def extract_code(title: str) -> Optional[str]:
    """从标题里抽番号（如 ``SSIS-001`` / ``300MIUM-1461``）；抽不到返回 None。"""
    for token in re.split(r"[\s\[\]()【】{}.,，、!！?？]+", str(title or "")):
        code = _token_code(token)
        if code:
            return code
    return None


def _get_json(url: str) -> dict:
    """GET JSON；失败统一抛 SourceError（status 供熔断识别 403/429）"""
    from backend.emby_server.metasources.sources import SourceError

    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", "ignore")
    except Exception as exc:  # noqa: BLE001 — 统一成 SourceError，交给引擎隔离
        status = getattr(exc, "code", None)
        raise SourceError(f"{type(exc).__name__}: {exc}", status=status) from exc
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise SourceError("返回的不是 JSON") from exc
    if not isinstance(data, dict):
        raise SourceError("返回结构不是对象")
    return data


def _norm(number: str) -> str:
    return re.sub(r"[^0-9a-z]", "", str(number or "").lower())


def search(title: str, year: Optional[int], kind: str, *, pool, gate):
    """搜索一条番号元数据；非番号标题返回 None（不发网络）。

    签名与其它源一致（``pool``/``gate`` 由引擎注入）；本源不需要密钥，
    ``pool`` 不参与判定。
    """
    from backend.emby_server.metasources.sources import Hit, SourceError

    if not configured():
        raise SourceError(f"未配置 {BASE_URL_ENV}", count_failure=False)
    code = extract_code(title)
    if not code:
        return None

    gate.acquire()
    url = f"{base_url()}/v1/movies/search?q={urllib.parse.quote(code)}"
    payload = _get_json(url)
    items = payload.get("data")
    if not isinstance(items, list):
        return None
    wanted = _norm(code)
    for raw in items:
        if not isinstance(raw, dict):
            continue
        if _norm(raw.get("number") or "") != wanted:
            continue  # 精确 number 匹配，宁可漏配不错配
        year_of = str(raw.get("release_date") or "")[:4]
        return Hit(
            source="metatube",
            title=str(raw.get("title") or ""),
            original_title="",
            overview="",   # 搜索接口不给简介（如实留空）
            genres=[],     # 同左
            rating=None,   # score 是 5 分制，与 10 分制口径不一致，留空
            year=int(year_of) if year_of.isdigit() else None,
            poster=str(raw.get("cover_url") or raw.get("thumb_url") or "") or None,
            lang="ja",
            external_ids={"metatube": str(raw.get("id") or "")} if raw.get("id") else {},
            kind_hint="movie",
        )
    return None
