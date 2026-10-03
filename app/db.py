"""SQLite 连接、建表与事务辅助。

- 单文件库，WAL + busy_timeout 应对并发发布；
- 所有修订号核对都在 BEGIN IMMEDIATE 事务里完成。
"""
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS draft_meta (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    revision    INTEGER NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- 工作草稿中的规则（精确规则或域兜底规则）
CREATE TABLE IF NOT EXISTS draft_rules (
    rule_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_type   TEXT NOT NULL CHECK (rule_type IN ('exact','domain')),
    match_key   TEXT NOT NULL,                 -- exact: 完整地址; domain: 域名字符串
    action      TEXT NOT NULL CHECK (action IN ('forward','reject')),
    position    INTEGER NOT NULL,
    UNIQUE(rule_type, match_key)
);

CREATE TABLE IF NOT EXISTS draft_targets (
    rule_id     INTEGER NOT NULL REFERENCES draft_rules(rule_id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('alias','mailbox')),
    address     TEXT NOT NULL,
    PRIMARY KEY (rule_id, position)
);

-- 已发布的不可变版本
CREATE TABLE IF NOT EXISTS versions (
    version     INTEGER PRIMARY KEY AUTOINCREMENT,
    revision    INTEGER NOT NULL,              -- 发布时对应的草稿修订号
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    rule_count  INTEGER NOT NULL,
    checksum    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS version_rules (
    version     INTEGER NOT NULL REFERENCES versions(version) ON DELETE CASCADE,
    rule_type   TEXT NOT NULL CHECK (rule_type IN ('exact','domain')),
    match_key   TEXT NOT NULL,
    action      TEXT NOT NULL CHECK (action IN ('forward','reject')),
    position    INTEGER NOT NULL,
    PRIMARY KEY (version, rule_type, match_key)
);

CREATE TABLE IF NOT EXISTS version_targets (
    version     INTEGER NOT NULL,
    rule_type   TEXT NOT NULL,
    match_key   TEXT NOT NULL,
    position    INTEGER NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('alias','mailbox')),
    address     TEXT NOT NULL,
    PRIMARY KEY (version, rule_type, match_key, position),
    FOREIGN KEY (version, rule_type, match_key)
        REFERENCES version_rules(version, rule_type, match_key) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_version_targets ON version_targets(version);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    if db_path != ":memory:":
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(db_path: str | None = None) -> None:
    """建库建表，并写入初始草稿（revision=0）。幂等。"""
    path = db_path or settings.db_path
    conn = _connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO draft_meta (id, revision) VALUES (1, 0)"
        )
        conn.commit()
    finally:
        conn.close()


@contextmanager
def get_conn(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    """普通只读/自动提交连接（每个请求一个）。"""
    conn = _connect(db_path or settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def immediate_tx(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    """开启写事务（BEGIN IMMEDIATE 立即拿写锁）。

    修订号核对、草稿写入与发布快照均在同一事务内，
    提交即原子生效；任何异常都会回滚。
    """
    conn = _connect(db_path or settings.db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
