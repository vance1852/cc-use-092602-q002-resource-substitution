"""补偿单价、土地库存、地块资源池、提名和跨村安置替代编排的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    IndexQuote,
    Facility,
    InventoryLot,
    NominationRequest,
    RelocationApplicationInput,
    RelocationNeeds,
    ResettlementResourceInput,
    ResettlementSiteInput,
    Route,
    SupplyScenario,
    identifier,
)
from .planning import (
    AllocationRequest,
    PricePoint,
    allocate_capacity,
    canonical_json,
    decimal_text,
    delivered_after_loss,
    digest,
    effective_capacity,
    latest_streak,
    moving_average,
    quantize_volume,
    scenario_projection,
    weighted_inventory_cost,
)
from .relocation import build_candidates, infra_remaining_households, remaining_households
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {"quote.write", "catalog.write", "scenario.write", "scenario.run", "resettlement.catalog"},
    "dispatcher": {"nomination.write", "allocation.run", "transfer.write", "inventory.write", "relocation.write", "relocation.read"},
    "risk": {"outage.write", "scenario.approve", "report.read"},
    "auditor": {"report.read", "audit.read", "relocation.read"},
}


class SupplyService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM supply_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM supply_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO supply_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def record_quote(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "quote.write")
        quote = IndexQuote.from_dict(raw)
        previous = self.connection.execute(
            "SELECT quote_id,source_revision FROM market_index_quotes WHERE market_index=? AND trade_date=? "
            "ORDER BY quote_id DESC LIMIT 1",
            (quote.market_index, quote.trade_date),
        ).fetchone()
        if previous is not None and previous["source_revision"] == quote.source_revision:
            raise Conflict("同一来源修订已登记")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO market_index_quotes(market_index,trade_date,close_cny,source_revision,observed_at,"
                    "supersedes_quote_id,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        quote.market_index,
                        quote.trade_date,
                        decimal_text(quote.close_cny),
                        quote.source_revision,
                        quote.observed_at,
                        None if previous is None else previous["quote_id"],
                        actor_id,
                        self._now(),
                    ),
                )
                quote_id = int(cursor.lastrowid)
                self._audit(
                    "quote",
                    str(quote_id),
                    "quote.recorded",
                    actor_id,
                    {"market_index": quote.market_index, "trade_date": quote.trade_date},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("补偿单价版本冲突") from exc
        return {"quote_id": quote_id, "market_index": quote.market_index, "trade_date": quote.trade_date}

    def price_summary(self, market_index: str, sessions: int = 20) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT q.trade_date,q.close_cny FROM market_index_quotes q "
            "JOIN (SELECT trade_date,max(quote_id) quote_id FROM market_index_quotes "
            "WHERE market_index=? GROUP BY trade_date) latest ON latest.quote_id=q.quote_id "
            "ORDER BY q.trade_date DESC LIMIT ?",
            (market_index.upper(), sessions),
        ).fetchall()
        points = [PricePoint(row["trade_date"], Decimal(row["close_cny"])) for row in rows]
        if not points:
            raise NotFound("没有基准补偿单价")
        streak = latest_streak(points)
        average = moving_average(points, min(5, len(points)))
        latest = max(points, key=lambda item: item.trade_date)
        return {
            "market_index": market_index.upper(),
            "latest": {"trade_date": latest.trade_date, "close_cny": decimal_text(latest.close)},
            "latest_streak": None if streak is None else streak.as_dict(),
            "moving_average": None if average is None else decimal_text(average),
            "observations": len(points),
        }

    def create_facility(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        facility = Facility.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO facilities(facility_id,name,kind,timezone,capacity_mu,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        facility.facility_id,
                        facility.name,
                        facility.kind,
                        facility.timezone,
                        decimal_text(facility.capacity_mu),
                        self._now(),
                    ),
                )
                self._audit("facility", facility.facility_id, "facility.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("设施编号已经存在") from exc
        return dict(raw)

    def create_route(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        route = Route.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO routes(route_id,origin_id,destination_id,product,daily_capacity,"
                    "loss_basis_points,transit_hours,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        route.route_id,
                        route.origin_id,
                        route.destination_id,
                        route.product,
                        decimal_text(route.daily_capacity),
                        route.loss_basis_points,
                        route.transit_hours,
                        self._now(),
                    ),
                )
                self._audit("route", route.route_id, "route.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("地块资源池编号冲突或设施不存在") from exc
        return self.route(route.route_id)

    def route(self, route_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if row is None:
            raise NotFound("地块资源池不存在")
        return dict(row)

    def announce_outage(
        self,
        actor_id: str,
        route_id: str,
        starts_at: str,
        ends_at: str | None,
        capacity_percent: object,
        reason: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "outage.write")
        self.route(route_id)
        try:
            start = parse_utc(starts_at, "starts_at")
            end = None if ends_at is None else parse_utc(ends_at, "ends_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end is not None and end <= start:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        percentage = Decimal(str(capacity_percent))
        if percentage < 0 or percentage > 100:
            raise ValidationFailed("capacity_percent 必须在 0 到 100 之间")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO route_outages(route_id,starts_at,ends_at,capacity_percent,reason,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (route_id, utc_text(start), None if end is None else utc_text(end), decimal_text(percentage), reason, actor_id, self._now()),
            )
            outage_id = int(cursor.lastrowid)
            self._audit("route", route_id, "outage.announced", actor_id, {"outage_id": outage_id})
        return {"outage_id": outage_id, "route_id": route_id, "state": "announced"}

    def add_inventory_lot(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "inventory.write")
        lot = InventoryLot.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO inventory_lots(lot_id,facility_id,product,grade,quantity_mu,available_mu,"
                    "unit_cost_cny,received_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        lot.lot_id,
                        lot.facility_id,
                        lot.product,
                        lot.grade,
                        decimal_text(lot.quantity_mu),
                        decimal_text(lot.quantity_mu),
                        decimal_text(lot.unit_cost_cny),
                        lot.received_at,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("inventory_lot", lot.lot_id, "inventory.received", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("土地资源批次冲突或设施不存在") from exc
        return self.inventory_lot(lot.lot_id)

    def inventory_lot(self, lot_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if row is None:
            raise NotFound("土地资源批次不存在")
        return dict(row)

    def inventory_summary(self, facility_id: str, product: str) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT * FROM inventory_lots WHERE facility_id=? AND product=? ORDER BY received_at,lot_id",
            (facility_id, product),
        ).fetchall()
        return {"facility_id": facility_id, "product": product, **weighted_inventory_cost(rows)}

    def submit_nomination(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "nomination.write")
        nomination = NominationRequest.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM supply_idempotency WHERE scope='nomination' AND idempotency_key=?",
            (nomination.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同提名内容")
            return json.loads(stored["response_json"])
        route = self.route(nomination.route_id)
        if route["state"] != "active":
            raise InvalidState("地块资源池当前不可提名")
        response = {
            "nomination_id": nomination.nomination_id,
            "route_id": nomination.route_id,
            "state": "submitted",
            "revision": 1,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO nominations(nomination_id,route_id,shipper_id,service_date,requested_mu,"
                    "priority,idempotency_key,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        nomination.nomination_id,
                        nomination.route_id,
                        nomination.shipper_id,
                        nomination.service_date,
                        decimal_text(nomination.requested_mu),
                        nomination.priority,
                        nomination.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('nomination',?,?,?,?)",
                    (nomination.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit("nomination", nomination.nomination_id, "nomination.submitted", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("提名编号或幂等键冲突") from exc
        return response

    def _capacity_for_date(self, route: sqlite3.Row, service_date: str) -> Decimal:
        start = service_date + "T00:00:00Z"
        end = service_date + "T23:59:59Z"
        rows = self.connection.execute(
            "SELECT capacity_percent FROM route_outages WHERE route_id=? AND state IN ('announced','active') "
            "AND starts_at<=? AND (ends_at IS NULL OR ends_at>=?) ORDER BY outage_id",
            (route["route_id"], end, start),
        ).fetchall()
        percentages = [Decimal(row["capacity_percent"]) for row in rows]
        return effective_capacity(Decimal(route["daily_capacity"]), percentages)

    def allocate(self, actor_id: str, route_id: str, service_date: str) -> dict[str, Any]:
        self._require(actor_id, "allocation.run")
        route = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if route is None:
            raise NotFound("地块资源池不存在")
        nominations = self.connection.execute(
            "SELECT * FROM nominations WHERE route_id=? AND service_date=? AND state='submitted' "
            "ORDER BY priority,submitted_at,nomination_id",
            (route_id, service_date),
        ).fetchall()
        if not nominations:
            raise InvalidState("没有待分配提名")
        requests = [
            AllocationRequest(
                row["nomination_id"],
                Decimal(row["requested_mu"]),
                int(row["priority"]),
                row["submitted_at"],
            )
            for row in nominations
        ]
        available = self._capacity_for_date(route, service_date)
        input_value = [dict(row) for row in nominations]
        input_sha256 = digest({"route": dict(route), "nominations": input_value, "capacity": str(available)})
        result_rows = allocate_capacity(available, requests)
        result = {
            "route_id": route_id,
            "service_date": service_date,
            "available_capacity": decimal_text(available),
            "allocations": result_rows,
        }
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO allocation_runs(route_id,service_date,input_sha256,available_capacity,result_json,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (route_id, service_date, input_sha256, decimal_text(available), canonical_json(result), actor_id, self._now()),
            )
            for item in result_rows:
                state = "allocated" if Decimal(item["allocated_mu"]) > 0 else "cancelled"
                self.connection.execute(
                    "UPDATE nominations SET allocated_mu=?,state=?,revision=revision+1 "
                    "WHERE nomination_id=? AND state='submitted'",
                    (item["allocated_mu"], state, item["nomination_id"]),
                )
            allocation_id = int(cursor.lastrowid)
            self._audit("route", route_id, "allocation.completed", actor_id, {"allocation_id": allocation_id})
        return {"allocation_id": allocation_id, **result}

    def dispatch_transfer(
        self,
        actor_id: str,
        transfer_id: str,
        nomination_id: str,
        lot_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        self._require(actor_id, "transfer.write")
        nomination = self.connection.execute(
            "SELECT n.*,r.loss_basis_points,r.transit_hours,r.origin_id FROM nominations n "
            "JOIN routes r ON r.route_id=n.route_id WHERE n.nomination_id=?",
            (nomination_id,),
        ).fetchone()
        if nomination is None:
            raise NotFound("提名不存在")
        if nomination["state"] != "allocated" or nomination["revision"] != expected_revision:
            raise InvalidState("提名不是当前可移交版本")
        lot = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if lot is None:
            raise NotFound("土地资源批次不存在")
        allocated = Decimal(nomination["allocated_mu"])
        available = Decimal(lot["available_mu"])
        if lot["facility_id"] != nomination["origin_id"] or lot["product"] != self.route(nomination["route_id"])["product"]:
            raise Conflict("土地资源批次与地块资源池起点或土地类型不匹配")
        if available < allocated:
            raise Conflict("土地库存不足以完成分配")
        expected_delivery = delivered_after_loss(allocated, int(nomination["loss_basis_points"]))
        departed_at = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE inventory_lots SET available_mu=?,revision=revision+1 WHERE lot_id=? AND revision=?",
                (decimal_text(quantize_volume(available - allocated)), lot_id, lot["revision"]),
            )
            self.connection.execute(
                "UPDATE nominations SET state='in_transit',revision=revision+1 WHERE nomination_id=? AND revision=?",
                (nomination_id, expected_revision),
            )
            self.connection.execute(
                "INSERT INTO transfers(transfer_id,nomination_id,inventory_lot_id,surveyed_mu,"
                "expected_delivered_mu,departed_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    transfer_id,
                    nomination_id,
                    lot_id,
                    decimal_text(allocated),
                    decimal_text(expected_delivery),
                    departed_at,
                    actor_id,
                    departed_at,
                ),
            )
            self._audit("transfer", transfer_id, "transfer.dispatched", actor_id, {"nomination_id": nomination_id})
        return {
            "transfer_id": transfer_id,
            "state": "in_transit",
            "surveyed_mu": decimal_text(allocated),
            "expected_delivered_mu": decimal_text(expected_delivery),
            "expected_arrival": utc_text(parse_utc(departed_at) + timedelta(hours=int(nomination["transit_hours"]))),
        }

    def create_scenario(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "scenario.write")
        scenario = SupplyScenario.from_dict(raw)
        definition = canonical_json(raw)
        content_sha256 = hashlib.sha256(definition.encode("utf-8")).hexdigest()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_scenarios(scenario_id,name,definition_json,content_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (scenario.scenario_id, scenario.name, definition, content_sha256, actor_id, self._now()),
                )
                self._audit("scenario", scenario.scenario_id, "scenario.created", actor_id, {"sha256": content_sha256})
        except sqlite3.IntegrityError as exc:
            raise Conflict("情景编号或内容已经存在") from exc
        return {"scenario_id": scenario.scenario_id, "state": "draft", "sha256": content_sha256}

    def approve_scenario(self, actor_id: str, scenario_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "scenario.approve")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE supply_scenarios SET state='approved',revision=revision+1 "
                "WHERE scenario_id=? AND state='draft' AND revision=?",
                (scenario_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("情景不是当前草稿版本")
            self._audit("scenario", scenario_id, "scenario.approved", actor_id, {})
        return {"scenario_id": scenario_id, "state": "approved", "revision": expected_revision + 1}

    def run_scenario(self, actor_id: str, scenario_id: str, as_of_date: str) -> dict[str, Any]:
        self._require(actor_id, "scenario.run")
        row = self.connection.execute(
            "SELECT * FROM supply_scenarios WHERE scenario_id=?", (scenario_id,)
        ).fetchone()
        if row is None:
            raise NotFound("情景不存在")
        if row["state"] != "approved":
            raise InvalidState("只有已批准情景可以运行")
        scenario = SupplyScenario.from_dict(json.loads(row["definition_json"]))
        price_row = self.connection.execute(
            "SELECT close_cny FROM market_index_quotes WHERE trade_date<=? ORDER BY trade_date DESC,quote_id DESC LIMIT 1",
            (as_of_date,),
        ).fetchone()
        if price_row is None:
            raise InvalidState("截止日期没有可用补偿单价")
        routes = self.connection.execute("SELECT * FROM routes WHERE state='active' ORDER BY route_id").fetchall()
        inventory = self.connection.execute(
            "SELECT facility_id,product,sum(CAST(available_mu AS REAL)) available_mu "
            "FROM inventory_lots GROUP BY facility_id,product ORDER BY facility_id,product"
        ).fetchall()
        input_value = {
            "scenario_sha256": row["content_sha256"],
            "as_of_date": as_of_date,
            "price": price_row["close_cny"],
            "routes": [dict(item) for item in routes],
            "inventory": [dict(item) for item in inventory],
        }
        input_sha256 = digest(input_value)
        existing = self.connection.execute(
            "SELECT run_id,result_json FROM scenario_runs WHERE scenario_id=? AND as_of_date=? AND input_sha256=?",
            (scenario_id, as_of_date, input_sha256),
        ).fetchone()
        if existing is not None:
            return {"run_id": existing["run_id"], **json.loads(existing["result_json"]), "replayed": True}
        result = scenario_projection(
            current_price=Decimal(price_row["close_cny"]),
            market_index_drop_percent=scenario.market_index_drop_percent,
            routes=routes,
            inventory=inventory,
            route_capacity_changes=scenario.route_capacity_changes,
            demand_changes=scenario.demand_changes,
        )
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO scenario_runs(scenario_id,as_of_date,input_sha256,result_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (scenario_id, as_of_date, input_sha256, canonical_json(result), actor_id, self._now()),
            )
            run_id = int(cursor.lastrowid)
            self._audit("scenario", scenario_id, "scenario.executed", actor_id, {"run_id": run_id})
        return {"run_id": run_id, **result, "replayed": False}

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM supply_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}

    def _idempotency_lookup(self, scope: str, key: str, request_digest: str) -> dict[str, Any] | None:
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM supply_idempotency WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if stored is None:
            return None
        if stored["request_sha256"] != request_digest:
            raise Conflict("幂等键对应不同请求内容")
        return json.loads(stored["response_json"])

    def _idempotency_store(self, scope: str, key: str, request_digest: str, response: Mapping[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
            "VALUES(?,?,?,?,?)",
            (scope, key, request_digest, canonical_json(response), self._now()),
        )

    @staticmethod
    def _expected_revision(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValidationFailed("expected_revision 必须是正整数")
        return value

    def create_resettlement_site(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "resettlement.catalog")
        site = ResettlementSiteInput.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO resettlement_sites(site_id,name,township,village,infra_capacity_households,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        site.site_id,
                        site.name,
                        site.township,
                        site.village,
                        site.infra_capacity_households,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("resettlement_site", site.site_id, "resettlement_site.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("安置点编号已经存在") from exc
        return self.resettlement_site(site.site_id)

    def resettlement_site(self, site_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM resettlement_sites WHERE site_id=?", (site_id,)
        ).fetchone()
        if row is None:
            raise NotFound("安置点不存在")
        return dict(row)

    def set_resettlement_site_state(self, actor_id: str, site_id: str, state: object, expected_revision: object) -> dict[str, Any]:
        self._require(actor_id, "resettlement.catalog")
        if state not in ("active", "suspended"):
            raise ValidationFailed("安置点状态必须是 active 或 suspended")
        revision = self._expected_revision(expected_revision)
        self.resettlement_site(site_id)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE resettlement_sites SET state=?,revision=revision+1 WHERE site_id=? AND revision=? AND state<>?",
                (state, site_id, revision, state),
            )
            if cursor.rowcount != 1:
                raise InvalidState("安置点不是指定版本或已处于目标状态")
            self._audit("resettlement_site", site_id, "resettlement_site.state_changed", actor_id, {"state": state})
        return self.resettlement_site(site_id)

    def create_resettlement_resource(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "resettlement.catalog")
        resource = ResettlementResourceInput.from_dict(raw)
        self.resettlement_site(resource.site_id)
        eligible = "*" if resource.eligible_townships == ("*",) else ",".join(resource.eligible_townships)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO resettlement_resources(resource_id,site_id,kind,area_sqm,max_household_size,"
                    "accessible,commute_minutes,school_km,clinic_km,eligible_townships,capacity_households,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        resource.resource_id,
                        resource.site_id,
                        resource.kind,
                        decimal_text(resource.area_sqm),
                        resource.max_household_size,
                        1 if resource.accessible else 0,
                        resource.commute_minutes,
                        decimal_text(resource.school_km),
                        decimal_text(resource.clinic_km),
                        eligible,
                        resource.capacity_households,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("resettlement_resource", resource.resource_id, "resettlement_resource.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("安置资源编号冲突或安置点不存在") from exc
        return self.resettlement_resource(resource.resource_id)

    def resettlement_resource(self, resource_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM resettlement_resources WHERE resource_id=?", (resource_id,)
        ).fetchone()
        if row is None:
            raise NotFound("安置资源不存在")
        result = dict(row)
        result["accessible"] = bool(result["accessible"])
        result["eligible_townships"] = str(result["eligible_townships"]).split(",")
        return result

    def set_resettlement_resource_state(self, actor_id: str, resource_id: str, state: object, expected_revision: object) -> dict[str, Any]:
        self._require(actor_id, "resettlement.catalog")
        if state not in ("active", "suspended", "retired"):
            raise ValidationFailed("安置资源状态必须是 active、suspended 或 retired")
        revision = self._expected_revision(expected_revision)
        self.resettlement_resource(resource_id)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE resettlement_resources SET state=?,revision=revision+1 WHERE resource_id=? AND revision=? AND state<>?",
                (state, resource_id, revision, state),
            )
            if cursor.rowcount != 1:
                raise InvalidState("安置资源不是指定版本或已处于目标状态")
            self._audit("resettlement_resource", resource_id, "resettlement_resource.state_changed", actor_id, {"state": state})
        return self.resettlement_resource(resource_id)

    def register_relocation_application(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "relocation.write")
        application = RelocationApplicationInput.from_dict(raw)
        request_digest = digest(raw)
        stored = self._idempotency_lookup("relocation-application", application.idempotency_key, request_digest)
        if stored is not None:
            return stored
        response = {
            "application_id": application.application_id,
            "household_id": application.household_id,
            "state": "submitted",
            "revision": 1,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO relocation_applications(application_id,household_id,origin_township,origin_village,"
                    "household_size,needs_json,idempotency_key,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        application.application_id,
                        application.household_id,
                        application.needs.origin_township,
                        application.origin_village,
                        application.needs.household_size,
                        canonical_json(application.needs.as_dict()),
                        application.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self._idempotency_store("relocation-application", application.idempotency_key, request_digest, response)
                self._audit("relocation_application", application.application_id, "relocation.application_submitted", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("申请编号或幂等键冲突") from exc
        return response

    def _relocation_application_row(self, application_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM relocation_applications WHERE application_id=?", (application_id,)
        ).fetchone()
        if row is None:
            raise NotFound("安置申请不存在")
        return row

    @staticmethod
    def _plan_payload(plan_id: str, rank: int, state: str, revision: int, assignments: list, tradeoffs: list) -> dict[str, Any]:
        relaxed: list[str] = []
        for tradeoff in tradeoffs:
            condition = tradeoff["condition"]
            if condition not in relaxed:
                relaxed.append(condition)
        return {
            "plan_id": plan_id,
            "rank": rank,
            "state": state,
            "revision": revision,
            "assignments": assignments,
            "tradeoffs": tradeoffs,
            "relaxed_constraints": relaxed,
        }

    def generate_relocation_candidates(self, actor_id: str, application_id: str) -> dict[str, Any]:
        self._require(actor_id, "relocation.write")
        application = self._relocation_application_row(application_id)
        state = application["state"]
        if state == "moved_in":
            raise InvalidState("已经入住的家庭不能自动换房")
        if state == "confirmed":
            raise InvalidState("申请已确认安置方案，如需调整请先取消申请")
        if state == "cancelled":
            raise InvalidState("申请已取消，不能重新编排")
        needs_dict = json.loads(application["needs_json"])
        needs = RelocationNeeds.from_dict(needs_dict)
        resource_rows = self.connection.execute(
            "SELECT * FROM resettlement_resources ORDER BY resource_id"
        ).fetchall()
        site_rows = self.connection.execute("SELECT * FROM resettlement_sites ORDER BY site_id").fetchall()
        resources = [dict(row) for row in resource_rows]
        sites = {row["site_id"]: dict(row) for row in site_rows}
        input_sha256 = digest({
            "application_id": application_id,
            "needs": needs_dict,
            "resources": resources,
            "sites": [dict(row) for row in site_rows],
        })
        existing = self.connection.execute(
            "SELECT * FROM relocation_plans WHERE application_id=? AND input_sha256=? AND state='offered' "
            "ORDER BY candidate_rank",
            (application_id, input_sha256),
        ).fetchall()
        if existing:
            return {
                "application_id": application_id,
                "input_sha256": input_sha256,
                "replayed": True,
                "candidates": [
                    self._plan_payload(
                        row["plan_id"], row["candidate_rank"], row["state"], row["revision"],
                        json.loads(row["assignments_json"]), json.loads(row["tradeoffs_json"]),
                    )
                    for row in existing
                ],
                "unmet_conditions": json.loads(existing[0]["unmet_json"]),
            }
        result = build_candidates(needs, resources, sites)
        now = self._now()
        candidates: list[dict[str, Any]] = []
        with transaction(self.connection, immediate=True):
            old_plans = self.connection.execute(
                "SELECT plan_id FROM relocation_plans WHERE application_id=? AND state='offered'",
                (application_id,),
            ).fetchall()
            for old in old_plans:
                self.connection.execute(
                    "UPDATE relocation_plans SET state='superseded',revision=revision+1 WHERE plan_id=?",
                    (old["plan_id"],),
                )
                self.connection.execute(
                    "UPDATE relocation_reservations SET state='released',released_at=?,revision=revision+1 "
                    "WHERE plan_id=? AND state='offered'",
                    (now, old["plan_id"]),
                )
            for candidate in result["candidates"]:
                plan_id = f"{application_id}-{input_sha256[:12]}-{candidate['rank']}"
                self.connection.execute(
                    "INSERT INTO relocation_plans(plan_id,application_id,candidate_rank,input_sha256,"
                    "assignments_json,tradeoffs_json,unmet_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        plan_id,
                        application_id,
                        candidate["rank"],
                        input_sha256,
                        canonical_json(candidate["assignments"]),
                        canonical_json(candidate["tradeoffs"]),
                        canonical_json(result["unmet_conditions"]),
                        actor_id,
                        now,
                    ),
                )
                for assignment in candidate["assignments"]:
                    self.connection.execute(
                        "INSERT INTO relocation_reservations(reservation_id,plan_id,application_id,resource_id,"
                        "site_id,resource_revision,site_revision,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            f"{plan_id}-{assignment['resource_id']}",
                            plan_id,
                            application_id,
                            assignment["resource_id"],
                            assignment["site_id"],
                            assignment["resource_revision"],
                            assignment["site_revision"],
                            now,
                        ),
                    )
                candidates.append(
                    self._plan_payload(
                        plan_id, candidate["rank"], "offered", 1,
                        candidate["assignments"], candidate["tradeoffs"],
                    )
                )
            self.connection.execute(
                "UPDATE relocation_applications SET state='planned',revision=revision+1 "
                "WHERE application_id=? AND state IN ('submitted','planned')",
                (application_id,),
            )
            self._audit(
                "relocation_application",
                application_id,
                "relocation.candidates_generated",
                actor_id,
                {"input_sha256": input_sha256, "candidates": len(candidates)},
            )
        return {
            "application_id": application_id,
            "input_sha256": input_sha256,
            "replayed": False,
            "candidates": candidates,
            "unmet_conditions": result["unmet_conditions"],
        }

    def confirm_relocation_plan(
        self,
        actor_id: str,
        plan_id: str,
        expected_revision: object,
        idempotency_key: object,
    ) -> dict[str, Any]:
        self._require(actor_id, "relocation.write")
        revision = self._expected_revision(expected_revision)
        key = identifier(idempotency_key, "idempotency_key")
        request_digest = digest({"action": "confirm", "plan_id": plan_id, "expected_revision": revision})
        stored = self._idempotency_lookup("relocation-confirm", key, request_digest)
        if stored is not None:
            return stored
        plan = self.connection.execute(
            "SELECT * FROM relocation_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if plan is None:
            raise NotFound("安置方案不存在")
        if plan["state"] != "offered" or plan["revision"] != revision:
            raise InvalidState("安置方案不是当前可确认版本")
        application = self._relocation_application_row(plan["application_id"])
        if application["state"] == "moved_in":
            raise InvalidState("已经入住的家庭不能自动换房")
        if application["state"] != "planned":
            raise InvalidState("申请当前状态不能确认方案")
        reservations = self.connection.execute(
            "SELECT * FROM relocation_reservations WHERE plan_id=? AND state='offered' ORDER BY reservation_id",
            (plan_id,),
        ).fetchall()
        if not reservations:
            raise InvalidState("安置方案没有可确认的预留")
        with transaction(self.connection, immediate=True):
            resource_revisions: dict[str, int] = {}
            site_revisions: dict[str, int] = {}
            for reservation in reservations:
                resource_id = reservation["resource_id"]
                resource = self.connection.execute(
                    "SELECT * FROM resettlement_resources WHERE resource_id=?", (resource_id,)
                ).fetchone()
                expected_resource = resource_revisions.get(resource_id, reservation["resource_revision"])
                if resource["revision"] != expected_resource or resource["state"] != "active":
                    raise Conflict(f"房源 {resource_id} 版本已变化，整单确认失败")
                if remaining_households(resource) < 1:
                    raise Conflict(f"房源 {resource_id} 容量不足，整单确认失败")
                site_id = reservation["site_id"]
                site = self.connection.execute(
                    "SELECT * FROM resettlement_sites WHERE site_id=?", (site_id,)
                ).fetchone()
                expected_site = site_revisions.get(site_id, reservation["site_revision"])
                if site["revision"] != expected_site or site["state"] != "active":
                    raise Conflict(f"安置点 {site_id} 版本已变化，整单确认失败")
                if infra_remaining_households(site) < 1:
                    raise Conflict(f"安置点 {site_id} 基础设施容量不足，整单确认失败")
                cursor = self.connection.execute(
                    "UPDATE resettlement_resources SET reserved_households=reserved_households+1,"
                    "revision=revision+1 WHERE resource_id=? AND revision=?",
                    (resource_id, expected_resource),
                )
                if cursor.rowcount != 1:
                    raise Conflict(f"房源 {resource_id} 版本已变化，整单确认失败")
                resource_revisions[resource_id] = expected_resource + 1
                cursor = self.connection.execute(
                    "UPDATE resettlement_sites SET reserved_households=reserved_households+1,"
                    "revision=revision+1 WHERE site_id=? AND revision=?",
                    (site_id, expected_site),
                )
                if cursor.rowcount != 1:
                    raise Conflict(f"安置点 {site_id} 版本已变化，整单确认失败")
                site_revisions[site_id] = expected_site + 1
                self.connection.execute(
                    "UPDATE relocation_reservations SET state='reserved',revision=revision+1 WHERE reservation_id=?",
                    (reservation["reservation_id"],),
                )
            cursor = self.connection.execute(
                "UPDATE relocation_plans SET state='confirmed',revision=revision+1 WHERE plan_id=? AND revision=?",
                (plan_id, revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("安置方案不是当前可确认版本")
            siblings = self.connection.execute(
                "SELECT plan_id FROM relocation_plans WHERE application_id=? AND state='offered'",
                (application["application_id"],),
            ).fetchall()
            for sibling in siblings:
                self.connection.execute(
                    "UPDATE relocation_plans SET state='superseded',revision=revision+1 WHERE plan_id=?",
                    (sibling["plan_id"],),
                )
                self.connection.execute(
                    "UPDATE relocation_reservations SET state='released',released_at=?,revision=revision+1 "
                    "WHERE plan_id=? AND state='offered'",
                    (self._now(), sibling["plan_id"]),
                )
            self.connection.execute(
                "UPDATE relocation_applications SET state='confirmed',revision=revision+1 WHERE application_id=?",
                (application["application_id"],),
            )
            response = {
                "plan_id": plan_id,
                "application_id": application["application_id"],
                "state": "confirmed",
                "revision": revision + 1,
                "reservations": [
                    {
                        "reservation_id": reservation["reservation_id"],
                        "resource_id": reservation["resource_id"],
                        "site_id": reservation["site_id"],
                        "state": "reserved",
                    }
                    for reservation in reservations
                ],
            }
            self._idempotency_store("relocation-confirm", key, request_digest, response)
            self._audit(
                "relocation_plan",
                plan_id,
                "relocation.plan_confirmed",
                actor_id,
                {
                    "application_id": application["application_id"],
                    "reservations": [reservation["reservation_id"] for reservation in reservations],
                },
            )
        return response

    def move_in_application(self, actor_id: str, application_id: str, expected_revision: object) -> dict[str, Any]:
        self._require(actor_id, "relocation.write")
        revision = self._expected_revision(expected_revision)
        application = self._relocation_application_row(application_id)
        if application["state"] != "confirmed" or application["revision"] != revision:
            raise InvalidState("申请不是当前可入住版本")
        plan = self.connection.execute(
            "SELECT * FROM relocation_plans WHERE application_id=? AND state='confirmed'",
            (application_id,),
        ).fetchone()
        if plan is None:
            raise InvalidState("没有已确认的安置方案")
        reservations = self.connection.execute(
            "SELECT * FROM relocation_reservations WHERE plan_id=? AND state='reserved' ORDER BY reservation_id",
            (plan["plan_id"],),
        ).fetchall()
        if not reservations:
            raise InvalidState("没有待入住的预留")
        now = self._now()
        with transaction(self.connection, immediate=True):
            for reservation in reservations:
                self.connection.execute(
                    "UPDATE relocation_reservations SET state='delivered',delivered_at=?,revision=revision+1 "
                    "WHERE reservation_id=? AND state='reserved'",
                    (now, reservation["reservation_id"]),
                )
                self.connection.execute(
                    "UPDATE resettlement_resources SET reserved_households=reserved_households-1,"
                    "occupied_households=occupied_households+1,revision=revision+1 WHERE resource_id=?",
                    (reservation["resource_id"],),
                )
                self.connection.execute(
                    "UPDATE resettlement_sites SET reserved_households=reserved_households-1,"
                    "occupied_households=occupied_households+1,revision=revision+1 WHERE site_id=?",
                    (reservation["site_id"],),
                )
            self.connection.execute(
                "UPDATE relocation_applications SET state='moved_in',revision=revision+1 "
                "WHERE application_id=? AND revision=?",
                (application_id, revision),
            )
            self._audit(
                "relocation_application",
                application_id,
                "relocation.household_moved_in",
                actor_id,
                {"reservations": [reservation["reservation_id"] for reservation in reservations]},
            )
        return {
            "application_id": application_id,
            "state": "moved_in",
            "revision": revision + 1,
            "delivered": [reservation["reservation_id"] for reservation in reservations],
        }

    def cancel_relocation_application(self, actor_id: str, application_id: str, idempotency_key: object) -> dict[str, Any]:
        self._require(actor_id, "relocation.write")
        key = identifier(idempotency_key, "idempotency_key")
        request_digest = digest({"action": "cancel", "application_id": application_id})
        stored = self._idempotency_lookup("relocation-cancel", key, request_digest)
        if stored is not None:
            return stored
        application = self._relocation_application_row(application_id)
        if application["state"] == "cancelled":
            raise InvalidState("申请已取消")
        now = self._now()
        released: list[str] = []
        discarded: list[str] = []
        retained: list[str] = []
        with transaction(self.connection, immediate=True):
            reservations = self.connection.execute(
                "SELECT * FROM relocation_reservations WHERE application_id=? AND state IN ('offered','reserved','delivered') "
                "ORDER BY reservation_id",
                (application_id,),
            ).fetchall()
            for reservation in reservations:
                if reservation["state"] == "reserved":
                    self.connection.execute(
                        "UPDATE relocation_reservations SET state='released',released_at=?,revision=revision+1 "
                        "WHERE reservation_id=?",
                        (now, reservation["reservation_id"]),
                    )
                    self.connection.execute(
                        "UPDATE resettlement_resources SET reserved_households=reserved_households-1,"
                        "revision=revision+1 WHERE resource_id=?",
                        (reservation["resource_id"],),
                    )
                    self.connection.execute(
                        "UPDATE resettlement_sites SET reserved_households=reserved_households-1,"
                        "revision=revision+1 WHERE site_id=?",
                        (reservation["site_id"],),
                    )
                    released.append(reservation["reservation_id"])
                elif reservation["state"] == "offered":
                    self.connection.execute(
                        "UPDATE relocation_reservations SET state='released',released_at=?,revision=revision+1 "
                        "WHERE reservation_id=?",
                        (now, reservation["reservation_id"]),
                    )
                    discarded.append(reservation["reservation_id"])
                else:
                    retained.append(reservation["reservation_id"])
            self.connection.execute(
                "UPDATE relocation_plans SET state='superseded',revision=revision+1 "
                "WHERE application_id=? AND state='offered'",
                (application_id,),
            )
            self.connection.execute(
                "UPDATE relocation_applications SET state='cancelled',revision=revision+1 WHERE application_id=?",
                (application_id,),
            )
            response = {
                "application_id": application_id,
                "state": "cancelled",
                "released_reservations": released,
                "discarded_offers": discarded,
                "retained_delivered": retained,
            }
            self._idempotency_store("relocation-cancel", key, request_digest, response)
            self._audit(
                "relocation_application",
                application_id,
                "relocation.application_cancelled",
                actor_id,
                {"released": released, "retained_delivered": retained},
            )
        return response

    def relocation_lineage(self, actor_id: str, application_id: str) -> dict[str, Any]:
        self._require(actor_id, "relocation.read")
        application = self._relocation_application_row(application_id)
        plans = self.connection.execute(
            "SELECT * FROM relocation_plans WHERE application_id=? ORDER BY created_at,candidate_rank",
            (application_id,),
        ).fetchall()
        reservations = self.connection.execute(
            "SELECT r.*,res.revision AS resource_revision_current,s.revision AS site_revision_current "
            "FROM relocation_reservations r "
            "JOIN resettlement_resources res ON res.resource_id=r.resource_id "
            "JOIN resettlement_sites s ON s.site_id=r.site_id "
            "WHERE r.application_id=? ORDER BY r.reservation_id",
            (application_id,),
        ).fetchall()
        entity_ids = [application_id, *[plan["plan_id"] for plan in plans]]
        placeholders = ",".join("?" for _ in entity_ids)
        events = self.connection.execute(
            f"SELECT entity_type,entity_id,event_type,actor_id,created_at FROM supply_audit_events "
            f"WHERE entity_id IN ({placeholders}) ORDER BY event_id",
            entity_ids,
        ).fetchall()
        return {
            "application": {
                "application_id": application["application_id"],
                "household_id": application["household_id"],
                "origin_township": application["origin_township"],
                "origin_village": application["origin_village"],
                "household_size": application["household_size"],
                "state": application["state"],
                "revision": application["revision"],
                "needs": json.loads(application["needs_json"]),
            },
            "plans": [
                {
                    "plan_id": plan["plan_id"],
                    "rank": plan["candidate_rank"],
                    "state": plan["state"],
                    "revision": plan["revision"],
                    "input_sha256": plan["input_sha256"],
                    "assignments": json.loads(plan["assignments_json"]),
                    "tradeoffs": json.loads(plan["tradeoffs_json"]),
                    "unmet_conditions": json.loads(plan["unmet_json"]),
                }
                for plan in plans
            ],
            "reservations": [
                {
                    "reservation_id": reservation["reservation_id"],
                    "plan_id": reservation["plan_id"],
                    "resource_id": reservation["resource_id"],
                    "site_id": reservation["site_id"],
                    "state": reservation["state"],
                    "resource_revision_frozen": reservation["resource_revision"],
                    "resource_revision_current": reservation["resource_revision_current"],
                    "site_revision_frozen": reservation["site_revision"],
                    "site_revision_current": reservation["site_revision_current"],
                    "delivered_at": reservation["delivered_at"],
                    "released_at": reservation["released_at"],
                }
                for reservation in reservations
            ],
            "events": [dict(event) for event in events],
        }
