"""admin_logs.admin_user_id 存的是管理员的 web_users.id（_audit 口径）：外键必须指向
web_users。指向从没人写的 admin_users 时，PG 上所有带审计的后台写操作都会 500 回滚。"""
from backend import models


def test_admin_log_fk_points_to_web_users():
    fks = {fk.column.table.name for fk in models.AdminLog.__table__.c.admin_user_id.foreign_keys}
    assert fks == {"web_users"}


def test_admin_log_relationship_is_web_user():
    assert models.AdminLog.admin_user.property.mapper.class_ is models.WebUser
