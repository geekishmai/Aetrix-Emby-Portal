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
import http.cookiejar as cookiejar
import re
import threading
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


def _oss_object_url(bucket, object_key, query=None):
    """虚拟主机式 OSS URL：{scheme}://{bucket}.{host}/{object}。"""
    scheme, _, host = PAN115_OSS_ENDPOINT.partition("://")
    url = f"{scheme}://{bucket}.{host}/{object_key}"
    if query:
        url += "?" + urlencode(query)
    return url


def _oss_auth_headers(token, method, bucket, object_key, query=None,
                      extra_headers=None, content_type=""):
    """阿里云 OSS 头签名（p115oss oss_sign 移植，STS 凭据）。

    待签名串 = METHOD + "\n" + Content-MD5（空）+ "\n" +
    Content-Type + "\n" + Date + "\n" + 排序后的 x-oss-* 头
    （每行 "k:v\n"）+ CanonicalizedResource（/{bucket}/{object}
    + 命中的子资源 query 按键排序 urlencode）。签名 =
    base64(HMAC-SHA1(AccessKeySecret, 待签名串))。
    """
    headers = dict(extra_headers or {})
    headers["date"] = formatdate(usegmt=True)
    if token.get("SecurityToken"):
        headers["x-oss-security-token"] = token["SecurityToken"]
    if content_type:
        headers["content-type"] = content_type
    canon = "".join(
        f"{key}:{headers[key]}\n"
        for key in sorted(headers) if key.startswith("x-oss-"))
    resource = f"/{bucket}/{object_key}"
    if query:
        subs = sorted((k, v) for k, v in query.items()
                      if k in _OSS_SUBRESOURCE_KEYS)
        if subs:
            resource += "?" + urlencode(subs)
    string_to_sign = (
        f"{method}\n\n{content_type}\n{headers['date']}\n"
        f"{canon}{resource}")
    signature = b64encode(hmac.new(
        str(token.get("AccessKeySecret") or "").encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1).digest()).decode("ascii")
    headers["Authorization"] = f"OSS {token.get('AccessKeyId')}:{signature}"
    return headers


def _xml_find_text(content, tag):
    """OSS XML 响应里按本地名找第一个元素的文本（命名空间容错）。"""
    try:
        root = ET.fromstring(content)
    except Exception:  # noqa: BLE001 — XML 不可解析即提不到
        return ""
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == tag:
            return (el.text or "").strip()
    return ""


def _http_full(method: str, url: str, *, headers: Optional[dict] = None,
               data=None, client=None):
    """OSS 专用底层请求：返回 (status, 小写键响应头 dict, content)。

    与 _http_bytes 同分工（测试可替换本函数）。非 2xx 统一抛
    Pan115Error 并摘录响应体——OSS 的错误是 XML，且 STS 令牌失效
    与 115 Cookie 失效是两回事，不套 _http_bytes 的 401/403 →
    AuthError 换算。超时按大文件口径放大（单片可达数十 MiB）。
    """
    import httpx

    own = client is None
    oss_timeout = httpx.Timeout(connect=30.0, read=600.0, write=600.0,
                                pool=30.0)
    # 长传输（OSS 上传/下载，read 600s）**不复用共享池**：共享池只有 4 条，
    # 一个长传输就会长期占一条，列目录/取链会排到 PoolTimeout 并被计为错误。
    # 每次独立 client，传输结束即关；长传输频次低，重做一次握手可接受。
    c = client or httpx.Client(timeout=oss_timeout, follow_redirects=True)
    try:
        resp = c.request(method, url, headers=headers or {}, data=data)
    except httpx.HTTPError as exc:
        raise Pan115Error(f"115 OSS 请求失败：{exc}") from exc
    finally:
        if own:
            c.close()
    if resp.status_code >= 400:
        raise Pan115Error(
            f"115 OSS 返回 HTTP {resp.status_code}："
            f"{resp.content[:200]!r}")
    return (resp.status_code,
            {k.lower(): v for k, v in resp.headers.items()},
            resp.content)


def _read_exact(fileobj, n):
    """从 fileobj 顺序读满 n 字节（读短返回实得字节，调用方判失败）。"""
    buf = bytearray()
    while len(buf) < n:
        piece = fileobj.read(n - len(buf))
        if not piece:
            break
        buf += piece
    return bytes(buf)


