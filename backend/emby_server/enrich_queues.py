"""识别 / 刮削两条队列（清单第 17 项，FMBY #17：enrich 主链路重构）。

机制：队列是虚拟的（抢单谓词），不是新状态值——

- 识别队列：尚未绑定（tmdb_id / imdb_id 双空且 external_ids 里任一
  在用来源键皆空，NULL 与空串都算空）的 pending 条目；产出身份绑定
  （provider / 实体 / 置信度 / 证据）。
- 刮削队列：已绑定（任一身份列非空，或 external_ids 任一在用来源键
  非空，与 identity_gate.is_bound_row 同判据）的 pending 条目；按绑定
  拉元数据快照、只填空缺。

两队列独立重试：识别段未定身份的条目走现行审查 / none 终态，不空
转 pending；刮削失败只回刮削队列（条目仍绑定，不会回炉重识别）；
识别段定下的绑定下一轮由刮削队列认领（同轮不直通，保证两段是两
次独立的认领与重试）。

总开关：env ``ENRICH_TWO_QUEUE_ENABLED``（默认 1=开）。关时抢单与
处理回单队列 + 现行写库；开时 worker 按队列分批抢单、分段处理。
本模块只含谓词与开关，不碰扫描器、播放、源适配器。
"""

from __future__ import annotations

from sqlalchemy import and_ as _and
from sqlalchemy import func as _func
from sqlalchemy import not_ as _not
from sqlalchemy import or_ as _or

from backend.emby_server import models as em
from backend.emby_server.env_util import env_int
from backend.emby_server.identity_gate import (
    external_bound_sql as _external_bound_sql,
)
from backend.emby_server.identity_gate import has_external_identity

#: 两队列总开关（默认开，FMBY #17 闭环：worker 真实抢单即按队列分批；
#: 要回单队列须显式置 0）。启动时读一次，热路径不碰 env。
TWO_QUEUE_ENABLED = env_int("ENRICH_TWO_QUEUE_ENABLED", 1, 0, 1)

#: 队列名：识别队列 / 刮削队列。
QUEUE_IDENTIFY = "identify"
QUEUE_FILL = "fill"


def two_queue_enabled() -> bool:
    """两队列是否开启（全仓读开关的唯一入口；测试 patch 本模块常量）。"""
    return TWO_QUEUE_ENABLED > 0


def _is_empty(value) -> bool:
    """身份列的“空”判定：None 或空白字符串都算空（与 _empty 同口径）。"""
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def queue_of(item) -> str:
    """条目所属队列（纯函数）：已绑定进刮削队列，否则进识别队列。

    已绑定判据与 identity_gate.is_bound_row 同源：tmdb_id / imdb_id
    任一 strip 后非空，或 external_ids 任一在用来源键非空（2026-11-02
    扩围；PA-081 之前只认两列，豆瓣-only 行永远进不了刮削队列）。
    getattr 兜底：调用方（含测试轻量替身）未必带身份字段，缺字段按
    未绑定处理（识别优先，绝不把未识别条目推进刮削队列）。
    """
    bound = not _is_empty(getattr(item, "tmdb_id", None)) or not _is_empty(
        getattr(item, "imdb_id", None)
    ) or has_external_identity(item)
    return QUEUE_FILL if bound else QUEUE_IDENTIFY


def identify_filter():
    """识别队列抢单谓词：未绑定（与 queue_of 同判据的 SQL 版）。

    tmdb_id / imdb_id 双空（NULL、空串与纯空白都算空；trim 须显式传
    空白字符集以对齐 Python strip() 的 \t\n\r\f\v）且 external_ids
    任一在用来源键皆空。外部身份部分复用
    identity_gate.external_bound_sql，不在队列模块里另写 LIKE 模式
    （横切能力只许一套）。NULL 外部身份必须显式算无身份：NOT(LIKE)
    在 NULL 上得 NULL，会把整个 AND 毒成 NULL、漏捞全部 external_ids
    为空的未绑定行。必须与 queue_of 逐字一致。
    """
    _ws = " \t\n\r\f\v"
    return _and(
        _or(
            em.MediaItem.tmdb_id.is_(None),
            _func.trim(em.MediaItem.tmdb_id, _ws) == "",
        ),
        _or(
            em.MediaItem.imdb_id.is_(None),
            _func.trim(em.MediaItem.imdb_id, _ws) == "",
        ),
        _or(
            em.MediaItem.external_ids.is_(None),
            _not(_external_bound_sql(em.MediaItem.external_ids)),
        ),
    )


def fill_filter():
    """刮削队列抢单谓词：已绑定（即识别谓词的反面，含外部身份）。"""
    return _not(identify_filter())


def queue_filter(queue: str):
    """按队列名取抢单谓词；未知队列名直接 ValueError（不静默回落）。"""
    if queue == QUEUE_IDENTIFY:
        return identify_filter()
    if queue == QUEUE_FILL:
        return fill_filter()
    raise ValueError("unknown enrich queue: %r" % (queue,))
