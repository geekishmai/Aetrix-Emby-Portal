"""115 网盘账号与直挂客户端

面板侧只保留「用 115 直挂媒体库」所需的两件事：

- **账号配置档**：可以配置多个命名账号（`pan115_accounts`），其中一个为默认账号；
  媒体库可单独绑定账号（`Library.account_115_id`），未绑定时回退「默认账号 → 服务器级
  `PAN115_COOKIE`」，因此历史部署里只在 .env 放一个 Cookie 的写法继续可用。
- **Cookie 型客户端**：校验 Cookie、列目录、取下载地址（直挂播放由 EA 用它换直链）。

v2.18.0 起不再包含「分享链接转存 / 下载任务」那一套（解析分享、任务状态机、断点续跑、
`PAN115_STATE_DIR` 落盘全部移除）：把分享搬进自己账号这件事在 115 官方 App / 网页里
一次即可完成，面板长期维护一套抓第三方页面的任务引擎不值得。115 直挂本身不走那套代码。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from base64 import b64encode
from email.utils import formatdate
import xml.etree.ElementTree as ET
from binascii import b2a_hex, crc32
from typing import Optional
from urllib.parse import urlencode

from sqlalchemy.orm import Session

from backend.emby_server import models as em
from backend.emby_server.env_util import env_float
from backend.emby_server.upstream_sources import SealedSecretError, seal_secret, unseal_secret

logger = logging.getLogger(__name__)

# ==================== 配置 ====================

PAN115_COOKIE_ENV = "PAN115_COOKIE"
# 端点可用环境变量覆盖：115 的 Web API 域名偶有调整，也便于测试指向本地假服务
PAN115_WEBAPI_BASE = os.getenv("PAN115_WEBAPI_BASE", "https://webapi.115.com").rstrip("/")
# 上传域名（与 Web API 分离，见 115cli 0.3.4 的 Endpoint.UPLB）
PAN115_UPLB_BASE = os.getenv("PAN115_UPLB_BASE", "https://uplb.115.com").rstrip("/")
PAN115_TIMEOUT = env_float("PAN115_TIMEOUT", 20.0)
PAN115_UA = os.getenv(
    "PAN115_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36",
)

# ==================== 秒传（initupload）协议 ====================
# 协议对照 p115cipher 0.0.6、p115client 的 upload_init/upload_file_init
# 与 py115 的 UploadInitApi（三方实现公式互洽，2026-10-30 对照确认）：
# - 先 GET proapi /app/uploadinfo 取 user_id + userkey；
# - POST uplb /4.0/initupload.php：query 带 k_ec（ECDH token），body
#   为 AES-128-CBC（固定 ECDH 派生 key/iv，PKCS7 填充）加密的
#   urlencode 表单（空值字段不进表单）；
# - sig = SHA1(userkey + hex(SHA1("{userid}{fileid}{target}0"))
#   + "000000")，大写；token = MD5(MD5_SALT + fileid + filesize
#   + sign_key + sign_val + userid + t + hex(MD5(userid))
#   + appversion)，小写；
# - 响应同为 AES 加密，解密后是分段 lz4 帧（2 字节小端块长 + lz4
#   block）包着的 JSON；status：2 = 秒传命中（reuse，文件已落用户
#   盘）、7 = 需 sign_check 二轮校验（按 sign_check 区间算 sha1
#   回填 sign_val 重发）、1 = 未命中（需走 OSS 上传）。
# 秒传只做到「命中判定」为止；未命中（status=1）的 OSS 落盘由
# 本模块的 multipart_upload 接完（见下「分片上传」节），调用方
# 按大小在 simple_upload（≤5 GiB）与 multipart_upload（>5 GiB）
# 之间路由。**真实端到端未实测**（不许用生产用户 Cookie 做
# 开发测试），协议正确性靠上述开源实现对照 + mock。

PAN115_PROAPI_BASE = os.getenv(
    "PAN115_PROAPI_BASE", "https://proapi.115.com").rstrip("/")
PAN115_APP_VERSION = "36.2.28"  # p115client 当前 appversion
#: initupload 是 app 端点，服务端按 UA 里的 app 版本号放行：浏览器
#: UA 没有版本标记，会回 status=4 / statuscode=403「请升级到最新
#: 版本」（2026-10-31 活体实证：同一 appversion 字段、换 app UA
#: 即通）。形态与 p115client 的 upload_init 一致，版本号跟随
#: PAN115_APP_VERSION，升版本时两处一起走。
PAN115_APP_UA = (
    f"Mozilla/5.0 115disk/{PAN115_APP_VERSION} "
    f"115Browser/{PAN115_APP_VERSION} "
    f"115wangpan_android/{PAN115_APP_VERSION}")

_P115_MD5_SALT = b"Qclm8MGWUv59TnrR0XPg"
_P115_CRC_SALT = b"^j>WD3Kr?J2gLFjD4W2y@"
_P115_AES_PUBKEY = (
    b"\x1d\x03\x0e\x80\xa1x\xdc\xee\xce\xcd\xa3w\xde\x12"
    b"\x8d\x8e\xd9\xdd\xcfU\xaea\xedF\xea\x12\x1a\x1c\xfc\x81")
_P115_AES_KEY = b"\xfb\x1a\x19\xd6R\xf5\xaa\xf7\xbce\x1d\x0fi\xbfB/"
_P115_AES_IV = b"i\xbfB/I\x96\x05P\xa0\xadD\xec4F\xcbL"


# ==================== 分片上传（initupload + 阿里云 OSS 多段） ====================
# 协议对照 p115oss 0.1.2（upload.py 的 upload_file_init /
# upload_file、oss.py 的 oss_sign / oss_multipart_*）与 py115
# 0.1.1（UploadInitApi / UploadTokenApi / Cloud.request_upload_stream），
# 2026-10-30 对照确认，步骤：
# 1. 与秒传同一 initupload 初始化（含 sign_check 二轮）：status=2
#    存量命中；status=1 返回 bucket / object / callback{callback,
#    callback_var} / pickcode（#1526 秒传在此止步，本节接第 2 步）；
# 2. GET uplb /3.0/gettoken.php（Cookie 鉴权）取 OSS STS 临时凭据
#    {AccessKeyId, AccessKeySecret, SecurityToken, Expiration}；
# 3. OSS InitiateMultipartUpload：POST {bucket}.
#    oss-cn-shenzhen.aliyuncs.com/{object}?sequential=1&uploads=1，
#    HMAC-SHA1 头签名（见 _oss_auth_headers），XML 响应取 UploadId；
# 4. 逐片 UploadPart：PUT ?partNumber=N&uploadId=…，串行；每片
#    校验响应 ETag == 本片 MD5（大写 hex），单片有限重试；
# 5. CompleteMultipartUpload：POST ?uploadId=…，body 为 parts 的
#    XML 清单，带 x-oss-callback / x-oss-callback-var（init 回的
#    callback 两段 base64；callbackBody 里的 ${sha1} 占位先按
#    py115 口径替换为整文件 sha1）触发 115 回调落盘，响应体即
#    回调 JSON（与 simple_upload 回调同形态）；
# 6. 任一步失败 best-effort AbortMultipartUpload（DELETE
#    ?uploadId=…）再抛错——未 complete 前不声称成功、不登记。
# 7. 令牌口径（2026-10-31 换代第一刀 step 1）：STS 令牌寿命有
#    限、长上传中途会过期（生产 part 188 实证：同令牌三试全败），
#    但会话（UploadId）不随令牌死——part PUT / complete 撞
#    SecurityTokenExpired 时先刷新令牌再重试该步（每片至多 1
#    次＋整单总闸），不再拿过期令牌空重试；Expiration 领取时记
#    日志并进会话状态，先把真实寿命测出来（此前被 upload_token
#    丢弃、从未有数）。
# **真实端到端未实测**（与秒传同状态：不许用生产用户 Cookie 做
# 开发测试），协议正确性靠上述开源实现对照 + mock，上线后靠
# 真实大文件 job 观察。

#: OSS 端点（p115oss DEFAULT_ENDPOINT）；可用环境变量覆盖（同
#: PAN115_UPLB_BASE 口径，便于测试指向本地假服务）。
PAN115_OSS_ENDPOINT = os.getenv(
    "PAN115_OSS_ENDPOINT", "http://oss-cn-shenzhen.aliyuncs.com").rstrip("/")
#: init 响应缺 bucket 时的缺省桶（p115oss DEFAULT_BUCKET）。
PAN115_OSS_DEFAULT_BUCKET = "fhnfile"
#: 分片大小下限：p115oss determine_partsize 的算法下限是 100 KiB，
#: 但 p115client 明确分片至少 10 MB，且分片过小徒增请求数——
#: 取 10 MiB 为下限，倍增算法与 determine_partsize 同源。
_MULTIPART_MIN_PART_SIZE = 10 * 1024 * 1024
#: 分片数上限（OSS 协议上限，determine_partsize 同值）。
_MULTIPART_MAX_PARTS = 10000
#: 单片 PUT 有限重试次数（与搬运层有界重试同口径；整单最终成败
#: 仍由 job 层有界重试兜底）。
_MULTIPART_PART_ATTEMPTS = 3
#: 片间令牌刷新的整单总闸（单次 multipart_upload 调用内）：每片
#: 至多刷新 1 次由 _oss_upload_part_retry 钉死，这里再防 gettoken
#: 异常形态下跨片无界领令牌。STS 真实寿命未实测，长上传正当需
#: 要多次刷新，故总闸取得宽，只作兜底不作节流。
_MULTIPART_TOKEN_REFRESH_MAX = 64


#: 进 OSS 签名 CanonicalizedResource 的子资源键（p115oss
#: SUBRESOURCE_KEYS 中与分片上传相关者）。
_OSS_SUBRESOURCE_KEYS = frozenset({
    "uploads", "uploadId", "partNumber", "sequential",
})


def _determine_partsize(length, *, min_partsize=None, max_parts=None):
    """分片大小（p115oss determine_partsize 移植）：自下限倍增至
    分片数不超过上限。length 不大于下限时回下限。"""
    if min_partsize is None:
        min_partsize = _MULTIPART_MIN_PART_SIZE
    if max_parts is None:
        max_parts = _MULTIPART_MAX_PARTS
    if length <= min_partsize:
        return min_partsize
    n = -(-length // max_parts)
    partsize = min_partsize
    while partsize < n:
        partsize <<= 1
    return partsize


# ==================== 异常 ====================


class Pan115Error(RuntimeError):
    """115 调用失败（业务错误）"""


class Pan115AuthError(Pan115Error):
    """115 登录态失效（Cookie 过期 / 被踢）——调用方应提示重新配置 Cookie"""


class Pan115ChannelLimitError(Pan115Error):
    """取链通道自身的限制（网页端对大文件回 50028「文件大小超出限制」）。

    文件没死、凭据没问题，只是这条通道不肯发地址。调用方不许据此
    给副本记 stale strike、也不许把绑定落失败态；换 app 通道可解。
    """

    kind = "size_limit"


# ==================== Cookie 解析 ====================


def normalize_cookie(raw: Optional[str]) -> str:
    """规范化 Cookie：去掉换行与首尾空白，便于整体粘贴"""
    return re.sub(r"\s+", " ", (raw or "").strip())


def unseal_account_cookie(stored: Optional[str]) -> str:
    """解开账号配置档 Cookie：fernet-v1 密封值走 Fernet 解封；无前缀的旧明文行原样返回
    （迁移前兼容）；损坏的密封值响亮抛 SealedSecretError，调用方须按凭据失效处理。"""
    value = (stored or "").strip()
    if not value:
        return ""
    if value.startswith("fernet-v1:"):
        return unseal_secret(value)
    return value


def _seal_legacy_row(db, account) -> None:
    """读到旧明文行时顺带密封（只 flush，不单独 commit，随调用方事务落盘；已密封行跳过）。"""
    stored = (account.cookie or "").strip()
    if stored and not stored.startswith("fernet-v1:"):
        account.cookie = seal_secret(normalize_cookie(stored))
        db.flush()


def _account_cookie_for_use(db, account) -> str:
    """账号配置档 Cookie 明文（供 resolve_cookie 各分支共用）：旧明文行顺带密封，
    损坏的密封值转 Pan115Error 明示重录。"""
    _seal_legacy_row(db, account)
    try:
        return unseal_account_cookie(account.cookie or "")
    except SealedSecretError:
        raise Pan115Error(f"115 账号配置档「{account.name}」凭据已损坏，请管理员重录")


def migrate_pan115_account_cookies(db) -> dict:
    """密封迁移：无 fernet-v1 前缀的旧明文行密封入库；已密封/空行跳过；幂等、可重跑。

    生产侧调用点是 resolve_cookie 的顺带密封（本函数供测试与管理脚本直接调用，自带 commit）。
    """
    migrated = 0
    skipped = 0
    for account in db.query(em.Pan115Account).order_by(em.Pan115Account.id).all():
        stored = (account.cookie or "").strip()
        if not stored or stored.startswith("fernet-v1:"):
            skipped += 1
            continue
        account.cookie = seal_secret(normalize_cookie(stored))
        migrated += 1
    if migrated:
        db.commit()
    return {"migrated": migrated, "skipped": skipped}


def resolve_cookie(
    db: Session,
    *,
    library=None,
    account_id: Optional[int] = None,
    explicit_cookie: Optional[str] = None,
) -> tuple[str, str]:
    """解析要使用的 115 Cookie，返回 ``(cookie, 来源说明)``

    优先级：显式账号 > 显式 Cookie > 媒体库绑定账号 > 默认账号 > 服务器级环境变量。
    每次解析前都会刷新一次，因此**配置档更新后无需重启**。

    媒体库绑定账号被删除时，回退到默认账号并给出可读来源；被停用时响亮报错，
    不静默回落到默认账号。
    """
    if account_id is not None:
        account = db.query(em.Pan115Account).filter(em.Pan115Account.id == account_id).first()
        if not account:
            raise Pan115Error(f"115 账号配置档 #{account_id} 不存在")
        if not account.is_enabled:
            raise Pan115Error(f"115 账号配置档「{account.name}」已停用")
        return _account_cookie_for_use(db, account), f"账号配置档「{account.name}」"

    if explicit_cookie and explicit_cookie.strip():
        return normalize_cookie(explicit_cookie), "表单 Cookie"

    bound_id = getattr(library, "account_115_id", None)
    if bound_id:
        account = db.query(em.Pan115Account).filter(em.Pan115Account.id == bound_id).first()
        if account and account.is_enabled:
            return _account_cookie_for_use(db, account), f"媒体库绑定账号「{account.name}」"
        if account:
            raise Pan115Error(f"媒体库绑定的 115 账号「{account.name}」已停用")
        logger.warning(
            "媒体库 #%s 绑定的 115 账号 #%s 不可用，回退到默认账号",
            getattr(library, "id", None), bound_id,
        )

    default = (
        db.query(em.Pan115Account)
        .filter(em.Pan115Account.is_default == True, em.Pan115Account.is_enabled == True)  # noqa: E712
        .first()
    )
    if default:
        return _account_cookie_for_use(db, default), f"默认账号「{default.name}」"

    env_cookie = os.getenv(PAN115_COOKIE_ENV, "").strip()
    if env_cookie:
        return normalize_cookie(env_cookie), f"服务器级 {PAN115_COOKIE_ENV}"

    return "", "未配置"


def resolve_ua(
    db: Session,
    *,
    library=None,
    account_id: Optional[int] = None,
    explicit_ua: str = "",
) -> str:
    """解析该用哪个 UA，**优先级与 :func:`resolve_cookie` 完全一致**

    刻意与 Cookie 走同一套优先级而不是各选各的：UA 是「这个账号像哪台设备」的
    一部分，两边选到不同账号（例如 Cookie 用了默认档、UA 用了停用档）就串味了。
    返回空串 = 用服务器级 ``PAN115_UA``。
    """
    if account_id is not None:
        account = db.query(em.Pan115Account).filter(em.Pan115Account.id == account_id).first()
        return (account.ua or "") if account else ""

    if (explicit_ua or "").strip():
        return explicit_ua.strip()

    bound_id = getattr(library, "account_115_id", None)
    if bound_id:
        account = db.query(em.Pan115Account).filter(em.Pan115Account.id == bound_id).first()
        if account and account.is_enabled:
            return account.ua or ""

    default = (
        db.query(em.Pan115Account)
        .filter(em.Pan115Account.is_default == True, em.Pan115Account.is_enabled == True)  # noqa: E712
        .first()
    )
    return (default.ua or "") if default else ""


def ensure_single_default(db: Session, keep_id: int) -> None:
    """保证同一时间只有一个默认账号（否则配置档更新后行为不确定）"""
    for other in db.query(em.Pan115Account).filter(em.Pan115Account.id != keep_id).all():
        if other.is_default:
            other.is_default = False


# ==================== HTTP 客户端 ====================

# 115 用 state/errno 表达失败；这几个 errno 说明是登录态问题（而不是业务错误）
_AUTH_ERRNOS = {"401", "403", "40101017", "40101002", "990002", "990009"}


def _http_json(
    method: str,
    url: str,
    *,
    params: Optional[dict] = None,
    data: Optional[dict] = None,
    cookie: str = "",
    timeout: Optional[float] = None,
    ua: str = "",
) -> dict:
    """薄 HTTP 封装（测试可替换本函数，其余逻辑全部与网络解耦）"""
    import httpx

    headers = {
        "User-Agent": (ua or "").strip() or PAN115_UA,
        "Referer": "https://115.com/",
        "Accept": "application/json, text/plain, */*",
    }
    if cookie:
        headers["Cookie"] = cookie
    with httpx.Client(timeout=timeout or PAN115_TIMEOUT, follow_redirects=True) as client:
        resp = client.request(method, url, params=params, data=data, headers=headers)
    if resp.status_code in (401, 403):
        raise Pan115AuthError(f"115 返回 HTTP {resp.status_code}，Cookie 可能已失效")
    if resp.status_code >= 400:
        raise Pan115Error(f"115 返回 HTTP {resp.status_code}")
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — 115 偶发返回 HTML 登录页
        raise Pan115AuthError("115 返回了非 JSON 响应，通常是登录态失效")
    if not isinstance(body, dict):
        return {"state": True, "data": body}
    return body


def _raise_for_state(body: dict) -> dict:
    """把 115 的 state/errno 表达统一成异常或成功返回"""
    if body.get("state") in (True, 1, "1"):
        return body
    errno = str(body.get("errno") or body.get("errNo")
                or body.get("msg_code") or "")
    message = str(body.get("error") or body.get("message")
                  or body.get("msg") or "115 接口返回失败")
    if errno in _AUTH_ERRNOS or "登录" in message or "cookie" in message.lower():
        raise Pan115AuthError(message)
    if errno == "50028" or "文件大小超出限制" in message:
        raise Pan115ChannelLimitError(message)
    raise Pan115Error(message)


def _parse_user_id(auth_value: str) -> int:
    """从 115 凭据串的 UID 段解析数字 user_id（app 通道参数用）。

    UID 形如 ``123456_A1_1700000000``；解析不到返回 0（不带此参数）。
    """
    for part in (auth_value or "").split(";"):
        part = part.strip()
        if part.startswith("UID="):
            head = part[4:].split("_")[0]
            return int(head) if head.isdigit() else 0
    return 0


class Pan115Client:
    """Cookie 型 115 客户端（只覆盖直挂与后台浏览用到的几个端点）"""

    def __init__(self, cookie: str, ua: str = ""):
        self.cookie = normalize_cookie(cookie)
        # 空 = 用服务器级 PAN115_UA；不同配置档可以指向不同设备（115 直链接口挑 UA）
        self.ua = (ua or "").strip() or PAN115_UA
        if not self.cookie:
            raise Pan115AuthError("未配置 115 Cookie")

    # ---- 账号 ----

    def check(self) -> dict:
        """校验 Cookie 是否有效，返回 ``{ok, uid, vip}``"""
        body = self._call(
            "GET", f"{PAN115_WEBAPI_BASE}/files",
            params={"aid": 1, "cid": 0, "limit": 1, "offset": 0, "show_dir": 1},
        )
        _raise_for_state(body)
        info = body.get("data") or {}
        if isinstance(info, list):
            info = {}
        return {"ok": True, "uid": str(info.get("uid") or "") or None,
                "vip": bool(info.get("vip") or info.get("is_vip"))}


    # ---- 目录 ----

    def list_dir(self, cid: str = "0", limit: int = 200) -> list[dict]:
        """列目录（用于目标路径浏览）

        P1 修复（审查）：此前写死 limit=200/offset=0，超 200 个文件的目录
        被静默截断。现自动翻页取全量（单页 limit 可配，上限 1000）。
        """
        limit = max(1, min(int(limit or 200), 1000))
        out: list[dict] = []
        offset = 0
        while True:
            body = self._call(
                "GET", f"{PAN115_WEBAPI_BASE}/files",
                params={
                    "aid": 1, "cid": cid or "0", "o": "user_ptime", "asc": 1,
                    "show_dir": 1, "limit": limit, "offset": offset,
                },
            )
            _raise_for_state(body)
            data = body.get("data") or []
            if not isinstance(data, list):
                break
            out.extend(
                {
                    # #13 PR-3：fid 必须是文件自身的真实 id，绝不拿父目录 cid
                    # 回落冒充——同目录所有文件会共享一个"身份"，file_id 认亲层
                    # 会大规模错认。API 没给 fid 时 fid 为空串（无身份）。
                    # cid 侧保留回落（目录浏览靠 cid，目录项本无 fid）。
                    "fid": str(entry.get("fid") or ""),
                    "cid": str(entry.get("cid") or entry.get("fid") or ""),
                    "name": entry.get("n") or entry.get("name") or "",
                    "is_dir": _is_dir(entry),
                    "size": int(entry.get("s") or 0),
                    "pickcode": entry.get("pc") or "",
                    # sha1 仅透传（响应里有才取，缺时为空）：本 PR 不建
                    # hash 上传秒传路径——服务端内 copy 不需要 sha1，
                    # 绝不拿 pickcode 或其它字段冒充哈希。
                    "sha1": entry.get("sha1") or "",
                }
                for entry in data
            )
            if len(data) < limit:
                break
            offset += len(data)
            # 安全阀：单目录 10 万条封顶，防异常 API 无限翻页
            if offset >= 100000:
                break
        return out

    # ---- 下载 ----

    def download_url(self, pickcode: str, *, app_first: bool = False) -> str:
        """取单个文件下载地址（供外部下载器 / 直链使用）。

        两条通道（2026-10-31 生产探针实证）：
        - 网页端 webapi /files/download：大文件回 50028「文件大小超出
          限制」不给地址（843MB 变体实测中招，157MB 正常）；
        - app 端 proapi /app/chrome/downurl（RSA 加密通道）：无此
          大小限制，843MB 实测取到地址且 Range 拉到真字节（206）。

        默认网页端优先、撞大小限制回落 app 端；``app_first=True``
        时 app 端优先——播放器直链场景用：115 的签名地址绑签名时
        的 UA，网页端用播放器 UA 签出的地址拉流会被 CDN 拒
        （实测 403「no cookie」），app 通道用播放器 UA 签名 +
        同 UA 拉流实测 206。认证错误两条通道同命，不回落直接抛。

        ``app_first=True`` 时 app 通道失败**不回落网页端**：网页端
        在播放器 UA 下签出的地址正是上面说的必拒形态，回落等于把
        死地址当成功 302 发给播放器（台账记 ok、播放器撞 403、
        服务端零感知）。如实抛错（认证→expired、50028→
        size_limit、其余→transient），调用方按档定性、用户重试
        即可；app 通道恢复后自然能播。
        """
        if app_first:
            try:
                return self._download_url_app(pickcode)
            except Pan115Error as exc:
                logger.warning(
                    "115 app 通道取链失败，播放器场景不回落网页端："
                    "err=%s", exc)
                raise
        try:
            return self._download_url_web(pickcode)
        except Pan115ChannelLimitError:
            return self._download_url_app(pickcode)

    def _download_url_web(self, pickcode: str) -> str:
        """网页端取链（webapi /files/download，大文件有大小限制）。"""
        body = self._call(
            "GET", f"{PAN115_WEBAPI_BASE}/files/download", params={"pickcode": pickcode},
        )
        _raise_for_state(body)
        url = body.get("file_url") or body.get("url")
        if not url:
            raise Pan115Error("115 未返回下载地址")
        return str(url)

    def _download_url_app(self, pickcode: str) -> str:
        """app 端取链（proapi /app/chrome/downurl，RSA 加密通道）。

        请求体 ``{"data": rsa_encrypt(JSON)}``，JSON 含 pickcode 与
        user_id（从凭据 UID 段解析，解析不到就不带——服务端可由
        会话推导，2026-10-31 探针两形态均成功）；响应 data 字段
        同算法加密，解开后是 ``{文件 id: {"url": {"url": 地址}}}``。
        签名用的 UA 就是本客户端的 UA（地址绑 UA，见 download_url）。
        """
        from backend.emby_server import pan115_cipher

        payload: dict = {"pickcode": pickcode}
        user_id = _parse_user_id(self.cookie)
        if user_id:
            payload["user_id"] = user_id
        body = self._call(
            "POST", f"{PAN115_PROAPI_BASE}/app/chrome/downurl",
            data={"data": pan115_cipher.rsa_encrypt(
                json.dumps(payload).encode("utf-8"))},
        )
        _raise_for_state(body)
        data = body.get("data")
        if isinstance(data, str) and data:
            try:
                data = json.loads(pan115_cipher.rsa_decrypt(data))
            except Exception as exc:  # noqa: BLE001 — 解密失败即通道失败
                raise Pan115Error("115 app 通道响应解密失败") from exc
        if isinstance(data, dict):
            for entry in data.values():
                if not isinstance(entry, dict):
                    continue
                url_field = entry.get("url")
                if isinstance(url_field, dict):
                    url = url_field.get("url") or url_field.get("address")
                else:
                    url = url_field
                if url:
                    return str(url)
        raise Pan115Error("115 app 通道未返回下载地址")

    # ---- 转存（同 provider 内 copy，不搬字节） ----

    def copy_file(self, source_fid: str, dest_cid: str) -> dict:
        """服务端内 copy（转存到用户盘用）。

        请求形态：POST /files/copy，fid=源 fid、pid=目标目录 cid。
        响应形态待 115 测试账号实测：返回原始 body，调用方用多字段
        容错提新 fid/pickcode，提不到再列目标目录匹配。115 copy 是
        服务端内操作、通常瞬时，但大文件不假设严格瞬时——状态机与
        Drive 线同一套（准备中 → 就绪/失败），不因“秒传”省状态。
        """
        body = self._call(
            "POST", f"{PAN115_WEBAPI_BASE}/files/copy",
            data={"fid": source_fid, "pid": dest_cid},
        )
        return _raise_for_state(body)

    def mkdir(self, parent_cid: str, name: str) -> dict:
        """在指定目录下建子目录（转存目录用）。

        请求形态：POST /files/add，pid=父目录 cid、cname=目录名。
        响应形态待实测：返回原始 body，调用方建完按名重列验 cid，
        不依赖本响应字段。
        """
        body = self._call(
            "POST", f"{PAN115_WEBAPI_BASE}/files/add",
            data={"pid": parent_cid, "cname": name},
        )
        return _raise_for_state(body)

    def rename_file(self, fid: str, new_name: str) -> dict:
        """改名（云下载落盘名是 URL 尾段，认领后改回副本名用）。

        请求形态：POST /files/edit，fid + file_name（p115client
        fs_edit 口径）。返回原始 body 经 state 校验。
        """
        body = self._call(
            "POST", f"{PAN115_WEBAPI_BASE}/files/edit",
            data={"fid": fid, "file_name": new_name},
        )
        return _raise_for_state(body)

    # ---- 云下载（形态 B：让 115 自己拉，2026-10-30 立项） ----
    # 协议形态以 2026-10-30 生产探针实证为准（报告
    # pan115-cloudpull-feasibility-20261030.md 第六节）：web 形态
    # lixian 接口，签名先行；抓取器是单次普通 GET、无 HEAD/Range。
    # 任务记录里存有提交的 URL（我方场景下是带签名的取流地址），
    # 调用方用完必须 cloudpull_del_task 删掉，不许留痕。


    # ---- 上传（跨 provider 转存 Drive→115 用） ----


    # ---- 秒传（initupload，跨 provider 转存 Drive→115 用） ----


    # ---- 分片上传（initupload + OSS 多段，>5 GiB 全量路） ----


    # ---- 内部 ----

    def _call(self, method: str, url: str, *, params: Optional[dict] = None,
              data: Optional[dict] = None) -> dict:
        return _http_json(method, url, params=params, data=data, cookie=self.cookie,
                          ua=self.ua)


def _is_dir(entry: dict) -> bool:
    """115 目录项判定：优先显式字段，其次 fc == "0"（115 的目录分类码）"""
    if "is_dir" in entry:
        return bool(entry["is_dir"])
    for key in ("isdir", "is_dir"):
        if key in entry:
            return bool(entry[key])
    fc = entry.get("fc")
    if fc is not None:
        return str(fc) == "0"
    # 目录没有大小、没有 pickcode 是常见特征
    return not entry.get("s") and not entry.get("pc")


def verify_account(cookie: str) -> dict:
    """校验一个 Cookie（供后台「测试账号」按钮）"""
    client = Pan115Client(cookie)
    try:
        return client.check()
    except Pan115AuthError as exc:
        return {"ok": False, "message": str(exc), "auth_error": True}
    except Pan115Error as exc:
        return {"ok": False, "message": str(exc), "auth_error": False}