def _pkcs7_pad(data: bytes) -> bytes:
    pad = 16 - (len(data) % 16)
    return data + bytes([pad]) * pad


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        raise Pan115Error("115 秒传响应为空")
    pad = data[-1]
    if pad < 1 or pad > 16 or data[-pad:] != bytes([pad]) * pad:
        raise Pan115Error("115 秒传响应填充非法")
    return data[:-pad]


def _ecdh_aes_encrypt(data: bytes) -> bytes:
    """AES-128-CBC 加密（ECDH 派生的固定 key/iv，p115cipher 口径）。"""
    from cryptography.hazmat.primitives.ciphers import (
        Cipher, algorithms, modes)
    enc = Cipher(algorithms.AES(_P115_AES_KEY),
                 modes.CBC(_P115_AES_IV)).encryptor()
    return enc.update(_pkcs7_pad(data)) + enc.finalize()


def _unpad_response(data: bytes) -> bytes:
    """initupload 响应填充剥离（p115cipher ``_unpad`` 口径）。

    服务端加密前只在明文非块对齐时才补填充，块对齐时不补，所以
    不能按 PKCS7 严口径判「填充非法」：末字节构成 1..15 的等值
    短填充才剥，否则原样返回（无填充，或末字节本就是数据）。
    """
    if data:
        pad = data[-1]
        if 0 < pad < 16 and data[-pad:] == bytes([pad]) * pad:
            return data[:-pad]
    return data


def _ecdh_aes_decrypt(cipher_data: bytes) -> bytes:
    """AES-128-CBC 解密 + 分段 lz4 帧解压（p115cipher 口径）。

    115 的响应密文尾部带非块对齐的尾巴（2026-10-31 活体实测
    188 字节 = 176 字节密文 + 12 字节尾巴），解密前须先截到
    16 字节对齐（p115cipher 通行分支同口径）；整段喂解密器
    会在 finalize 处抛错——秒传上线以来每次 initupload 都死
    在这里、被执行器当协议异常回落全量上传。
    """
    if not cipher_data:
        raise Pan115Error("115 秒传响应为空")
    chunk = cipher_data[:len(cipher_data) & -16]
    if not chunk:
        raise Pan115Error("115 秒传响应过短")
    from cryptography.hazmat.primitives.ciphers import (
        Cipher, algorithms, modes)
    dec = Cipher(algorithms.AES(_P115_AES_KEY),
                 modes.CBC(_P115_AES_IV)).decryptor()
    plain = dec.update(chunk) + dec.finalize()
    return _framed_lz4_decompress(_unpad_response(plain))


def _lz4_block_decompress(block: bytes) -> bytes:
    """纯 Python LZ4 block 解压（运行环境无 lz4 库，不新增依赖）。"""
    out = bytearray()
    i = 0
    n = len(block)
    while i < n:
        token = block[i]
        i += 1
        lit_len = token >> 4
        if lit_len == 15:
            while True:
                if i >= n:
                    raise Pan115Error("lz4 块截断（字面量长度）")
                b = block[i]
                i += 1
                lit_len += b
                if b != 255:
                    break
        out += block[i:i + lit_len]
        i += lit_len
        if i >= n:
            break
        if i + 2 > n:
            raise Pan115Error("lz4 块截断（偏移）")
        offset = block[i] | (block[i + 1] << 8)
        i += 2
        if offset <= 0 or offset > len(out):
            raise Pan115Error("lz4 偏移非法")
        match_len = token & 0x0F
        if match_len == 15:
            while True:
                if i >= n:
                    raise Pan115Error("lz4 块截断（匹配长度）")
                b = block[i]
                i += 1
                match_len += b
                if b != 255:
                    break
        match_len += 4
        start = len(out) - offset
        for j in range(match_len):
            out.append(out[start + j])
    return bytes(out)


def _framed_lz4_decompress(data: bytes) -> bytes:
    """p115cipher 响应帧：重复（2 字节小端块长 + lz4 block）。

    帧解析不成立且明文像 JSON 时原样返回（个别响应只加密不压缩
    的兜底）；两者都不成立才报协议错误（调用方回落全量上传）。
    """
    out = bytearray()
    src = data
    ok = True
    while src and len(src) > 2:
        ln = src[0] | (src[1] << 8)
        if ln == 0 or ln > len(src) - 2:
            ok = False
            break
        try:
            out += _lz4_block_decompress(src[2:2 + ln])
        except Pan115Error:
            ok = False
            break
        src = src[2 + ln:]
    if ok and not src:
        return bytes(out)
    if out:
        # 已解出至少一段：与 p115cipher 同口径宽容返回已解部分，
        # 尾部残段差异不把好响应判死（JSON 是否成立由调用方判）。
        return bytes(out)
    if data.lstrip()[:1] == b"{":
        return data
    raise Pan115Error("115 秒传响应解压失败")


