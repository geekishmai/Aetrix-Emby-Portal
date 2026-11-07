from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import config_self_heal, models
from backend.database import Base
from backend.integrations import branding, store


def _make_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    return engine, Session()


def test_branding_gamification_switch_default_and_toggle():
    engine, db = _make_db()
    try:
        # 1) 默认开启（保持上游行为），站名回落默认
        info_default = branding.public_info(db)
        assert info_default["gamification_enabled"] is True
        assert info_default["site_name"] == "Aetrix"
        assert info_default["logo_url"] == ""

        # 2) 后台站点与品牌配置站名「帆桥影院」、Logo 与关闭游戏化开关
        store.write_values(
            db,
            {
                "site_name": "帆桥影院",
                "site_logo_url": "/brand-logo.svg",
                "site_gamification_enabled": "false",
            },
        )
        info_off = branding.public_info(db)
        assert info_off["site_name"] == "帆桥影院"
        assert info_off["logo_url"] == "/brand-logo.svg"
        assert info_off["gamification_enabled"] is False

        # 3) config_self_heal 保留 site_gamification_enabled 不误删
        config_self_heal.heal_system_config(db)
        saved = db.query(models.SystemConfig).filter_by(key="site_gamification_enabled").first()
        assert saved is not None and saved.value == "false"

        # 4) 重新开启恢复 True
        store.write_values(db, {"site_gamification_enabled": "true"})
        assert branding.public_info(db)["gamification_enabled"] is True
    finally:
        db.close()
        engine.dispose()
