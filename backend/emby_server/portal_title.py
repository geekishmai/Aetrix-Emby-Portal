"""门户展示标题与今日封存准入规则（feat/portal-ui-p1，纯展示/过滤辅助，不改库结构）。

目标：
1. 卡片、详情与实时动态优先展示中文名或 TMDB 规范片名，剥除残留 PT 压制组/音轨/位深尾巴；
2. 「今日封存」严格只取 **已刮削 + 有海报 + 有中文名** 的条目，杜绝纯色块配脏英文发布名。
"""
from __future__ import annotations

import posixpath
import re
from typing import Any

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_BOOK_TITLE_RE = re.compile(r"《([^》]{1,200})》")
_BRACKET_META_RE = re.compile(
    r"\[[^\]]*(?:字幕|音轨|配音|双语|国粤|国英|简繁|中英|特效|合集|tmdbid?|tmdb)[^\]]*\]"
    r"|\{tmdb(?:id)?[-=]\d+\}",
    re.IGNORECASE,
)
_COLLECTION_RANGE_RE = re.compile(r"(?:合集|部曲|\d+\s*[-~–]\s*\d+)", re.IGNORECASE)
_GENERIC_DIRS = frozenset({
    "", "/", "媒体库", "电影", "剧集", "电视剧", "华语电影", "外语电影", "动画电影",
    "国产剧", "欧美剧", "日韩剧", "港台剧", "动漫", "番剧", "综艺", "纪录片", "演唱会",
    "specials", "bdmv", "stream",
})
_SEASON_DIR_RE = re.compile(r"^(?:season\s*\d+|s\d{1,2}|第[一二三四五六七八九十\d]+[季部])$", re.IGNORECASE)
_LEADING_CJK_TITLE_RE = re.compile(
    r"^([\u4e00-\u9fff0-9][\u4e00-\u9fff0-9：:·・，,！!？?—\-（）()\s]{0,38}?[\u4e00-\u9fff0-9）)])"
    r"(?:(?:\s+|\.)[A-Za-z]{2,}|\s*$)"
)


def contains_cjk(text: str | None) -> bool:
    return bool(text and _CJK_RE.search(text))


def clean_pt_title(raw: str | None) -> str:
    """剥除标题末尾残留的 PT 压制参数/官组名；若为「中文名 + 英文原名」双语串则优先取中文段。"""
    text = (raw or "").strip()
    if not text:
        return ""
    text = _BRACKET_META_RE.sub(" ", text).strip()
    from backend.emby_server import tmdb as tmdb_lib

    cleaned = tmdb_lib._clean_query(text)
    tail = cleaned
    while True:
        nxt = tmdb_lib._REGION_TAIL_RE.sub(" ", tail).strip(" -_.()（）[]")
        nxt = re.sub(r"\s+", " ", nxt).strip()
        if not nxt or nxt == tail:
            break
        tail = nxt
    candidate = tail or cleaned or text
    # 双语拼接形如「谍影重重1 The Bourne Identity」或「第一次的离别 A First Farewell」→ 取中文主名
    m = _LEADING_CJK_TITLE_RE.match(candidate)
    if m:
        cjk_head = re.sub(r"\s+", " ", m.group(1)).strip(" .-_:：")
        if contains_cjk(cjk_head) and len(cjk_head) >= 2:
            return cjk_head
    return candidate


def extract_folder_cjk_title(file_path: str | None) -> str:
    """从挂载路径父目录（如 `宿醉3[繁英字幕].The.Hangover.Part.III.2013...`）提取中文片名。"""
    raw = (file_path or "").strip()
    if not raw:
        return ""
    if raw.startswith("mount://") or raw.startswith("byos://"):
        _, _, rel = raw.partition("://")
        _, _, rel = rel.partition("/")
    else:
        rel = raw.replace("\\", "/")
    parent = posixpath.basename(posixpath.dirname("/" + rel.lstrip("/"))).strip()
    if not parent or parent.lower() in _GENERIC_DIRS or _SEASON_DIR_RE.match(parent):
        return ""
    if _COLLECTION_RANGE_RE.search(parent):
        return ""
    stripped = _BRACKET_META_RE.sub(".", parent).strip(" ._")
    if stripped.startswith("[") and "]" in stripped:
        head_in_bracket = stripped[1:stripped.index("]")].strip()
        if contains_cjk(head_in_bracket) and not _COLLECTION_RANGE_RE.search(head_in_bracket):
            return head_in_bracket
    first_seg = stripped.split(".", 1)[0].strip()
    if contains_cjk(first_seg):
        cleaned = clean_pt_title(first_seg)
        if contains_cjk(cleaned):
            return cleaned
    return ""