def _ecdh_encode_token(timestamp: int) -> str:
    """k_ec 参数：公钥 + 时间戳 + CRC 的 base64（p115cipher 口径）。"""
    token = bytearray()
    token += _P115_AES_PUBKEY[:15]
    token += b"\x00s\x00\x00\x00"
    token += int(timestamp).to_bytes(4, "little")
    token += _P115_AES_PUBKEY[15:]
    token += b"\x00\x01\x00\x00\x00"
    token += crc32(_P115_CRC_SALT + bytes(token)).to_bytes(4, "little")
    return b64encode(bytes(token)).decode("ascii")


def _make_upload_payload(payload: dict):
    """构造 initupload 请求：返回 (params, AES 加密 body)。

    payload 须含 userid/userkey/fileid/target/filesize/appversion，
    sign_key/sign_val 可空（空值不进加密表单，与开源实现同口径）；
    本函数补 t/sig/token（每次调用都刷新，二轮校验重发同理）。
    """
    payload = dict(payload)
    t = payload["t"] = int(time.time())
    sig = hashlib.sha1(payload["userkey"].encode("ascii"))
    sig.update(b2a_hex(hashlib.sha1(
        "{userid}{fileid}{target}0".format_map(payload)
        .encode("ascii")).digest()))
    sig.update(b"000000")
    payload["sig"] = sig.hexdigest().upper()
    token = hashlib.md5(_P115_MD5_SALT)
    token.update(
        "{fileid}{filesize}{sign_key}{sign_val}{userid}{t}"
        .format_map(payload).encode("ascii"))
    token.update(b2a_hex(hashlib.md5(
        b"%d" % int(payload["userid"])).digest()))
    token.update(payload["appversion"].encode("ascii"))
    payload["token"] = token.hexdigest()
    items = sorted((k, v) for k, v in payload.items() if v)
    body = _ecdh_aes_encrypt(urlencode(items).encode("latin-1"))
    return {"k_ec": _ecdh_encode_token(t)}, body


def _parse_sign_check_range(sign_check):
    """sign_check 是 Range 区间串 "start-end"（闭区间）；失败回 None。"""
    try:
        start_s, end_s = str(sign_check).strip().split("-", 1)
        start, end = int(start_s), int(end_s)
    except (TypeError, ValueError):
        return None
    if start < 0 or end < start:
        return None
    return start, end


def _extract_init_result(resp):
    """秒传命中响应里提 fid + pickcode（多字段容错）。

    提不到返回空串，调用方走列目录匹配回落（不依赖本响应字段）。
    """
    if not isinstance(resp, dict):
        return "", ""
    data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    fid = str(data.get("file_id") or data.get("fid") or data.get("id")
              or resp.get("file_id") or "")
    pickcode = str(data.get("pickcode") or data.get("pick_code")
                   or data.get("pc") or resp.get("pickcode") or "")
    return fid, pickcode


def _http_bytes(method: str, url: str, *, params: Optional[dict] = None,
                data: Optional[bytes] = None, auth_ck: Optional[str] = None,
                timeout: Optional[float] = None, ua: str = "",
                content_type: str = "") -> bytes:
    """initupload 专用薄封装：响应是加密字节、不是 JSON。

    与 _http_json 同分工（测试可替换本函数，其余逻辑与网络解耦）。
    """
    import httpx

    headers = {
        "User-Agent": (ua or "").strip() or PAN115_UA,
        "Referer": "https://115.com/",
        "Accept": "application/json, text/plain, */*",
    }
    if auth_ck:
        headers["Cookie"] = auth_ck
    if content_type:
        headers["Content-Type"] = content_type
    resp = _request_shared(method, url, params=params, data=data,
                           headers=headers,
                           timeout=timeout or PAN115_TIMEOUT,
                           follow_redirects=True)
    if resp.status_code in (401, 403):
        raise Pan115AuthError(
            f"115 返回 HTTP {resp.status_code}，Cookie 可能已失效")
    if resp.status_code >= 400:
        raise Pan115Error(f"115 返回 HTTP {resp.status_code}")
    return resp.content

