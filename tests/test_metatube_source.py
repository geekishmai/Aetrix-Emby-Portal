"""MetaTube（番号）元数据源离线单测：假 HTTP 服务，零真实网络。

覆盖：
- 番号抽取（真番号 / 技术 token 噪音 / 日期 / 无番号）
- 未配置 METATUBE_BASE_URL：search 抛 count_failure=False 的 SourceError，
  且配置快照里该源整体不启用（ordered() 不含 metatube）
- 配置后：search 走假 metatube 服务，正确映射 Hit；number 不精确匹配返回 None
- 非番号标题：不发任何请求（假服务计数为 0）
- 上游 500：SourceError 带 status（供熔断识别）
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from backend.emby_server.metasources import config as ms_config
from backend.emby_server.metasources import metatube, sources

PAYLOAD = {
    "data": [
        {
            "id": "ssis00001",
            "number": "SSIS-001",
            "title": "テスト作品タイトル",
            "provider": "JAV321",
            "homepage": "https://example.invalid/video/ssis00001",
            "thumb_url": "http://pics.example.invalid/ssis00001ps.jpg",
            "cover_url": "http://pics.example.invalid/ssis00001pl.jpg",
            "score": 4.5,
            "actors": ["葵つかさ"],
            "release_date": "2021-02-19T00:00:00Z",
        }
    ]
}


class FakeMetaTube:
    """本地假 metatube 服务：记录请求，按配置返回状态码/负载。"""

    def __init__(self, payload: dict | None = None, status: int = 200) -> None:
        self.payload = PAYLOAD if payload is None else payload
        self.status = status
        self.requests: list[dict] = []

    def __enter__(self) -> "FakeMetaTube":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 — http.server 约定
                parsed = urlparse(self.path)
                outer.requests.append({
                    "path": parsed.path,
                    "q": (parse_qs(parsed.query).get("q") or [""])[0],
                })
                if outer.status != 200:
                    self.send_response(outer.status)
                    self.end_headers()
                    return
                body = json.dumps(outer.payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # 静默
                return

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self._srv.server_address[1]}"
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._srv.shutdown()
        self._srv.server_close()


class _Pool:
    usable = False


class _Gate:
    def __init__(self) -> None:
        self.calls = 0

    def acquire(self) -> None:
        self.calls += 1


def _search(title: str):
    return sources.SPEC_BY_ID["metatube"].search(title, None, "movie",
                                                 pool=_Pool(), gate=_Gate())


# ---- 番号抽取 ----


@pytest.mark.parametrize("title,code", [
    ("SSIS-001", "SSIS-001"),
    ("[FHD] ssis-001 1080p", "SSIS-001"),
    ("hsoda-058", "HSODA-058"),
    ("300MIUM-1461", "300MIUM-1461"),
    ("JERA-055 中文字幕", "JERA-055"),
    ("abc123", "ABC-123"),
])
def test_extract_code_positive(title, code):
    assert metatube.extract_code(title) == code


@pytest.mark.parametrize("title", [
    "Show.S01E01.1080p.WEB-DL.x264.mkv",
    "Movie.2024.2160p.BluRay.HEVC.10bit",
    "2024-01-01 演唱会",
    "某部国产剧 第12集",
    "[Sub] 纪录片 4K HDR",
    "",
])
def test_extract_code_noise(title):
    assert metatube.extract_code(title) is None


# ---- 未配置：不启用 ----


def test_unconfigured_raises_non_counting_error(monkeypatch):
    monkeypatch.delenv(metatube.BASE_URL_ENV, raising=False)
    with pytest.raises(sources.SourceError) as exc:
        _search("SSIS-001")
    assert exc.value.count_failure is False


def test_unconfigured_source_excluded_from_snapshot(monkeypatch):
    """未配置 env：配置快照给 metatube enabled=False（刮削链路零请求）。"""
    monkeypatch.delenv(metatube.BASE_URL_ENV, raising=False)
    snap = ms_config.read_config(None, sources.SPECS)
    metatube_cfg = snap.by_id("metatube")
    assert metatube_cfg is not None
    assert metatube_cfg.enabled is False
    assert "metatube" not in [s.id for s in snap.ordered()]


def test_configured_source_included_in_snapshot(monkeypatch):
    monkeypatch.setenv(metatube.BASE_URL_ENV, "http://127.0.0.1:9")
    snap = ms_config.read_config(None, sources.SPECS)
    assert snap.by_id("metatube").enabled is True
    assert "metatube" in [s.id for s in snap.ordered()]


# ---- 配置后：真实搜索路径（假服务） ----


def test_search_maps_hit_from_fake_service(monkeypatch):
    with FakeMetaTube() as fake:
        monkeypatch.setenv(metatube.BASE_URL_ENV, fake.base + "/")
        hit = _search("SSIS-001")
    assert fake.requests == [{"path": "/v1/movies/search", "q": "SSIS-001"}]
    assert hit is not None
    assert hit.source == "metatube"
    assert hit.title == "テスト作品タイトル"
    assert hit.year == 2021
    assert hit.poster == "http://pics.example.invalid/ssis00001pl.jpg"
    assert hit.external_ids == {"metatube": "ssis00001"}
    assert hit.rating is None  # 5 分制不冒充 10 分制
    assert hit.overview == "" and hit.genres == []


def test_search_non_fanhao_title_sends_no_request(monkeypatch):
    with FakeMetaTube() as fake:
        monkeypatch.setenv(metatube.BASE_URL_ENV, fake.base)
        hit = _search("Show.S01E01.1080p.WEB-DL.x264.mkv")
    assert hit is None
    assert fake.requests == []  # 零网络请求


def test_search_number_mismatch_returns_none(monkeypatch):
    payload = {"data": [dict(PAYLOAD["data"][0], number="SSIS-999")]}
    with FakeMetaTube(payload=payload) as fake:
        monkeypatch.setenv(metatube.BASE_URL_ENV, fake.base)
        hit = _search("SSIS-001")
    assert hit is None  # 精确 number 不匹配：宁可漏配不错配


def test_search_upstream_error_carries_status(monkeypatch):
    with FakeMetaTube(status=500) as fake:
        monkeypatch.setenv(metatube.BASE_URL_ENV, fake.base)
        with pytest.raises(sources.SourceError) as exc:
            _search("SSIS-001")
    assert exc.value.status == 500
    assert exc.value.count_failure is True  # 真失败：喂熔断


def test_write_config_metatube_rate_saved(monkeypatch):
    """后台保存含 metatube 的限速：写通用 meta_source_rate_metatube 键，不许报错。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend import models as base_models
    from backend.integrations import store
    from backend.models import SystemConfig

    monkeypatch.setenv("METATUBE_BASE_URL", "http://127.0.0.1:1")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    base_models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    store.invalidate()
    try:
        ms_config.write_config(
            db, enabled=True, prefer_chinese=True,
            order=list(ms_config.DEFAULT_ORDER),
            rates={"metatube": 2.0, "bangumi": 1.0})
        row = db.query(SystemConfig).filter(
            SystemConfig.key == "meta_source_rate_metatube").first()
        assert row is not None and row.value == "2.0"
        snap = ms_config.read_config(db, sources.SPECS)
        assert snap.by_id("metatube").rate == 2.0
    finally:
        db.close()
        store.invalidate()