def preferred_media_title(item: Any) -> str:
    """按「中文名 > TMDB 规范名 > 清洗后标题」返回门户卡片与动态展示标题。"""
    raw_name = str(getattr(item, "name", "") or "").strip()
    aliases_raw = str(getattr(item, "aliases", "") or "")
    aliases = [a.strip() for a in aliases_raw.split(",") if a and a.strip()]

    # 1) aliases 首位的中文名（TMDB details 写入的官方中文译名最干净，无年份/季号噪音）
    for alias in aliases:
        if contains_cjk(alias):
            return clean_pt_title(alias) or alias

    # 2) 条目自身 name 已含中文 → 剥净 PT 尾巴与双语英文尾巴后返回
    if contains_cjk(raw_name):
        return clean_pt_title(raw_name) or raw_name

    # 3) 父目录含明确中文片名（如外语库 `[简繁字幕]` 目录前缀）
    folder_cjk = extract_folder_cjk_title(getattr(item, "file_path", None))
    if folder_cjk:
        return folder_cjk

    # 4) TMDB 官方非中文标题（aliases 首位或 original_title）
    if aliases:
        return clean_pt_title(aliases[0]) or aliases[0]
    orig = str(getattr(item, "original_title", "") or "").strip()
    if orig:
        return clean_pt_title(orig) or orig

    # 5) 兜底：剥除 PT 发布尾巴后的条目名
    return clean_pt_title(raw_name) or raw_name


def clean_feed_display_text(text: str | None, title_map: dict[str, str] | None = None) -> str:
    """对实时动态文案中书名号《...》内的残留 PT 文件名做无损中文反查 + 清洗（兼容历史已落库动态）。"""
    raw = text or ""
    if "《" not in raw or "》" not in raw:
        return raw

    def _repl(m: re.Match[str]) -> str:
        inner = m.group(1).strip()
        if title_map and inner in title_map:
            return f"《{title_map[inner]}》"
        cleaned = clean_pt_title(inner)
        return f"《{cleaned or inner}》"

    return _BOOK_TITLE_RE.sub(_repl, raw)


def build_feed_title_map(db: Any, texts: list[str]) -> dict[str, str]:
    """对一批动态文案里非中文的《标题》做单次批量反查，优先解析为中文名/别名。"""
    candidates: set[str] = set()
    for t in texts:
        if not t or "《" not in t:
            continue
        for m in _BOOK_TITLE_RE.finditer(t):
            inner = m.group(1).strip()
            if inner and not contains_cjk(inner):
                candidates.add(inner)
    if not candidates or db is None:
        return {}
    try:
        from backend.emby_server import models as _em

        rows = (
            db.query(_em.MediaItem)
            .filter(_em.MediaItem.name.in_(list(candidates)))
            .all()
        )
        resolved: dict[str, str] = {}
        for it in rows:
            best = preferred_media_title(it)
            if best and it.name:
                resolved[it.name] = best
        return resolved
    except Exception:  # noqa: BLE001
        return {}


def is_qualified_parcel_item(item: Any) -> bool:
    """今日封存准入判据：必须同时满足「已刮削 + 有海报 + 有中文名」。"""
    status = str(getattr(item, "enrich_status", "") or "").strip().lower()
    if status != "done":
        return False
    source = str(getattr(item, "metadata_source", "") or "").strip().lower()
    has_identity = bool(
        str(getattr(item, "tmdb_id", "") or "").strip()
        or str(getattr(item, "imdb_id", "") or "").strip()
    )
    if source == "none" or (not source and not has_identity):
        return False
    has_poster = bool(
        str(getattr(item, "poster_path", "") or "").strip()
        or str(getattr(item, "primary_image_url", "") or "").strip()
    )
    if not has_poster:
        return False
    title = preferred_media_title(item)
    return contains_cjk(title)