# ==================== 异常 ====================


class Pan115Error(RuntimeError):
    """115 调用失败（业务错误）"""


class Pan115AuthError(Pan115Error):
    """115 登录态失效（Cookie 过期 / 被踢）——调用方应提示重新配置 Cookie"""


class Pan115CooldownError(Pan115Error):
    """115 账号处于风控冷却期（pan115_guard 限速冷却，Q&A Z-1）。

    **不是 Cookie 失效**：本次调用未发出、凭据没问题，到点自动恢复。
    调用方（挂载测试/播放错误/管理面板）必须把它与 ``Pan115AuthError``
    区分开——提示「账号限速冷却中，约 X 分钟后自动恢复」，绝不能引导
    用户去重配 Cookie（重配过程本身可能再触发风控）。

    - ``reason``：冷却原因（429 / 405 / 排队超时等）
    - ``retry_after``：剩余冷却秒数（浮点，向上取整到分钟用于展示）
    """

    def __init__(self, reason: str, remaining: float):
        super().__init__(f"115 账号限速冷却中（{reason}），约 "
                         f"{int(remaining // 60) + 1} 分钟后自动恢复")
        self.reason = reason
        self.retry_after = max(1.0, float(remaining))


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

# ---- 进程级共享 HTTP 客户端（keep-alive） ----
# 每次调用都新建 httpx.Client 意味着每个请求重做 TCP+TLS 握手：2 次/秒的
# 列目录就是 2 个新 TLS 连接/秒，既放大握手超时概率，也不像正常客户端。
# 共享一个客户端即可复用连接；Cookie/UA 仍然逐次放进请求头（不进 client
# 级 headers），不同账号不会串。
#
# - 线程安全：httpx.Client 本身可并发；创建/丢弃由 _http_client_lock 串行化。
# - 连接池上限刻意压小（4 条、空闲 30s 回收），与 pan115_guard 的并发口径
#   一致，不因为 keep-alive 放大对 115 的连接数。
# - fork 后子进程不能复用父进程连接：按 pid 检测并重建（旧引用直接丢弃，
#   不 close——fd 属于父进程，关闭只影响子进程副本，没必要动）。
# - 连接级错误（连接被对端关掉/协议错/连接超时）后丢弃当前客户端，下一次
#   调用重建；超时/HTTP 状态错误不重建。
_http_client_lock = threading.Lock()
_http_client = None
_http_client_pid = None


def _reset_http_client_after_fork() -> None:
    """fork 子进程里换掉锁与客户端引用。

    父进程 fork 时若别的线程正持锁，子进程里这把锁会永远锁着——必须先
    换新锁再去碰状态；连接对象本身也不复用（fd 属于父进程）。
    """
    global _http_client_lock, _http_client, _http_client_pid
    _http_client_lock = threading.Lock()
    _http_client = None
    _http_client_pid = None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_http_client_after_fork)


class _RejectAllCookies(cookiejar.DefaultCookiePolicy):
    """共享 client 拒收一切 Set-Cookie。

    115 响应会带 Set-Cookie；共享 jar 会把 A 账号的会话 Cookie 存下来，
    再被 B 账号的请求自动带上（跨账号串 Cookie）。Cookie 一律由调用方逐
    请求放 headers，因此这里直接拒收。
    """

    def set_ok(self, cookie, request):   # noqa: D102 — cookiejar 协议
        return False


def _new_http_client():
    import httpx

    return httpx.Client(
        timeout=PAN115_TIMEOUT,
        follow_redirects=False,     # 逐请求传：json/bytes 要跟随
        # 不保存任何 Set-Cookie：多账号共用 client，jar 必须是空的
        cookies=cookiejar.CookieJar(policy=_RejectAllCookies()),
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=4,
                            keepalive_expiry=30.0),
    )


def _get_http_client():
    """取进程级共享客户端；fork 后按 pid 重建（不复用父进程连接）。"""
    global _http_client, _http_client_pid
    pid = os.getpid()
    with _http_client_lock:
        if _http_client is not None and _http_client_pid != pid:
            _http_client = None
            _http_client_pid = None
        if _http_client is None:
            _http_client = _new_http_client()
            _http_client_pid = pid
        return _http_client


