"""能力：站点与品牌

站点名称、Logo、主题色与 SEO。这一项没有外部依赖，但同样属于「管理员自己填」——
用户端顶栏站名、登录页标题、浏览器标题与 meta 信息都由它驱动，换站点皮肤不用改代码。

两处输入做了**格式校验**，因为这两个值会进到页面里：

- ``site_theme_color``：必须是十六进制色（``#rgb`` / ``#rrggbb`` / ``#rrggbbaa``），否则回落默认色。
  它会被写进 CSS 自定义属性，不校验等于把一个可以写任意 CSS 的注入点交给后台表单；
- ``site_logo_url``：只接受 ``http(s)://`` 或站内相对路径（``/...``），其它协议（``javascript:``、
  ``data:``）一律忽略并回落成内置图标。

没有 ``test()``：这里没有可测的外部依赖，后台因此不会显示「测试」按钮（不显示一个点了只会说
「不支持测试」的按钮）。
"""
from __future__ import annotations

import re
import os

from backend.emby_server import media_sets as ms
from backend.emby_server.env_util import env_int

from sqlalchemy.orm import Session

from backend.integrations import store

DEFAULT_SITE_NAME = "Aetrix"
DEFAULT_THEME_COLOR = "#22d3ee"

SPEC = {
    "title": "站点与品牌",
    "desc": "站点名称、Logo、主题色、SEO 与游戏化组件开关；用户端顶栏、登录页、导航与首页随之变化。",
    "group": "站点与品牌",
    "docs_hint": "主题色填十六进制（如 #22d3ee）；Logo 留空使用内置图标；留空项回落到默认值。",
    "fields": [
        {"key": "site_name", "label": "站点名称", "type": "str", "default": DEFAULT_SITE_NAME,
         "required": True, "placeholder": DEFAULT_SITE_NAME},
        {"key": "site_logo_url", "label": "Logo 图片地址", "type": "str", "default": "",
         "placeholder": "https://cdn.example.com/logo.png 或 /logo.png",
         "hint": "留空则显示内置图标"},
        {"key": "site_theme_color", "label": "主题色", "type": "str", "default": DEFAULT_THEME_COLOR,
         "placeholder": DEFAULT_THEME_COLOR, "hint": "十六进制颜色，例如 #22d3ee"},
        {"key": "site_seo_title", "label": "SEO 标题", "type": "str", "default": "",
         "hint": "留空则用「站点名称 - 影音媒体服务平台」"},
        {"key": "site_seo_description", "label": "SEO 描述", "type": "str", "default": ""},
        {"key": "site_seo_keywords", "label": "SEO 关键词", "type": "str", "default": "",
         "hint": "逗号分隔"},
        {"key": "site_gamification_enabled", "label": "显示游戏化与世界组件", "type": "bool", "default": "true",
         "hint": "关闭后用户端隐藏能量、体力、每日一封、抽一签、世界、到此一游、收藏家、观影碑，以及导航栏的补给站与世界入口（默认开启）"},
    ],
}

_FIELD_KEYS = [f["key"] for f in SPEC["fields"]]
_FIELD_DEFAULTS = {f["key"]: (f.get("default") or "") for f in SPEC["fields"]}

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def read(db: Session) -> dict:
    """一次查询取回品牌配置"""
    return store.read_values(db, _FIELD_KEYS, _FIELD_DEFAULTS)


def theme_color(values: dict) -> str:
    """校验过的主题色（非法值回落默认色）"""
    raw = str(values.get("site_theme_color") or "").strip()
    return raw if _HEX_RE.match(raw) else DEFAULT_THEME_COLOR


def logo_url(values: dict) -> str:
    """校验过的 Logo 地址（非 http(s) / 站内相对路径一律回落到空 = 用内置图标）"""
    raw = str(values.get("site_logo_url") or "").strip()
    if not raw:
        return ""
    if raw.startswith("/"):
        return raw
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return ""



def gamification_enabled(values: dict) -> bool:
    """是否显示用户端游戏化与世界组件（默认开启，保持上游行为）"""
    raw = values.get("site_gamification_enabled")
    if isinstance(raw, bool):
        return raw
    text = str(raw if raw is not None else "true").strip().lower()
    return text not in {"0", "false", "no", "off"}

def public_info(db: Session) -> dict:
    """给前端的公开信息（只含能公开的字段，没有任何凭据）"""
    values = read(db)
    name = str(values.get("site_name") or "").strip() or DEFAULT_SITE_NAME
    series_follow_on = (os.getenv("SERIES_FOLLOW_ENABLED") or "0").strip().lower() in {
        "1", "true", "yes", "on",
    }
    return {
        "site_name": name,
        "logo_url": logo_url(values),
        "theme_color": theme_color(values),
        "seo_title": str(values.get("site_seo_title") or "").strip() or f"{name} - 影音媒体服务平台",
        "seo_description": str(values.get("site_seo_description") or "").strip(),
        "seo_keywords": str(values.get("site_seo_keywords") or "").strip(),
        "gamification_enabled": gamification_enabled(values),
        "collections_enabled": bool(ms.collections_enabled()),
        "home_sections_enabled": bool(env_int("HOME_SECTIONS_PROJECTION_ENABLED", 0, 0, 1)),
        "series_follow_enabled": series_follow_on,
    }


def is_configured(values: dict) -> bool:
    """站点名是必填项，有名字就算配过（其余项留空即用默认）"""
    return bool(str(values.get("site_name") or "").strip())
