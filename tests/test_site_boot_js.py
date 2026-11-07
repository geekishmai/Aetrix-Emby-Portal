from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.site import site_router
from backend.database import Base, get_db
from backend.integrations import store


def test_site_branding_no_store_and_boot_js_xss_safe():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        store.write_values(
            db,
            {
                "site_name": "帆桥</script><script>alert(1)</script>",
                "site_logo_url": "/brand-fanqiao.svg",
                "site_gamification_enabled": "false",
            },
        )
        db.commit()

        app = FastAPI()
        app.include_router(site_router)
        app.dependency_overrides[get_db] = lambda: db

        client = TestClient(app)
        r_brand = client.get("/api/site/branding")
        assert r_brand.status_code == 200
        assert "no-store" in r_brand.headers.get("cache-control", "")
        assert r_brand.json()["gamification_enabled"] is False

        r_boot = client.get("/api/site/boot.js")
        assert r_boot.status_code == 200
        assert "application/javascript" in r_boot.headers.get("content-type", "")
        assert "no-store" in r_boot.headers.get("cache-control", "")
        text = r_boot.text
        assert text.startswith("window.__AETRIX_BOOT__=")
        # 绝不允许裸 </script> 闭合逃逸
        assert "</script>" not in text.lower()
        payload_str = text[len("window.__AETRIX_BOOT__="):].strip().rstrip(";")
        parsed = json.loads(payload_str.replace(r"<\/", "</"))
        assert parsed["gamification_enabled"] is False
        assert parsed["logo_url"] == "/brand-fanqiao.svg"
    finally:
        db.close()
        engine.dispose()
