"""媒体库「有没有来源」判定：CMS 库的 upstream_source_ids 也是来源

真实起因：无界（CMS）库的内容由 CMS 同步工人按 ``upstream_source_ids`` 名单灌入，
paths / mount_ids 按设计就是空的；但可达性判定只认路径与挂载两种老形态，于是
58–66 全部库在管理后台被误报「没有配置任何来源」并挂「待确认」，而库里几十万条
内容播放一切正常。

这里钉住三件事：

- ``upstream_source_ids`` 非空 → 算有来源，不报 ``no_sources``，并带上来源家数；
- 三种形态（paths / mount_ids / upstream_source_ids）全空 → 照常报 ``no_sources``；
- 普通库（本机路径）的口径一字不变。

不碰数据库：判定函数在传入现成 ``Context`` 时不查表，假库行 + 空上下文即可测。
"""
from types import SimpleNamespace

from backend.emby_server import reachability


def _ctx() -> reachability.Context:
    return reachability.Context(
        playback=reachability.PLAYBACK_PANEL,
        playback_label="面板本机（EM 一体化）",
    )


def _lib(**overrides):
    row = {
        "paths": "",
        "mount_ids": "",
        "upstream_source_ids": "",
        "kind": "local",
        "is_virtual": False,
        "node_id": None,
    }
    row.update(overrides)
    return SimpleNamespace(**row)


def _codes(result) -> list:
    return [p["code"] for p in result["problems"]]


def test_cms_library_with_upstream_sources_is_ok():
    """CMS 库只配 upstream_source_ids：不误报没来源（复现 58–66 全库误报）"""
    result = reachability.library_reachability(
        None, _lib(kind="applecms", upstream_source_ids="7,9,10,11"), _ctx())
    assert result["level"] == reachability.LEVEL_OK
    assert "no_sources" not in _codes(result)
    assert result["upstream_sources"] == ["CMS 源 #7", "CMS 源 #9", "CMS 源 #10", "CMS 源 #11"]
    assert "4" in result["message"]


def test_upstream_source_ids_are_parsed_leniently():
    """名单解析与同步工人同一口径：去重、跳过非法片段与非正数"""
    result = reachability.library_reachability(
        None, _lib(kind="applecms", upstream_source_ids="7, 7, abc, 0, 9"), _ctx())
    assert result["level"] == reachability.LEVEL_OK
    assert result["upstream_sources"] == ["CMS 源 #7", "CMS 源 #9"]


def test_library_with_no_sources_at_all_still_warns():
    """三种形态全空：照常报「没有配置任何来源」，不许因为这次修复漏报"""
    result = reachability.library_reachability(None, _lib(), _ctx())
    assert result["level"] == reachability.LEVEL_WARN
    assert result["code"] == "no_sources"


def test_applecms_kind_without_upstream_ids_still_warns():
    """只标了 CMS 类型却没接任何上游源：一样是真没来源，照常报警"""
    result = reachability.library_reachability(
        None, _lib(kind="applecms", upstream_source_ids=""), _ctx())
    assert result["level"] == reachability.LEVEL_WARN
    assert result["code"] == "no_sources"


def test_local_path_library_is_unchanged():
    """普通库填本机路径：老口径不变，面板本机出流时不报任何问题"""
    result = reachability.library_reachability(
        None, _lib(paths="/data/movies"), _ctx())
    assert result["level"] == reachability.LEVEL_OK
    assert result["problems"] == []
    assert result["local_sources"] == ["/data/movies"]


def test_missing_mount_still_reported_bad():
    """挂载引用了不存在的挂载：原有 bad 判定不许被这次修复误伤"""
    result = reachability.library_reachability(
        None, _lib(mount_ids="99"), _ctx())
    assert result["level"] == reachability.LEVEL_BAD
    assert "mount_missing" in _codes(result)
