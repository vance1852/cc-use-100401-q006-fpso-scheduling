"""储运与提油编排服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS lifting_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('planner','platform','vessel','dispatcher','auditor')),
    party TEXT NOT NULL CHECK(party IN ('platform','ship','operator','audit')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quality_limits (
    grade TEXT PRIMARY KEY,
    max_water_cut_percent TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES lifting_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tanks (
    tank_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    compatible_grades TEXT NOT NULL,
    max_water_cut_percent TEXT NOT NULL,
    capacity_m3 TEXT NOT NULL,
    heel_m3 TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES lifting_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vessels (
    vessel_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    compatible_grades TEXT NOT NULL,
    min_cargo_m3 TEXT NOT NULL,
    max_cargo_m3 TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES lifting_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS berth_windows (
    window_id TEXT PRIMARY KEY,
    berth_id TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    max_wave_height_m TEXT NOT NULL,
    loading_rate_m3h TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES lifting_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_windows_time ON berth_windows(starts_at, ends_at);

CREATE TABLE IF NOT EXISTS processing_batches (
    batch_id TEXT PRIMARY KEY,
    grade TEXT NOT NULL,
    water_cut_percent TEXT NOT NULL,
    daily_rate_m3 TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL REFERENCES lifting_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sea_state_versions (
    version_id TEXT PRIMARY KEY,
    issued_at TEXT NOT NULL,
    source TEXT NOT NULL,
    supersedes_version_id TEXT REFERENCES sea_state_versions(version_id),
    created_by TEXT NOT NULL REFERENCES lifting_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sea_state_observations (
    version_id TEXT NOT NULL REFERENCES sea_state_versions(version_id),
    hour_start TEXT NOT NULL,
    wave_height_m TEXT NOT NULL,
    PRIMARY KEY(version_id, hour_start)
);

CREATE TABLE IF NOT EXISTS lift_plans (
    plan_id TEXT PRIMARY KEY,
    vessel_id TEXT NOT NULL REFERENCES vessels(vessel_id),
    window_id TEXT NOT NULL REFERENCES berth_windows(window_id),
    load_grade TEXT NOT NULL,
    load_target_m3 TEXT NOT NULL,
    loaded_m3 TEXT NOT NULL DEFAULT '0',
    sea_state_version_id TEXT NOT NULL REFERENCES sea_state_versions(version_id),
    production_cut_percent TEXT NOT NULL DEFAULT '0',
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN (
        'draft','reserved','sealed','completed','cancelled','superseded','lapsed'
    )),
    revision INTEGER NOT NULL DEFAULT 1,
    frozen_priority TEXT NOT NULL DEFAULT '{}',
    lease_minutes INTEGER NOT NULL DEFAULT 30,
    lease_expires_at TEXT,
    platform_confirmed_by TEXT,
    platform_confirmed_at TEXT,
    vessel_confirmed_by TEXT,
    vessel_confirmed_at TEXT,
    sealed_at TEXT,
    evaluation_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES lifting_users(user_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_plans_state ON lift_plans(state, window_id, revision);

CREATE TABLE IF NOT EXISTS plan_entries (
    plan_id TEXT NOT NULL REFERENCES lift_plans(plan_id),
    revision INTEGER NOT NULL,
    entry_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('storage','production-cut','waitlist')),
    tank_id TEXT REFERENCES tanks(tank_id),
    batch_id TEXT REFERENCES processing_batches(batch_id),
    vessel_id_ref TEXT REFERENCES vessels(vessel_id),
    opening_level_m3 TEXT,
    rate_m3d TEXT,
    priority INTEGER,
    requested_m3 TEXT,
    cut_percent TEXT,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','released','switched','promoted','done')),
    PRIMARY KEY(plan_id, revision, entry_id)
);

CREATE TABLE IF NOT EXISTS plan_buckets (
    bucket_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES lift_plans(plan_id),
    revision INTEGER NOT NULL,
    bucket_kind TEXT NOT NULL CHECK(bucket_kind IN ('tank','window','vessel')),
    resource_id TEXT NOT NULL,
    hour_start TEXT NOT NULL,
    reserved_m3 TEXT NOT NULL,
    is_exclusive INTEGER NOT NULL CHECK(is_exclusive IN (0,1)),
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','released')),
    UNIQUE(plan_id, revision, bucket_kind, resource_id, hour_start)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_exclusive_active_bucket
ON plan_buckets(bucket_kind, resource_id, hour_start)
WHERE is_exclusive = 1 AND state = 'active';

CREATE INDEX IF NOT EXISTS idx_tank_bucket_lookup
ON plan_buckets(bucket_kind, resource_id, hour_start, state);

CREATE TABLE IF NOT EXISTS plan_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES lift_plans(plan_id),
    revision INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES lifting_users(user_id),
    detail_json TEXT NOT NULL,
    explanation_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_plan_events ON plan_events(plan_id, event_id);

CREATE TABLE IF NOT EXISTS lifting_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS lifting_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lifting_audit_entity
ON lifting_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    # ThreadingHTTPServer 会在工作线程中复用连接；WAL 与 BEGIN IMMEDIATE
    # 已串行化所有写事务，允许跨线程共享同一连接。
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def row_dict(row: sqlite3.Row | None) -> dict[object, object] | None:
    return None if row is None else dict(row)