def _discard_http_client(client) -> None:
    """连接级错误后丢弃共享客户端；只丢弃当前那一个（并发下不误杀新实例）。

    取舍：close() 会连带打断其他线程正在用这个 client 的请求（它们会拿到
    ConnectError/ReadError）。连接级错误意味着连接池本身已坏，继续复用只
    会扩大失败面；代价是这一瞬间的并发请求可能被误伤，由调用方按普通网络
    错误重试/降级。不 close 而只是丢引用则会把坏连接留给下一次调用，故选择
    立即关闭。
    """
    global _http_client, _http_client_pid
    with _http_client_lock:
        if _http_client is not client:
            return
        _http_client = None
        _http_client_pid = None
    try:
        client.close()
    except Exception:  # noqa: BLE001 — 丢弃路径不因关闭失败而报错
        pass


def _is_connection_level_error(exc: BaseException) -> bool:
    """连接本身坏了（而非 115 业务/HTTP 状态错误）——需要重建连接池。

    取舍（QA P3 问：ReadTimeout 算不算连接级错误）——**不算**：
    - ReadTimeout 说明连接已经建立、服务器只是没在时限内回包，是「服务端慢」
      而不是「连接坏了」。115 在列目录满速段（1.8–2.0 次/秒）延迟本来就高，
      这时若把超时当连接坏了就丢弃池子，等于在最该稳住连接的时候不停重连，
      反而制造新的 TLS 握手风暴（run 10 的握手超时就是这么来的）。
    - 真正「连接已死」的表现是 httpx.NetworkError（ConnectError/ReadError/
      WriteError）、RemoteProtocolError（对端在空闲后直接关掉 keep-alive
      连接）、ConnectTimeout（连不上）——这三种都算连接级，按需重建。
    - 读超时的后果由调用方按普通网络错误处理（重试/降级/记错误），不重建池。
    """
    import httpx

    return isinstance(exc, (httpx.NetworkError, httpx.RemoteProtocolError,
                            httpx.ConnectTimeout))


def _is_closed_client_error(exc: BaseException) -> bool:
    """httpx 客户端已被关闭（另一个线程丢弃并 close 了共享实例）。

    httpx 对「client 已关闭」抛的是 **RuntimeError**（不是 httpx.HTTPError）：
    ``Cannot send a request, as the client has been closed.``。
    共享 client 被别的线程丢弃时，本线程的请求会撞上它——必须识别出来，
    重取一个新 client 再发一次，否则并发下一个连接级错误会把整个扫描打挂。
    """
    return isinstance(exc, RuntimeError) and "has been closed" in str(exc)


