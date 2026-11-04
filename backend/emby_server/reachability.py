"""播放可达性：把「这个库的内容，出流的那台机器能不能拿到」变成可查证的结论

## 为什么需要它

同一条 ``Library`` 会被**两个进程分别使用**：

- **EM（面板 / 控制面）**：扫描、刮削、目录浏览 —— 按来源枚举文件并写 ``file_path``
- **EA（网关 / 数据面）**：播放出流 —— ``mounts.resolve_play_target`` 按 ``file_path``
  去读文件或换远程直链

而库的来源里存在**主机相对**的东西：

- ``Library.paths`` 里的本机目录（只存在于填它的那台机器上）；
- ``local`` / ``strm`` 类型的 ``StorageMount``（``mounts.MOUNT_TYPE_MAP`` 里 ``kind=local``，
  路径同样是本机的）。

按推荐拓扑分离部署（EM 只跑控制面、EA 出流、媒体来源统一走共享 WebDAV/rclone）时，
这类来源的后果非常隐蔽：**面板扫描完全正常、``item_count`` 照样涨，但客户端一点播放就是
404/502** —— 因为落库的 ``file_path`` 是 EM 上的路径，EA 那台机器根本没有。
这正是「面板扫描正常、播放节点找不到媒体」的根因。

本模块把这件事提前变成后台可见的结论，一条库给一个 ``playback`` 判定：

- ``ok``：出流的那台机器能拿到这条库的内容；
- ``warn``：**无法确认**（没拉过 EA 体检 / 来源是主机相对的本机目录）——提醒去核实，不代表已经坏了；
- ``bad``：**有证据**说明拿不到（EA 体检快照明确报这条挂载不可达）。

判定只用可查证的证据，不靠猜：EA 视角的挂载体检快照（``mount_health.ea_mount_map``）
说某条挂载在 EA 上不可达 → ``bad``；快照里没这条挂载（没拉过体检 / 已停用）→ ``warn``；
库直接填的本机目录任何快照都覆盖不到 → ``warn`` 并给出改法（换成共享挂载），
只有快照明确证明「本机读得到、EA 读不到」时才升级为 ``bad``。

第三种来源形态是 CMS 库的 ``upstream_source_ids``（接入的上游源名单）：内容由 CMS
同步工人按名单灌入、播放走上游线路解析，既不经本机目录也不经挂载出流——名单非空
即算「有来源」，且不参与 EA 挂载体检口径（体检证据只覆盖挂载）。

除了逐库判定，还提供 `client_endpoint_check`：用户客户端**该连哪个地址**是否与
当前出流方式一致（面板已关协议面却仍让用户连面板、地址解析成 localhost 等）。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from backend import models, realms
from backend.emby_server import applecms_library as cms_lib
from backend.emby_server import models as em
from backend.emby_server import mount_health, mounts as mount_lib, nodes

logger = logging.getLogger(__name__)

# 出流方式（与 mount_health.PLAYBACK_NODE_* 对齐，见那里的判定规则）
PLAYBACK_PANEL = "panel"
PLAYBACK_EA = "ea"
PLAYBACK_EXTERNAL = "external"

PLAYBACK_LABELS = {
    PLAYBACK_PANEL: "面板本机（EM 一体化）",
    PLAYBACK_EA: "后端播放节点（EA）",
    PLAYBACK_EXTERNAL: "已有的 Emby 服务器",
}

LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_BAD = "bad"

_LEVEL_ORDER = {LEVEL_OK: 0, LEVEL_WARN: 1, LEVEL_BAD: 2}


def worst_level(levels) -> str:
    """取最严重的一档（面板顶部横幅用）"""
    level = LEVEL_OK
    for item in levels:
        if _LEVEL_ORDER.get(item, 0) > _LEVEL_ORDER[level]:
            level = item
    return level


def gateway_enabled() -> bool:
    """本进程是否提供 Emby 协议面（与 ``backend/main.py`` 同一判定）

    分离部署时 EM 置 ``ENABLE_EMBY_GATEWAY=false``：面板只做控制面，客户端必须连 EA。
    """
    return os.getenv("ENABLE_EMBY_GATEWAY", "true").strip().lower() not in {
        "0", "false", "no", "off",
    }


def playback_mode(db: Session, realm_id: Optional[int] = None) -> str:
    """这个服当前谁在出流（复用挂载体检的判定，避免两处口径漂移）"""
    node = mount_health.playback_node(db, realm_id)
    return {
        mount_health.PLAYBACK_NODE_EA: PLAYBACK_EA,
        mount_health.PLAYBACK_NODE_EXTERNAL: PLAYBACK_EXTERNAL,
    }.get(node, PLAYBACK_PANEL)


def _is_local_source_kind(kind: str) -> bool:
    """这条来源是不是主机相对（本机路径）的

    ``remote``（WebDAV / rclone / 115 / AList）由 EA 自己按配置解析，两台机器都能用；
    ``local`` / ``strm`` 的路径是填它的那台机器上的东西。
    """
    return kind != "remote"


@dataclass
class Context:
    """一次请求内共用的判定上下文（避免逐库重复查表）"""

    realm_id: Optional[int] = None
    playback: str = PLAYBACK_PANEL
    playback_label: str = ""
    self_node_id: Optional[int] = None
    nodes: list = field(default_factory=list)          # 本服的 EA 记录（含停用）
    mounts: dict = field(default_factory=dict)          # 挂载 id → StorageMount
    ea_health: dict = field(default_factory=dict)       # 挂载 id → EA 体检条目
    ea_health_at: Optional[str] = None
    ea_health_ok: bool = False

    def targets_for(self, library) -> list:
        """这条库会由哪些节点出流

        分配了归属节点 → 只算它；未分配 → 本服所有 EA 都能看到它（见
        ``nodes.visible_library_ids``），所以每个都要满足。
        """
        node_id = getattr(library, "node_id", None)
        if node_id:
            return [n for n in self.nodes if n.id == node_id]
        return list(self.nodes)


def build_context(db: Session, realm_id: Optional[int] = None) -> Context:
    """准备判定上下文：出流方式 + 本服 EA + 挂载 + EA 体检快照"""
    if realm_id is None:
        realm_id = realms.active_realm_id(db)
    mode = playback_mode(db, realm_id)
    ctx = Context(realm_id=realm_id, playback=mode, playback_label=PLAYBACK_LABELS.get(mode, mode))
    try:
        ctx.self_node_id = nodes.self_node_id(db)
    except Exception:  # noqa: BLE001 — 身份解析失败不该让检查整体失败
        ctx.self_node_id = None

    if mode == PLAYBACK_EA:
        query = realms.scope_inclusive(db.query(models.RemoteServer), models.RemoteServer.realm_id, realm_id)
        ctx.nodes = (query.filter(models.RemoteServer.kind == "ea")
                     .order_by(models.RemoteServer.id).all())

    mount_rows = realms.scope_inclusive(db.query(em.StorageMount),
                                        em.StorageMount.realm_id, realm_id).all()
    ctx.mounts = {m.id: m for m in mount_rows}

    snapshot = mount_health.read_ea_health(db, realm_id)
    ctx.ea_health = mount_health.ea_mount_map(db, realm_id)
    ctx.ea_health_at = snapshot.get("checked_at")
    ctx.ea_health_ok = bool(snapshot.get("ok"))
    return ctx


def _problem(level: str, code: str, message: str, fix: str = "", source: str = "") -> dict:
    return {"level": level, "code": code, "message": message, "fix": fix, "source": source}


def library_reachability(db: Session, library, ctx: Optional[Context] = None,
                         realm_id: Optional[int] = None) -> dict:
    """一条媒体库的播放可达性判定（面板逐库展示用）"""
    if ctx is None:
        ctx = build_context(db, realm_id)

    local_sources: list[str] = []
    shared_sources: list[str] = []
    problems: list[dict] = []

    for raw in (getattr(library, "paths", "") or "").replace("，", ",").split(","):
        path = raw.strip()
        if path:
            local_sources.append(path)

    for mount_id in mount_lib.parse_mount_ids(library):
        mount = ctx.mounts.get(mount_id)
        if mount is None:
            problems.append(_problem(
                LEVEL_BAD, "mount_missing",
                f"挂载 #{mount_id} 不存在（可能已被删除）",
                "在「存储来源」页里重新选一条挂载，或删掉这条库里的引用",
                source=f"#{mount_id}",
            ))
            continue
        label = mount_lib.mount_label(mount)
        if not mount.is_enabled:
            problems.append(_problem(
                LEVEL_BAD, "mount_disabled",
                f"来源「{label}」已停用：扫描会跳过它，客户端也不会有内容",
                "在「存储来源」页启用这条挂载，或把它从库里移除",
                source=label,
            ))
            continue
        kind = (mount_lib.MOUNT_TYPE_MAP.get(mount.mount_type, {}) or {}).get("kind", "local")
        if _is_local_source_kind(kind):
            local_sources.append(label)
        else:
            shared_sources.append(label)

    # CMS 库的来源形态：接入的上游源名单。内容由 CMS 同步工人按名单灌入、播放走
    # 上游线路解析，不经本机目录 / 挂载出流；解析口径与同步工人共用一份实现。
    upstream_ids = cms_lib.parse_upstream_source_ids(library)
    upstream_sources = [f"CMS 源 #{sid}" for sid in upstream_ids]

    is_virtual = bool(getattr(library, "is_virtual", False))
    if is_virtual:
        return {
            "level": LEVEL_OK, "code": "", "message": "虚拟库：内容来自其它媒体库，可达性随来源库",
            "fix": "", "playback": ctx.playback, "playback_label": ctx.playback_label,
            "targets": [], "local_sources": [], "shared_sources": [],
            "upstream_sources": upstream_sources,
            "problems": [], "checked_at": ctx.ea_health_at,
        }

    # ---- 出流的机器是不是「本机」----
    targets = ctx.targets_for(library)
    remote_targets = [t for t in targets if t.id != ctx.self_node_id]
    target_names = [t.name for t in remote_targets] or [t.name for t in targets]

    def node_hint() -> str:
        if len(target_names) == 1:
            return f"节点「{target_names[0]}」"
        if target_names:
            return "节点 " + "、".join(f"「{n}」" for n in target_names)
        return "播放节点"

    # EA 已并入本容器（AETRIX_ROLE=all 时 API/worker/EA 同容器），此时本机路径 EA 直接可读，
    # 不应再按"另一台机器"报待确认。只有明确配置了远端目标时才算异机。
    # 注意：self_node_id 为 None 时无法区分远近，同容器模式下一律按本机处理。
    import os as _os
    _ea_colocated = _os.environ.get("AETRIX_ROLE", "") == "all"
    _has_real_remote = bool(remote_targets) and not (
        _ea_colocated and ctx.self_node_id is None
    )
    on_other_machine = ctx.playback in (PLAYBACK_EA, PLAYBACK_EXTERNAL) and (
        ctx.playback == PLAYBACK_EXTERNAL or _has_real_remote
        or (not targets and not _ea_colocated)
    )

    if not local_sources and not shared_sources and not upstream_ids:
        problems.append(_problem(
            LEVEL_WARN, "no_sources",
            "这条库没有配置任何来源（路径、挂载或上游源）",
            "在「媒体库」里补一条来源，否则扫描与播放都没有内容",
        ))
    elif not local_sources and not on_other_machine:
        # 共享来源 + 内容就在本机：播放没问题
        pass
    elif local_sources and on_other_machine:
        # 主机相对来源 + 另一台机器出流：能不能播取决于那台机器上有没有同样的路径。
        # 有 EA 体检证据时按证据判，没有就只报「无法确认」。
        for source in local_sources:
            mount = _mount_for_source(ctx, library, source)
            if mount is not None:
                item = ctx.ea_health.get(mount.id)
                if item is not None and item.get("ok") is False:
                    problems.append(_problem(
                        LEVEL_BAD, "mount_unreachable_on_node",
                        f"「{source}」在{node_hint()}上读不到："
                        f"{(item.get('message') or '探测失败')[:120]}",
                        "把这条来源换成共享挂载（WebDAV / rclone / 115 / AList），"
                        "让两台机器读同一份存储；或把库的「归属节点」改成由本机出流",
                        source=source,
                    ))
                    continue
                if item is None or item.get("ok") is None:
                    problems.append(_problem(
                        LEVEL_WARN, "host_local_unchecked",
                        f"「{source}」是本机路径类型的来源，还没在{node_hint()}上体检过，"
                        "无法确认那边也有同样的路径",
                        "在「存储来源」页点「拉取 EA 体检」核实；换成共享挂载最稳",
                        source=source,
                    ))
                    continue
                # 体检明确说 EA 能读到（如两端挂了同一份 NFS）→ 按共享来源看待
                shared_sources.append(source)
                continue
            problems.append(_problem(
                LEVEL_WARN, "local_path_unchecked",
                f"来源「{source}」填的是本机目录：面板扫描没问题，"
                f"但{node_hint()}上如果没有同样的路径，客户端会看得到条目却播不了",
                "把来源换成共享挂载（WebDAV / rclone / 115 / AList），"
                "或确认两台机器挂载的是同一份存储",
                source=source,
            ))

    # 共享来源（WebDAV/rclone/115/AList）：两台机器读同一份存储，但「配得上」不代表
    # 出流节点真的连得通 —— 有 EA 体检证据才敢说 ok
    if on_other_machine and shared_sources and ctx.playback == PLAYBACK_EA:
        for source in shared_sources:
            mount = _mount_for_source(ctx, library, source)
            if mount is None:
                continue
            item = ctx.ea_health.get(mount.id)
            if item is not None and item.get("ok") is False:
                problems.append(_problem(
                    LEVEL_BAD, "mount_unreachable_on_node",
                    f"「{source}」在{node_hint()}上读不到：{(item.get('message') or '探测失败')[:120]}",
                    "在「存储来源」页点「拉取 EA 体检」看具体报错（地址 / 账号 / 权限），再修好这条挂载",
                    source=source,
                ))
            elif item is None or item.get("ok") is None:
                problems.append(_problem(
                    LEVEL_WARN, "mount_unchecked",
                    f"「{source}」还没在{node_hint()}上体检过，无法确认那边能读到这份存储",
                    "在「存储来源」页点「拉取 EA 体检」核实（EA 与面板的地址、账号必须都填对）",
                    source=source,
                ))

    if ctx.playback == PLAYBACK_EXTERNAL and local_sources:
        problems.append(_problem(
            LEVEL_WARN, "external_emby_local_path",
            "当前入口是已有的 Emby 服务器：它按自己的媒体库出流，看不到这台机器的本机目录",
            "这台机器的路径只对「面板自己出流」有意义；已有 Emby 请用它自己的库",
        ))

    level = worst_level(p["level"] for p in problems) if problems else LEVEL_OK
    primary = next((p for p in problems if p["level"] == LEVEL_BAD), None) or (problems[0] if problems else None)
    if primary:
        ok_message = primary["message"]
    elif upstream_ids and not local_sources and not shared_sources:
        # 纯 CMS 供源：内容不在出流节点的文件系统上，默认那句「节点能拿到」不准确
        ok_message = f"内容由 CMS 同步供源：已接入 {len(upstream_ids)} 家上游来源，播放走上游线路"
    else:
        ok_message = "出流节点能拿到这条库的内容"
    return {
        "level": level,
        "code": primary["code"] if primary else "",
        "message": ok_message,
        "fix": primary["fix"] if primary else "",
        "playback": ctx.playback,
        "playback_label": ctx.playback_label,
        "targets": target_names,
        "local_sources": local_sources,
        "shared_sources": shared_sources,
        "upstream_sources": upstream_sources,
        "problems": problems,
        "checked_at": ctx.ea_health_at,
    }


def _mount_for_source(ctx: Context, library, label: str):
    """把来源标签还原成挂载行（本机路径来源没有挂载行 → None）

    支持 ``mount://<id>/<子目录>`` 格式：直接解析挂载 ID。
    """
    # mount:// 格式：直接解析 ID
    parsed = mount_lib.parse_mount_path(label)
    if parsed is not None:
        mount = ctx.mounts.get(parsed[0])
        if mount is not None:
            return mount
    # 旧格式：按展示名匹配
    for mount_id in mount_lib.parse_mount_ids(library):
        mount = ctx.mounts.get(mount_id)
        if mount is not None and mount_lib.mount_label(mount) == label:
            return mount
    return None


def unreachable_libraries(db: Session, realm_id: Optional[int] = None,
                          include_ok: bool = False) -> list[dict]:
    """逐库判定，默认只回有问题的那些（后台横幅与巡检用）"""
    ctx = build_context(db, realm_id)
    out: list[dict] = []
    query = realms.scope_inclusive(db.query(em.Library), em.Library.realm_id, ctx.realm_id)
    for lib in query.order_by(em.Library.id).all():
        if getattr(lib, "is_enabled", True) is False:
            continue
        item = library_reachability(db, lib, ctx)
        if include_ok or item["level"] != LEVEL_OK:
            item.update({"library_id": lib.id, "library_name": lib.name})
            out.append(item)
    out.sort(key=lambda i: -_LEVEL_ORDER.get(i["level"], 0))
    return out


def client_endpoint_check(db: Session, realm_id: Optional[int] = None,
                          ctx: Optional[Context] = None) -> dict:
    """用户客户端该连的地址，与当前出流方式是否自洽

    分离部署最容易踩的两个坑都在这里：面板已关协议面却仍把入口写成面板地址
    （客户端会拿到 404 指引页），以及地址解析成 ``localhost``（只有本机能连）。

    **什么都没配时不再报红**（v2.32.0）：以前 ``resolve_emby_base_url`` 会回退到写死的
    ``http://localhost:8000``，于是一台刚装好的服务器（还没配任何入口地址）第一眼就是一条红证据：
    「用户端账号卡显示的地址是 http://localhost:8000：只有这台机器自己能连」——那个地址从来
    不是配置，只是占位。现在这种情况报 ``url_source = none`` 且 ``url`` 留空，面板按管理员
    **当前访问用的地址**显示（页面与接口同源，所以它就是用户该连的那个地址）；
    只有真的把 localhost 填进配置 / 环境变量里（人写的）才报红。
    """
    from backend.emby_server import portal  # 延迟导入：portal 会反向用本模块，避免循环

    if realm_id is None:
        realm_id = realms.active_realm_id(db)
    if ctx is None:
        ctx = build_context(db, realm_id)

    url = ""
    source = portal.URL_SOURCE_NONE
    try:
        url, source = portal.resolve_emby_base_url_with_source(db, realm_id)
        url = url or ""
    except Exception as exc:  # noqa: BLE001 — 地址解析失败按未配置处理
        logger.warning("解析用户端地址失败：%s", exc)
        url, source = "", portal.URL_SOURCE_NONE
    if source == portal.URL_SOURCE_NONE:
        url = ""          # 占位地址不是配置：面板按当前访问地址显示，不拿它当证据
    gateway = gateway_enabled()
    problems: list[dict] = []

    if ctx.playback == PLAYBACK_PANEL and not gateway:
        problems.append(_problem(
            LEVEL_BAD, "gateway_off_but_panel_entry",
            "面板已关闭协议面（ENABLE_EMBY_GATEWAY=false），但媒体入口仍是「面板自己出流」："
            "客户端会连到面板并拿到 404 指引页",
            "在「服务器」页把入口改成后端节点（EA）或已有 Emby",
        ))
    if url and _is_local_only(url):
        problems.append(_problem(
            LEVEL_WARN if ctx.playback != PLAYBACK_PANEL else LEVEL_BAD, "client_url_localhost",
            f"用户端账号卡显示的地址是 {url}：只有这台机器自己能连",
            "填这台出流机器的对外地址（如 https://emby.example.com）",
        ))
    if ctx.playback == PLAYBACK_EA and gateway and not url:
        problems.append(_problem(
            LEVEL_WARN, "ea_entry_missing_url",
            "入口是后端节点（EA），但还没填它的对外地址",
            "在「服务器」页填 EA 的地址与密钥，用户端才会显示正确地址",
        ))
    if ctx.playback == PLAYBACK_EA and gateway and url:
        problems.append(_problem(
            LEVEL_WARN, "panel_gateway_still_on",
            "面板自己还开着协议面（ENABLE_EMBY_GATEWAY=true），同时又配了 EA 入口："
            "两个地址都能访问，排查时容易看错是哪套在出流",
            "分离部署建议把 EM 的 ENABLE_EMBY_GATEWAY 设为 false，只保留 EA 一个入口",
        ))

    level = worst_level(p["level"] for p in problems) if problems else LEVEL_OK
    primary = next((p for p in problems if p["level"] == LEVEL_BAD), None) or (problems[0] if problems else None)
    return {
        "level": level,
        "code": primary["code"] if primary else "",
        "message": primary["message"] if primary else "用户端地址与当前出流方式一致",
        "fix": primary["fix"] if primary else "",
        "url": url,
        # 地址是从哪来的：面板要能分清「配好的」与「按当前访问地址推的/没配」（见上面 docstring）
        "url_source": source,
        "gateway_enabled": gateway,
        "playback": ctx.playback,
        "playback_label": ctx.playback_label,
        "problems": problems,
    }


def _is_local_only(url: str) -> bool:
    """这个地址是不是「只有这台机器自己能连」（环回 / 未指定地址）

    IPv6 字面量是带方括号的（``http://[::1]:8000``）：按 ``:`` 切会把主机名切成 ``[``，
    于是环回地址被当成正常地址、这条提示永远不报——先剥括号再比。
    """
    text = (url or "").strip().lower()
    if not text:
        return False
    host = text.split("//", 1)[-1].split("/", 1)[0]
    host = host.split("]", 1)[0].lstrip("[") if host.startswith("[") else host.split(":", 1)[0]
    return host in {"localhost", "127.0.0.1", "0.0.0.0", "::1", "::"} or text.startswith("http://localhost")


def summary(db: Session, realm_id: Optional[int] = None) -> dict:
    """完整检查结果：出流方式 + 逐库判定 + 用户端地址一致性（后台一个接口拿全）"""
    ctx = build_context(db, realm_id)
    realm = realms.get_realm(db, ctx.realm_id) if ctx.realm_id else None
    libraries = unreachable_libraries(db, ctx.realm_id, include_ok=True)
    client = client_endpoint_check(db, ctx.realm_id, ctx)
    counts = {LEVEL_OK: 0, LEVEL_WARN: 0, LEVEL_BAD: 0}
    for item in libraries:
        counts[item["level"]] = counts.get(item["level"], 0) + 1
    return {
        "realm_id": ctx.realm_id,
        "realm_name": realm.name if realm else "全部服",
        "playback": {
            "mode": ctx.playback,
            "label": ctx.playback_label,
            "gateway_enabled": client["gateway_enabled"],
            "targets": [{"id": n.id, "name": n.name, "enabled": bool(n.is_enabled),
                         "url": n.url, "online": n.last_check_ok}
                        for n in ctx.nodes],
            "client_url": client["url"],
            # realm / config / env（都是人配的）或 none（没配，面板按当前访问地址显示）
            "client_url_source": client["url_source"],
        },
        "ea_health_at": ctx.ea_health_at,
        "ea_health_ok": ctx.ea_health_ok,
        "counts": {"libraries": len(libraries), **counts},
        "level": worst_level([client["level"], *[i["level"] for i in libraries]]),
        "client_endpoint": client,
        "libraries": libraries,
    }


__all__ = [
    "Context",
    "LEVEL_BAD",
    "LEVEL_OK",
    "LEVEL_WARN",
    "PLAYBACK_EA",
    "PLAYBACK_EXTERNAL",
    "PLAYBACK_LABELS",
    "PLAYBACK_PANEL",
    "build_context",
    "client_endpoint_check",
    "gateway_enabled",
    "library_reachability",
    "playback_mode",
    "summary",
    "unreachable_libraries",
    "worst_level",
]
