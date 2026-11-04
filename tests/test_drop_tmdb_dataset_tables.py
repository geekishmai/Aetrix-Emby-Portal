"""PR #1414 本地 TMDB 数据集表的删除迁移。

连跑两次不报错；只删 tmdb_dataset_items / tmdb_dataset_meta；
create_all 不会把这两张表建回来。movie_requests 保持不动。

PostgreSQL 路径在设置 DROP_TEST_PG_URL=postgresql://... 时执行
（建出 SERIAL 序列后再删，确认序列语句在真 PG 上可重复跑）。
"""
import os

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import StaticPool

from backend import models  # noqa: F401  注册门户表
from backend.database import Base, _drop_retired_tmdb_dataset_tables
from backend.emby_server import models as em  # noqa: F401  注册媒体表

_ITEMS = """
CREATE TABLE tmdb_dataset_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tmdb_id VARCHAR(20) NOT NULL,
    media_type VARCHAR(20) NOT NULL,
    title VARCHAR(300) NOT NULL
)
"""
_META = """
CREATE TABLE tmdb_dataset_meta (
    id INTEGER PRIMARY KEY,
    total_rows INTEGER DEFAULT 0
)
"""
_REQUESTS = """
CREATE TABLE movie_requests (
    id INTEGER PRIMARY KEY,
    movie_name VARCHAR(255) NOT NULL
)
"""


def _sqlite():
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _seed(eng):
    with eng.begin() as conn:
        conn.execute(text(_ITEMS))
        conn.execute(text(
            "CREATE UNIQUE INDEX uq_tmdb_dataset_tmdb_type "
            "ON tmdb_dataset_items (tmdb_id, media_type)"))
        conn.execute(text(
            "CREATE INDEX idx_tmdb_dataset_title ON tmdb_dataset_items (title)"))
        conn.execute(text(
            "CREATE INDEX idx_tmdb_dataset_type_year "
            "ON tmdb_dataset_items (media_type)"))
        conn.execute(text(
            "CREATE INDEX ix_tmdb_dataset_items_tmdb_id "
            "ON tmdb_dataset_items (tmdb_id)"))
        conn.execute(text(_META))
        conn.execute(text(_REQUESTS))
        conn.execute(text(
            "INSERT INTO tmdb_dataset_items (tmdb_id, media_type, title) "
            "VALUES ('550', 'movie', 'keep-me-not')"))
        conn.execute(text(
            "INSERT INTO tmdb_dataset_meta (id, total_rows) VALUES (1, 1)"))
        conn.execute(text(
            "INSERT INTO movie_requests (movie_name) VALUES ('求片保留')"))


def test_metadata_no_longer_declares_dataset_tables():
    names = set(Base.metadata.tables)
    assert "tmdb_dataset_items" not in names
    assert "tmdb_dataset_meta" not in names
    assert "movie_requests" in names
    assert "emby_transfer_sediment_records" in names


def test_drop_twice_sqlite_leaves_other_tables():
    eng = _sqlite()
    _seed(eng)
    _drop_retired_tmdb_dataset_tables(eng)
    _drop_retired_tmdb_dataset_tables(eng)

    names = set(inspect(eng).get_table_names())
    assert "tmdb_dataset_items" not in names
    assert "tmdb_dataset_meta" not in names
    assert "movie_requests" in names
    with eng.connect() as conn:
        kept = conn.execute(text(
            "SELECT movie_name FROM movie_requests")).scalar()
    assert kept == "求片保留"

    # 模型已删：create_all 不得把退役表建回来，求片表仍在。
    Base.metadata.create_all(eng)
    names = set(inspect(eng).get_table_names())
    assert "tmdb_dataset_items" not in names
    assert "tmdb_dataset_meta" not in names
    assert "movie_requests" in names
    with eng.connect() as conn:
        kept = conn.execute(text(
            "SELECT movie_name FROM movie_requests")).scalar()
    assert kept == "求片保留"


def test_drop_on_empty_sqlite_is_noop():
    eng = _sqlite()
    _drop_retired_tmdb_dataset_tables(eng)
    _drop_retired_tmdb_dataset_tables(eng)
    assert inspect(eng).get_table_names() == []


@pytest.mark.skipif(
    not os.getenv("DROP_TEST_PG_URL", "").startswith("postgresql"),
    reason="设置 DROP_TEST_PG_URL=postgresql://... 才跑 PostgreSQL 删表",
)
def test_drop_twice_postgresql_drops_sequences():
    eng = create_engine(os.environ["DROP_TEST_PG_URL"])
    with eng.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS tmdb_dataset_items CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS tmdb_dataset_meta CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS movie_requests CASCADE"))
        conn.execute(text("DROP SEQUENCE IF EXISTS tmdb_dataset_items_id_seq"))
        conn.execute(text("DROP SEQUENCE IF EXISTS tmdb_dataset_meta_id_seq"))
        conn.execute(text(_ITEMS.replace("AUTOINCREMENT", "")))
        conn.execute(text(
            "CREATE SEQUENCE tmdb_dataset_items_id_seq OWNED BY tmdb_dataset_items.id"))
        conn.execute(text(
            "ALTER TABLE tmdb_dataset_items ALTER COLUMN id "
            "SET DEFAULT nextval('tmdb_dataset_items_id_seq')"))
        # 序列脱离表：DROP TABLE 不会带走它，必须靠 DROP SEQUENCE IF EXISTS。
        conn.execute(text(
            "ALTER SEQUENCE tmdb_dataset_items_id_seq OWNED BY NONE"))
        conn.execute(text(
            "CREATE UNIQUE INDEX uq_tmdb_dataset_tmdb_type "
            "ON tmdb_dataset_items (tmdb_id, media_type)"))
        conn.execute(text(
            "CREATE INDEX idx_tmdb_dataset_title ON tmdb_dataset_items (title)"))
        conn.execute(text(
            "CREATE INDEX idx_tmdb_dataset_type_year "
            "ON tmdb_dataset_items (media_type)"))
        conn.execute(text(
            "CREATE INDEX ix_tmdb_dataset_items_tmdb_id "
            "ON tmdb_dataset_items (tmdb_id)"))
        conn.execute(text(_META))
        conn.execute(text(
            "CREATE SEQUENCE tmdb_dataset_meta_id_seq OWNED BY tmdb_dataset_meta.id"))
        conn.execute(text(_REQUESTS))
        conn.execute(text(
            "INSERT INTO movie_requests (id, movie_name) VALUES (1, '求片保留')"))

    _drop_retired_tmdb_dataset_tables(eng)
    _drop_retired_tmdb_dataset_tables(eng)

    with eng.connect() as conn:
        tables = {row[0] for row in conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'"))}
        seqs = {row[0] for row in conn.execute(text(
            "SELECT sequencename FROM pg_sequences WHERE schemaname='public'"))}
        kept = conn.execute(text(
            "SELECT movie_name FROM movie_requests")).scalar()
    assert "tmdb_dataset_items" not in tables
    assert "tmdb_dataset_meta" not in tables
    assert "movie_requests" in tables
    assert "tmdb_dataset_items_id_seq" not in seqs
    assert "tmdb_dataset_meta_id_seq" not in seqs
    assert kept == "求片保留"
    eng.dispose()
