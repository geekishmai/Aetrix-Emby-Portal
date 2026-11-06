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
_ROMAN_RE = re.compile(r"^(?:I{1,3}|IV|VI{0,3}|IX|XI{0,2}|[Ⅰ-Ⅻ])$")
_PT_DISPLAY_JUNK_RE = re.compile(
    r"\b(?:2160p|1440p|1080[ip]|720p|480p|4k|8k|uhd|hdr10\+?|hdr|sdr|dv|dolby|hevc|avc|"
    r"[hx][.\s]?26[45]|blu[\s\-]?ray|bluray|remux|web[\s\-]?dl|webrip|bdrip|brrip|hdtv|dvdrip|bdmv|"
    r"truehd(?:[.\s]*\d[.\s]\d)?|dts(?:[-\s.]?hd(?:[.\s]?ma|[.\s]?hra?)?)?(?:[.\s]?\d[.\s]\d)?|"
    r"dtshd(?:ma)?(?:[.\s]?\d[.\s]\d)?|hdma(?:[.\s]?\d[.\s]\d)?|hd\s+ma(?:\s+\d[.\s]\d)?|"
    r"ddp?[.\s]?\d[.\s]\d|(?:eac3|ac3|aac|flac)(?:[.\s]?\d[.\s]\d)?|atmos|(?:8|10|12)\s*bit|"
    r"(?:[2-9]|multi|dual|2ual)audios?|pgs|repack|proper|complete|minihd|mnhd|beitai|chdweb|chdbits|"
    r"tagweb|taghd|momoweb|momohd|ctrlweb|ctrlhd|enthd|bathd|homeweb|homehd|hdbweb|frds|adweb|ourtv|harestv|hhweb|mweb|ttg|2d3d)\b",
    re.IGNORECASE,
)
_CASE_SENSITIVE_GROUP_RE = re.compile(r"(?:\b[A-Za-z0-9_]+@[A-Za-z0-9_]+\b|\bbeAst\b)")
_TAIL_SHORT_TAG_RE = re.compile(
    r"(?:\s+|[-._])[\[(（【]?\s*(?:hktv|hk|tw|us|gb|kr|jp|cn|gl|cee|dc|alt|chd|xiaomi|ma|hd)\s*[\])）】]?\s*$",
    re.IGNORECASE,
)
# 双语标题切分：中文主名（允许末尾跟罗马数字续集号如 II/III/IV 或 2.0 版本号） + 纯英文副名直到句末（句末绝不含中文）
_BILINGUAL_SPLIT_RE = re.compile(
    r"^("
    r"[\u4e00-\u9fff0-9][\u4e00-\u9fff0-9A-Za-z\u2160-\u216b：:·・，,！!？?—\-（）().\s]{0,60}?"
    r"[\u4e00-\u9fff0-9）)\u2160-\u216b]"
    r"(?:\s+(?:I{2,3}|IV|VI{0,3}|IX|XI{0,2}))?"
    r")"
    r"\s+([A-Za-z][A-Za-z0-9'’:,.\-\s]*)$"
)


def contains_cjk(text: str | None) -> bool:
    return bool(text and _CJK_RE.search(text))


def _is_english_translation_tail(tail: str) -> bool:
    """判定末尾拉丁串是否为双语附加英文原名（排除单独罗马数字如 II/III）。"""
    t = tail.strip(" .-_:：()[]")
    if not t or contains_cjk(t) or _ROMAN_RE.match(t):
        return False
    # 去掉末尾纯年份后再看英文词数/长度
    t_no_year = re.sub(r"\b(?:19|20)\d{2}\b", " ", t).strip()
    words = [w for w in re.findall(r"[A-Za-z0-9]+", t_no_year) if not _ROMAN_RE.match(w)]
    alpha_words = [w for w in words if re.search(r"[A-Za-z]", w)]
    if len(alpha_words) >= 2:
        return True
    if len(alpha_words) == 1 and len(alpha_words[0]) >= 4:
        return True
    return False


def clean_pt_title(raw: str | None) -> str:
    """剥除标题末尾残留的 PT 压制参数/官组名；保留中文标点、罗马数字续集号与 2.0 版本号。"""
    text = (raw or "").strip()
    if not text:
        return ""
    s = _BRACKET_META_RE.sub(" ", text)
    s = _CASE_SENSITIVE_GROUP_RE.sub(" ", s)
    s = _PT_DISPLAY_JUNK_RE.sub(" ", s)
    # 仅保护片名版本号小数点（如 梅根2.0 / 哥斯拉-1.0 / 0.0兆赫），把 .2025 / .M3GAN 等文件名分隔符转空格
    s = re.sub(r"(?<!\d)\.|(?<=\d)\.(?!\d(?!\d))|[_+]+", " ", s)
    s = re.sub(r"(?<![\d.])\b(?:1\s0|2\s0|5\s1|7\s1)\b(?![\d.])", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .-_")
    while True:
        nxt = _TAIL_SHORT_TAG_RE.sub("", s).strip(" .-_")
        nxt = re.sub(r"\s+", " ", nxt).strip()
        if not nxt or nxt == s:
            break
        s = nxt
    # 若末尾跟了压制文件名年份（如 Eight and a Half 1963 / 梅根2.0 M3GAN 2.0 2025），在纯英文发布串末尾剥除年份
    candidate = s or text
    m = _BILINGUAL_SPLIT_RE.match(candidate)
    if m and contains_cjk(m.group(1)) and _is_english_translation_tail(m.group(2)):
        cjk_head = re.sub(r"\s+", " ", m.group(1)).strip(" .-_:：")
        if contains_cjk(cjk_head) and len(cjk_head) >= 2:
            return cjk_head
    # 纯英文 PT 发布名（原串含句点分隔或压制词）剥除末尾 4 位年份
    if not contains_cjk(candidate) and ("." in text or candidate != text):
        no_yr = re.sub(r"\s+(?:19|20)\d{2}$", "", candidate).strip(" -_.")
        if no_yr and re.search(r"[A-Za-z]", no_yr):
            return no_yr
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
