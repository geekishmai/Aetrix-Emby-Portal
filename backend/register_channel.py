"""注册渠道（v2.44.0 归因）：这个号是**怎么来的**

单选口径，一个用户只有一个主渠道。归因要能直接回答运营最常问的那句
「这批号是哪来的」，所以取的是**入口凭据**，不是事后行为：

- ``code``（卡密注册）：拿注册码进来的——管理员发出去的凭据；
- ``invitation``（邀请码注册）：别人邀请进来的；
- ``admin``（管理员创建）：后台/命令行建的号（含 ``create_admin``）；
- ``open``（开放注册）：什么都没带，自己点注册进来的；
- 空串 ``""``（未记录）：**升级前的存量用户**。当时没记，不硬猜成 open，
  后台按「未记录」显示——凭空把历史数据归到某个渠道，比留空更坏。

优先级：``admin`` > ``code`` > ``invitation`` > ``open``。
同时带卡密和邀请码的注册记成 ``code``——卡密是进门的凭据，邀请关系由
``invitation_records`` 另行记账，两件事不共用一个字段。
"""

ADMIN = "admin"
CODE = "code"
FANQIAO = "fanqiao"
INVITATION = "invitation"
OPEN = "open"
UNKNOWN = ""

LABELS = {
    ADMIN: "管理员创建",
    CODE: "卡密注册",
    FANQIAO: "帆桥联动",
    INVITATION: "邀请码注册",
    OPEN: "开放注册",
    UNKNOWN: "未记录",
}

#: 下拉顺序（未记录排最后）
ALL = (ADMIN, CODE, FANQIAO, INVITATION, OPEN)

def label_of(value: str) -> str:
    return LABELS.get(value or UNKNOWN, value or UNKNOWN)


def normalize(value: str) -> str:
    """写库前归一：认不出的值当未记录，不往库里塞脏串"""
    value = (value or "").strip().lower()
    return value if value in ALL else UNKNOWN


def resolve(entry: str, invitation_applied: bool) -> str:
    """注册收尾的最终渠道——**优先级的唯一出处**

    ``entry`` 是进门时记的凭据（卡密 / 开放注册 / 管理员建号），
    ``invitation_applied`` 是邀请码**是否真的建成了关系**（码无效/被拒为 False）。
    两条纪律在这里落地：

    - 卡密与管理员建号优先于邀请码——它们是更硬的凭据；
    - 邀请码没生效**不得**记成 invitation，否则渠道分析会把
      「自己注册的人」算成「邀请来的」。
    """
    value = normalize(entry)
    if value in (ADMIN, CODE, FANQIAO, INVITATION):
        return value
    if invitation_applied:
        return INVITATION
    if value == OPEN:
        return OPEN
    return UNKNOWN


__all__ = ["ADMIN", "ALL", "CODE", "FANQIAO", "INVITATION", "LABELS", "OPEN", "UNKNOWN",
           "label_of", "normalize", "resolve"]