def _request_shared(method: str, url: str, **kwargs):
    """用进程级共享 client 发请求；client 被并发线程关闭时重取一次再发。"""
    import httpx

    client = _get_http_client()
    try:
        return client.request(method, url, **kwargs)
    except RuntimeError as exc:
        if not _is_closed_client_error(exc):
            raise
        # 另一线程丢弃并关闭了共享 client：丢掉手里这个（可能已经不是当前
        # 实例，_discard 会自行判断），重取一个新的再试一次。
        _discard_http_client(client)
        fresh = _get_http_client()
        return fresh.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        # 连接级错误（池已坏）→ 丢弃共享 client，下一次调用重建；
        # 读超时不算连接级（见 _is_connection_level_error 的取舍说明）。
        if _is_connection_level_error(exc):
            _discard_http_client(client)
        raise


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
    resp = _request_shared(method, url, params=params, data=data,
                           headers=headers,
                           timeout=timeout or PAN115_TIMEOUT,
                           follow_redirects=True)
    if resp.status_code in (401, 403):
        raise Pan115AuthError(f"115 返回 HTTP {resp.status_code}，Cookie 可能已失效")
    if resp.status_code >= 400:
        err = Pan115Error(f"115 返回 HTTP {resp.status_code}")
        err.status = resp.status_code   # 给 pan115_guard 识别 429 / 405
        raise err
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

    def list_dir(self, cid: str = "0", limit: int = 1000,
                 deadline: Optional[float] = None) -> list[dict]:
        """列目录（用于目标路径浏览）

        P1 修复（审查）：此前写死 limit=200/offset=0，超 200 个文件的目录
        被静默截断。现自动翻页取全量（单页 limit 可配，上限 1000）。

        QA AC-3 P1（2026-11-03）：默认页从 200 提到 **1000**。依据（源码级）：
        115 Cookie 通道 /files 单页上限 1000，OpenList `drivers/115/meta.go`
        与 AList v3.63.0 `drivers/115/meta.go` 的默认 `page_size` 同为 1000
        （见 docs/pan115-rate-evidence.md §1.4/1.5）。1020 项的华语电影根
        目录从 6 页降到 2 页——首扫 list 次数直接砍掉 2/3。

        QA AE-3 P1（翻页静默截断）：115 /files 响应带 ``count``（目录条目
        总数，115-sdk-go types.go:87）。现在每页读 count 并**校验已取条数**：
        未取满继续翻页；数据提前耗尽仍不足则**抛错**（绝不静默少条目——
        少条目会被扫描器当成已删除，触发错误清理）。

        QA AC-3 P1（2026-11-03）：默认页从 200 提到 **1000**（115 /files
        单页上限，OpenList/AList 驱动源码默认同为 1000）。1020 项的华语
        电影根目录从 6 页降到 2 页——首扫 list 次数直接砍掉 2/3。

        deadline（修法②）：整次列目录的绝对截止时刻，翻页每页都检查——
        一页拿完已过截止就抛 ``Pan115DeadlineExceeded``，不再发下一页。
        """
        limit = max(1, min(int(limit or 1000), 1000))
        out: list[dict] = []
        offset = 0
        expected_total: Optional[int] = None
        while True:
            if deadline is not None and time.time() > deadline:
                from backend.emby_server import pan115_guard
                raise pan115_guard.Pan115DeadlineExceeded(
                    "sidecar 丢弃超时请求，未发下一页")
            body = self._call(
                "GET", f"{PAN115_WEBAPI_BASE}/files",
                params={
                    "aid": 1, "cid": cid or "0", "o": "user_ptime", "asc": 1,
                    "show_dir": 1, "limit": limit, "offset": offset,
                },
            )
            _raise_for_state(body)
            # QA AE-3：count 是目录条目总数（含目录）。只认第一页给出的值，
            # 后续页不再更新（并发增删时以首个快照为准，避免自激翻页）。
            if expected_total is None:
                try:
                    _cnt = body.get("count")
                    expected_total = int(_cnt) if _cnt is not None else None
                except (TypeError, ValueError):
                    expected_total = None
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
            offset += len(data)
            if expected_total is not None:
                # 按 count 校验：未取满继续翻；取满即停；数据耗尽仍不足 → 抛错
                if len(out) >= expected_total:
                    break
                if not data:
                    raise Pan115Error(
                        f"115 列目录不完整：count={expected_total} 实取={len(out)}"
                        f"（cid={cid} offset={offset}，拒绝静默少条目）")
                continue
            # 无 count（旧响应/异常载荷）：维持原口径（短页即止）
            if len(data) < limit:
                break
            # 安全阀：单目录 10 万条封顶，防异常 API 无限翻页
            if offset >= 100000:
                break
        if expected_total is not None and len(out) < expected_total:
            raise Pan115Error(
                f"115 列目录不完整：count={expected_total} 实取={len(out)}（cid={cid}）")
        return out

    # ---- 下载 ----

    def download_url(self, pickcode: str, *, app_first: bool = False,
                     deadline: Optional[float] = None) -> str:
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
            # Claude 决定（batch/deploy-2）：取上游 9d6a2d28 的「不回落」语义
            # （网页端在播放器 UA 下签出的地址必被 CDN 拒，回落=发死地址），
            # 同时保留我方 deadline 参数（sidecar 宿主截止时刻，sidecar-deadline 系列）。
            try:
                return self._download_url_app(pickcode, deadline=deadline)
            except Pan115Error as exc:
                logger.warning(
                    "115 app 通道取链失败，播放器场景不回落网页端："
                    "err=%s", exc)
                raise
        try:
            return self._download_url_web(pickcode, deadline=deadline)
        except Pan115ChannelLimitError:
            return self._download_url_app(pickcode, deadline=deadline)

    def _download_url_web(self, pickcode: str, deadline: Optional[float] = None) -> str:
        """网页端取链（webapi /files/download，大文件有大小限制）。"""
        body = self._call(
            "GET", f"{PAN115_WEBAPI_BASE}/files/download", params={"pickcode": pickcode},
            deadline=deadline,
        )
        _raise_for_state(body)
        url = body.get("file_url") or body.get("url")
        if not url:
            raise Pan115Error("115 未返回下载地址")
        return str(url)

    def _download_url_app(self, pickcode: str, deadline: Optional[float] = None) -> str:
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
            deadline=deadline,
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

    def simple_upload(self, fileobj, filename: str, pid: str) -> dict:
        """简单上传（115cli 0.3.4 的 simple_upload 口径，Cookie 鉴权）。

        两步，不走 initupload 加密协议（见模块外说明）：
        1. POST ``uplb.115.com/3.0/sampleinitupload.php``（filename +
           target=U_1_<pid>）取 OSS 表单参数（host/object/policy/
           accessid/callback/signature）；
        2. 向 host 做 OSS 表单 POST（字段 name/key/policy/
           OSSAccessKeyId/success_action_status=200/callback/signature
           + 文件体），success_action_status=200 时响应体即 115 回调
           JSON（含 file_id/file_name/pick_code/sha1/file_size）。

        上限：115cli 的 MAX_SIMPLE_UPLOAD_SIZE = 5 GiB，超限文件不
        走本方法（调用方先拦，超限改走 multipart_upload 分片）。
        本方法入口亦自带同一上限守卫：能用 seek/tell 测出大小时，
        超限直接报 Pan115Error，绝不在初始化 OSS 会话后再失败；
        测不出大小时才放行（调用方已拦，守卫是第二道、不是唯一一道）。
        fileobj 须支持 read/seek/tell（httpx 按 seek/tell 算
        Content-Length；跨 provider 搬字节用分段读取器直供，不落盘）。
        fileobj.read 中抛出的搬运层异常（如源盘凭据失效）原样上抛，
        本方法只把 httpx 层失败转成 Pan115Error/AuthError。
        返回 ``{fid, pickcode, raw}``；回调里提不到 fid/pickcode 时
        对应字段为空串，调用方走列目录匹配回落（不依赖本响应字段）。
        """
        import httpx

        # 入口上限守卫（第二道）：测得出大小且超 5 GiB 即拒，绝不
        # 先初始化 OSS 会话；测不出大小时放行，调用方已先拦过。
        _max = 5 * 1024 * 1024 * 1024  # 115cli MAX_SIMPLE_UPLOAD_SIZE
        try:
            _pos = fileobj.tell()
            _end = fileobj.seek(0, 2)
            fileobj.seek(_pos)
        except Exception:
            try:
                fileobj.seek(0)
            except Exception:
                pass
            _end = None
        if _end is not None and _end > _max:
            raise Pan115Error(
                f"文件 {_end} 字节超过 115 简单上传 5 GiB 上限；"
                "超限文件走 multipart_upload（initupload 分片），"
                "本方法不接")

        init = self._call(
            "POST", f"{PAN115_UPLB_BASE}/3.0/sampleinitupload.php",
            data={"filename": filename, "target": f"U_1_{pid}"},
        )
        if init.get("state") is False:
            raise Pan115Error(
                f"115 简单上传初始化失败：{init.get('error') or init.get('message') or init}")
        host = init.get("host") or ""
        fields = {
            "name": filename,
            "key": init.get("object") or "",
            "policy": init.get("policy") or "",
            "OSSAccessKeyId": init.get("accessid") or "",
            "success_action_status": "200",
            "callback": init.get("callback") or "",
            "signature": init.get("signature") or "",
        }
        if not host or not all((fields["key"], fields["policy"],
                                fields["OSSAccessKeyId"], fields["callback"],
                                fields["signature"])):
            raise Pan115Error("115 简单上传初始化未返回完整 OSS 参数")
        timeout = httpx.Timeout(connect=30.0, read=300.0, write=300.0,
                                pool=30.0)
        # 长传输（上传，read/write 300s）独立 client：不占共享池的 4 条连接。
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(
                    host, data=fields,
                    files={"file": (filename, fileobj)},
                    headers={"User-Agent": self.ua,
                             "Referer": "https://115.com/"},
                )
        except httpx.HTTPError as exc:
            raise Pan115Error(f"115 简单上传请求失败：{exc}") from exc
        if resp.status_code in (401, 403):
            raise Pan115AuthError(
                f"115 简单上传返回 HTTP {resp.status_code}，Cookie 可能已失效")
        if resp.status_code >= 400:
            raise Pan115Error(f"115 简单上传返回 HTTP {resp.status_code}")
        try:
            body = resp.json()
        except Exception as exc:  # noqa: BLE001 — OSS 回调非 JSON 即失败
            raise Pan115Error("115 简单上传回调非 JSON") from exc
        if not isinstance(body, dict):
            raise Pan115Error("115 简单上传回调形态异常")
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        fid = str(data.get("file_id") or data.get("fid")
                  or data.get("id") or "")
        pickcode = str(data.get("pick_code") or data.get("pickcode")
                       or data.get("pc") or "")
        # sha1 一并透出：回调里的 sha1 是 115 对实际落盘字节算的
        # 整文件哈希，调用方据此沉淀条目 content_hash（秒传资格）。
        sha1 = str(data.get("sha1") or "")
        return {"fid": fid, "pickcode": pickcode, "sha1": sha1,
                "raw": body}

    # ---- 秒传（initupload，跨 provider 转存 Drive→115 用） ----


    # ---- 分片上传（initupload + OSS 多段，>5 GiB 全量路） ----


    # ---- 内部 ----

    def _call(self, method: str, url: str, *, params: Optional[dict] = None,
              data: Optional[dict] = None,
              deadline: Optional[float] = None) -> dict:
        """deadline：绝对截止时刻（time.time() 口径，首扫修法②）。

        来自 sidecar 的请求对象；None = 无截止（宿主直调不受影响）。
        排队上限取 min(自身上限, deadline 剩余)；拿到令牌后、发 HTTP 前
        再复查一次——guard 排队可能吃掉全部剩余时间，此时放弃。
        """
        from backend.emby_server import pan115_guard

        kind = _call_kind(url)
        try:
            pan115_guard.before_call(self.cookie, kind, deadline=deadline)
        except pan115_guard.Pan115CooldownError:
            raise   # 冷却中不发请求，也不计调用次数；类型透传（Z-1）：
                    # 上层凭 isinstance 分流「限速冷却」与「凭据失效」，
                    # 绝不能包成普通 Pan115Error 被当成连接失败/Cookie 坏。
                    # pan115_guard.Pan115CooldownError 与本模块
                    # Pan115CooldownError 是两个类（guard 无反向依赖），
                    # 用属性判定兼容两者。
        if deadline is not None and time.time() > deadline:
            # guard 排队已耗尽宿主截止时刻：不发 115 请求（修法②）。
            # AD-1：独立超时类型，不是冷却。
            raise pan115_guard.Pan115DeadlineExceeded(
                "sidecar 丢弃超时请求，未发 115")
        try:
            body = _http_json(method, url, params=params, data=data, cookie=self.cookie,
                              ua=self.ua)
        except Exception as exc:
            _bump_call(kind, error=True)
            pan115_guard.after_error(self.cookie, getattr(exc, "status", None))
            raise
        _bump_call(kind)
        pan115_guard.after_body(self.cookie, body)
        return body


