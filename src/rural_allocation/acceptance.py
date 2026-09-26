"""贯通补偿单价、地块资源池、土地库存、提名和情景分析的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import SupplyService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = SupplyService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
    for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
        service.create_user(user_id, user_id, role)
    for index, close in enumerate(("108", "105", "102", "100", "98", "96"), start=18):
        service.record_quote("plan", {"market_index": "PEAK_VALLEY", "trade_date": f"2026-09-{index}", "close_cny": close, "source_revision": f"rev-{index}", "observed_at": f"2026-09-{index}T21:00:00Z"})
    service.create_facility("plan", {"facility_id": "village-a", "name": "北部示范村", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_mu": "500000"})
    service.create_facility("plan", {"facility_id": "settlement-b", "name": "东部安置片区", "kind": "settlement", "timezone": "Asia/Shanghai", "capacity_mu": "800000"})
    service.create_route("plan", {"route_id": "pool-a-b", "origin_id": "village-a", "destination_id": "settlement-b", "product": "cultivated-land", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
    service.add_inventory_lot("dispatch", {"lot_id": "lot-001", "facility_id": "village-a", "product": "cultivated-land", "grade": "PEAK_VALLEY", "quantity_mu": "150000", "unit_cost_cny": "91.25", "received_at": "2026-09-24T06:00:00Z"})
    service.submit_nomination("dispatch", {"nomination_id": "nom-001", "route_id": "pool-a-b", "shipper_id": "household-east", "service_date": "2026-09-25", "requested_mu": "80000", "priority": 10, "idempotency_key": "nom-key-001"})
    allocation = service.allocate("dispatch", "pool-a-b", "2026-09-25")
    transfer = service.dispatch_transfer("dispatch", "transfer-001", "nom-001", "lot-001", 2)
    service.create_scenario("plan", {"scenario_id": "relocation-recovery", "name": "关键机组检修恢复与需求回落", "market_index_drop_percent": "9", "route_capacity_changes": {"pool-a-b": "20"}, "demand_changes": {"village-a:cultivated-land": "-5"}})
    service.approve_scenario("risk", "relocation-recovery", 1)
    scenario = service.run_scenario("plan", "relocation-recovery", "2026-09-23")
    service.create_resettlement_site("plan", {"site_id": "site-east", "name": "东部集中安置小区", "township": "青石镇", "village": "东岗村", "infra_capacity_households": 3})
    service.create_resettlement_site("plan", {"site_id": "site-west", "name": "西部周转房片区", "township": "白云镇", "village": "溪口村", "infra_capacity_households": 2})
    service.create_resettlement_resource("plan", {"resource_id": "home-east-1", "site_id": "site-east", "kind": "resettlement-home", "area_sqm": "95", "max_household_size": 5, "accessible": True, "commute_minutes": 30, "school_km": "1.2", "clinic_km": "3", "eligible_townships": ["*"]})
    service.create_resettlement_resource("plan", {"resource_id": "turnover-west-1", "site_id": "site-west", "kind": "turnover-home", "area_sqm": "70", "max_household_size": 4, "accessible": False, "commute_minutes": 50, "school_km": "4", "clinic_km": "6", "eligible_townships": ["青石镇"]})
    service.register_relocation_application("dispatch", {"application_id": "reloc-001", "household_id": "hh-001", "origin_township": "青石镇", "origin_village": "上湾村", "household_size": 4, "min_area_sqm": "80", "requires_accessible": True, "max_commute_minutes": 45, "school_radius_km": "3", "clinic_radius_km": "5", "constraints": [{"name": "accessible", "enforce": "hard"}, {"name": "school_radius"}, {"name": "clinic_radius"}, {"name": "commute"}, {"name": "min_area"}], "downgrade_scope": {"allowed_kinds": ["resettlement-home", "turnover-home"], "max_area_relax_percent": "10", "max_extra_commute_minutes": 15, "max_extra_school_km": "2", "max_extra_clinic_km": "3"}, "idempotency_key": "reloc-key-001"})
    candidates = service.generate_relocation_candidates("dispatch", "reloc-001")
    plan_id = candidates["candidates"][0]["plan_id"]
    confirmed = service.confirm_relocation_plan("dispatch", plan_id, 1, "confirm-key-001")
    confirm_replay = service.confirm_relocation_plan("dispatch", plan_id, 1, "confirm-key-001")
    moved_in = service.move_in_application("dispatch", "reloc-001", 3)
    lineage = service.relocation_lineage("audit", "reloc-001")
    service.register_relocation_application("dispatch", {"application_id": "reloc-002", "household_id": "hh-002", "origin_township": "青石镇", "origin_village": "下湾村", "household_size": 2, "min_area_sqm": "60", "requires_accessible": False, "max_commute_minutes": 60, "constraints": [{"name": "commute"}, {"name": "min_area"}], "downgrade_scope": {"allowed_kinds": ["turnover-home", "resettlement-home"], "max_area_relax_percent": "5"}, "idempotency_key": "reloc-key-002"})
    candidates_two = service.generate_relocation_candidates("dispatch", "reloc-002")
    service.confirm_relocation_plan("dispatch", candidates_two["candidates"][0]["plan_id"], 1, "confirm-key-002")
    cancelled = service.cancel_relocation_application("dispatch", "reloc-002", "cancel-key-002")
    relocation = {"candidates": len(candidates["candidates"]), "confirmed_plan": confirmed["plan_id"], "confirm_replay_equal": confirm_replay == confirmed, "moved_in": moved_in["state"], "cancelled_released": len(cancelled["released_reservations"]), "lineage_reservations": len(lineage["reservations"])}
    result = {"status": "ok", "price": service.price_summary("PEAK_VALLEY"), "allocation_id": allocation["allocation_id"], "transfer": transfer, "scenario_run_id": scenario["run_id"], "relocation": relocation, "audit": service.audit_chain("audit"), "workspace": workspace.name}
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行乡镇片区调度服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
