"""贯通补偿单价、地块资源池、土地库存、提名和情景分析的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .relocation_service import RelocationService
from .service import SupplyService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = SupplyService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
    relocation = RelocationService(connection, service.clock)
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

    # 跨村安置资源替代编排：原定小区房源不合规，邻镇周转房按降级顺位成为首选。
    relocation.create_site("plan", {"site_id": "site-ben", "name": "本镇安置点", "township": "本镇", "capacity_units": 1})
    relocation.create_site("plan", {"site_id": "site-lin", "name": "邻镇周转片区", "township": "邻镇", "capacity_units": 2})
    relocation.register_resource("plan", {"resource_id": "home-ben-1", "kind": "resettlement-home", "site_id": "site-ben", "township": "本镇", "village": "中心村", "area_sqm": "55", "beds": 3, "accessible": False, "school_km": "0.5", "medical_km": "1.0", "commute_km": "2", "quality_tier": 1})
    relocation.register_resource("plan", {"resource_id": "turn-lin-1", "kind": "turnover-home", "site_id": "site-lin", "township": "邻镇", "village": "河西村", "area_sqm": "80", "beds": 4, "accessible": True, "school_km": "1.5", "medical_km": "2.5", "commute_km": "8", "quality_tier": 1})
    relocation.register_resource("plan", {"resource_id": "plot-lin-1", "kind": "homestead-quota", "site_id": None, "township": "邻镇", "village": "河东村", "area_sqm": "120", "beds": 6, "accessible": True, "school_km": "1.2", "medical_km": "1.8", "commute_km": "6", "quality_tier": 2, "restrictions": {"eligible_villages": ["中心村"], "self_build_only": True}})
    relocation_payload = {
        "household_id": "hh-reloc-1", "head_name": "李过渡", "origin_village": "中心村", "member_count": 4,
        "constraints": [
            {"code": "household_size", "required": True},
            {"code": "accessible", "required": True},
            {"code": "min_area", "required": True, "limit": "60"},
            {"code": "school_radius", "required": True, "limit": "2"},
            {"code": "medical_radius", "required": True, "limit": "3"},
            {"code": "commute_radius", "required": False, "limit": "5"},
        ],
        "preference_order": ["resettlement-home", "turnover-home", "homestead-quota"],
        "max_downgrade": 1,
        "idempotency_key": "reloc-app-key-1",
    }
    relocation.register_application("dispatch", relocation_payload)
    relocation_plan = relocation.generate_plan("dispatch", {"plan_id": "reloc-plan-1", "household_id": "hh-reloc-1", "idempotency_key": "reloc-plan-key-1"})
    confirm_payload = {"candidate_id": relocation_plan["candidates"][0]["candidate_id"], "expected_revision": 1, "idempotency_key": "reloc-confirm-key-1"}
    relocation_confirmed = relocation.confirm_plan("dispatch", "reloc-plan-1", confirm_payload)
    relocation.confirm_plan("dispatch", "reloc-plan-1", confirm_payload)  # 重放不重复占用
    relocation_delivered = relocation.deliver_plan("dispatch", "reloc-plan-1")
    relocation_genealogy = relocation.household_genealogy("audit", "hh-reloc-1")

    result = {"status": "ok", "price": service.price_summary("PEAK_VALLEY"), "allocation_id": allocation["allocation_id"], "transfer": transfer, "scenario_run_id": scenario["run_id"], "relocation": {"plan_id": relocation_plan["plan_id"], "candidate_count": len(relocation_plan["candidates"]), "rejected_count": len(relocation_plan["rejected"]), "unmet_conditions": relocation_plan["unmet_conditions"], "chosen_resource": relocation_confirmed["resource_genealogy"]["current"]["resource"]["resource_id"], "delivered_state": relocation_delivered["state"], "lineage_depth": len(relocation_genealogy["lineages"][0]["lineage"])}, "audit": service.audit_chain("audit"), "workspace": workspace.name}
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