#: 115 接口调用计数（Redis 哈希，跨 api / worker / ea 进程累计；进程内计数只看得到
#: 自己那一份）。只记次数不记参数，给运维按差分盯「每 5 分钟打了 115 多少次」。
PAN115_CALL_STATS_KEY = "aetrix:pan115:calls"


def _call_kind(url: str) -> str:
    path = url.split("?", 1)[0]
    if path.endswith("/files/download") or path.endswith("/downurl"):
        return "download_url"
    if path.endswith("/files"):
        return "list"
    return "other"


def _bump_call(kind: str, *, error: bool = False) -> None:
    """计数失败（Redis 不可用等）静默忽略：统计不许拖垮真实调用"""
    from backend.database import redis_call

    def _incr(client):
        pipe = client.pipeline()
        pipe.hincrby(PAN115_CALL_STATS_KEY, kind, 1)
        pipe.hincrby(PAN115_CALL_STATS_KEY, "total", 1)
        if error:
            pipe.hincrby(PAN115_CALL_STATS_KEY, "errors", 1)
        pipe.execute()

    try:
        redis_call(_incr)
    except Exception:  # noqa: BLE001
        pass


def call_stats() -> dict:
    """累计调用次数 ``{total, list, download_url, other, errors}``；Redis 不可用返回空字典"""
    from backend.database import redis_call

    try:
        raw = redis_call(lambda client: client.hgetall(PAN115_CALL_STATS_KEY), default=None)
    except Exception:  # noqa: BLE001
        raw = None
    if not raw:
        return {}
    stats = {}
    for key, value in raw.items():
        key = key.decode() if isinstance(key, bytes) else str(key)
        try:
            stats[key] = int(value)
        except (TypeError, ValueError):
            continue
    return stats


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
