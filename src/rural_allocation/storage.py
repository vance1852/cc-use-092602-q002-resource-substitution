"""供应服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS supply_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('planner','dispatcher','risk','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_index_quotes (
    quote_id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_index TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    close_cny TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    supersedes_quote_id INTEGER REFERENCES market_index_quotes(quote_id),
    recorded_by TEXT NOT NULL REFERENCES supply_users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE(market_index, trade_date, source_revision)
);

CREATE INDEX IF NOT EXISTS idx_quotes_series
ON market_index_quotes(market_index, trade_date, quote_id);

CREATE TABLE IF NOT EXISTS facilities (
    facility_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    timezone TEXT NOT NULL,
    capacity_mu TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS routes (
    route_id TEXT PRIMARY KEY,
    origin_id TEXT NOT NULL REFERENCES facilities(facility_id),
    destination_id TEXT NOT NULL REFERENCES facilities(facility_id),
    product TEXT NOT NULL,
    daily_capacity TEXT NOT NULL,
    loss_basis_points INTEGER NOT NULL,
    transit_hours INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','suspended','retired')),
    created_at TEXT NOT NULL,
    CHECK(origin_id <> destination_id)
);

CREATE TABLE IF NOT EXISTS route_outages (
    outage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    capacity_percent TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'announced' CHECK(state IN ('announced','active','closed','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outages_route_time
ON route_outages(route_id, starts_at, ends_at);

CREATE TABLE IF NOT EXISTS inventory_lots (
    lot_id TEXT PRIMARY KEY,
    facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
    product TEXT NOT NULL,
    grade TEXT NOT NULL,
    quantity_mu TEXT NOT NULL,
    available_mu TEXT NOT NULL,
    unit_cost_cny TEXT NOT NULL,
    received_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inventory_available
ON inventory_lots(facility_id, product, received_at);

CREATE TABLE IF NOT EXISTS inventory_adjustments (
    adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id TEXT NOT NULL REFERENCES inventory_lots(lot_id),
    delta_mu TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    note TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nominations (
    nomination_id TEXT PRIMARY KEY,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    shipper_id TEXT NOT NULL,
    service_date TEXT NOT NULL,
    requested_mu TEXT NOT NULL,
    allocated_mu TEXT NOT NULL DEFAULT '0',
    delivered_mu TEXT NOT NULL DEFAULT '0',
    priority INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'submitted'
        CHECK(state IN ('submitted','allocated','in_transit','delivered','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    submitted_by TEXT NOT NULL REFERENCES supply_users(user_id),
    submitted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nominations_schedule
ON nominations(route_id, service_date, priority, submitted_at);

CREATE TABLE IF NOT EXISTS allocation_runs (
    allocation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    service_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    available_capacity TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(route_id, service_date, input_sha256)
);

CREATE TABLE IF NOT EXISTS transfers (
    transfer_id TEXT PRIMARY KEY,
    nomination_id TEXT NOT NULL UNIQUE REFERENCES nominations(nomination_id),
    inventory_lot_id TEXT NOT NULL REFERENCES inventory_lots(lot_id),
    surveyed_mu TEXT NOT NULL,
    expected_delivered_mu TEXT NOT NULL,
    departed_at TEXT NOT NULL,
    arrived_at TEXT,
    state TEXT NOT NULL DEFAULT 'in_transit' CHECK(state IN ('in_transit','delivered','disputed')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS supply_scenarios (
    scenario_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','approved','retired')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scenario_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id TEXT NOT NULL REFERENCES supply_scenarios(scenario_id),
    as_of_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(scenario_id, as_of_date, input_sha256)
);

CREATE TABLE IF NOT EXISTS supply_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS supply_audit_events (
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

CREATE INDEX IF NOT EXISTS idx_supply_audit_entity
ON supply_audit_events(entity_type, entity_id, event_id);

-- 跨村安置资源替代编排 -----------------------------------------------------

CREATE TABLE IF NOT EXISTS relocation_sites (
    site_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    township TEXT NOT NULL,
    capacity_units INTEGER NOT NULL,
    occupied_units INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    CHECK(occupied_units >= 0 AND occupied_units <= capacity_units)
);

CREATE TABLE IF NOT EXISTS relocation_resources (
    resource_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('turnover-home','resettlement-home','homestead-quota')),
    site_id TEXT REFERENCES relocation_sites(site_id),
    township TEXT NOT NULL,
    village TEXT NOT NULL,
    area_sqm TEXT NOT NULL,
    beds INTEGER NOT NULL,
    accessible INTEGER NOT NULL CHECK(accessible IN (0,1)),
    school_km TEXT NOT NULL,
    medical_km TEXT NOT NULL,
    commute_km TEXT NOT NULL,
    quality_tier INTEGER NOT NULL,
    restrictions_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'available'
        CHECK(state IN ('available','reserved','occupied','retired')),
    household_id TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_relocation_resources_pool
ON relocation_resources(kind, state, township, village);

CREATE TABLE IF NOT EXISTS relocation_applications (
    household_id TEXT PRIMARY KEY,
    head_name TEXT NOT NULL,
    origin_village TEXT NOT NULL,
    member_count INTEGER NOT NULL,
    definition_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'registered'
        CHECK(state IN ('registered','confirmed','occupied','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    registered_by TEXT NOT NULL REFERENCES supply_users(user_id),
    registered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS relocation_plans (
    plan_id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES relocation_applications(household_id),
    snapshot_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    chosen_candidate_id TEXT,
    state TEXT NOT NULL DEFAULT 'proposed'
        CHECK(state IN ('proposed','confirmed','occupied','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    cancelled_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_relocation_active_plan
ON relocation_plans(household_id) WHERE state IN ('proposed','confirmed');

CREATE TABLE IF NOT EXISTS relocation_plan_resources (
    plan_id TEXT NOT NULL REFERENCES relocation_plans(plan_id),
    candidate_id TEXT NOT NULL,
    resource_id TEXT NOT NULL REFERENCES relocation_resources(resource_id),
    expected_resource_revision INTEGER NOT NULL,
    expected_site_revision INTEGER,
    PRIMARY KEY(plan_id, candidate_id)
);

CREATE TABLE IF NOT EXISTS relocation_occupancies (
    occupancy_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES relocation_plans(plan_id),
    household_id TEXT NOT NULL,
    resource_id TEXT NOT NULL REFERENCES relocation_resources(resource_id),
    site_id TEXT REFERENCES relocation_sites(site_id),
    state TEXT NOT NULL DEFAULT 'reserved' CHECK(state IN ('reserved','occupied','released')),
    parent_occupancy_id INTEGER REFERENCES relocation_occupancies(occupancy_id),
    resource_revision INTEGER NOT NULL,
    site_revision INTEGER,
    reserved_at TEXT NOT NULL,
    occupied_at TEXT,
    released_at TEXT,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_relocation_occupancy_resource
ON relocation_occupancies(resource_id, occupancy_id);

CREATE INDEX IF NOT EXISTS idx_relocation_occupancy_household
ON relocation_occupancies(household_id, occupancy_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10)
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


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)
