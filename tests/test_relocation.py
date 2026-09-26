from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone

from rural_allocation.api import JsonApplication
from rural_allocation.clock import FrozenClock
from rural_allocation.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from rural_allocation.models import RelocationNeeds
from rural_allocation.relocation import build_candidates
from rural_allocation.service import SupplyService


class RelocationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_resettlement_site("plan", {"site_id": "site-east", "name": "东部集中安置小区", "township": "青石镇", "village": "东岗村", "infra_capacity_households": 2})
        self.service.create_resettlement_site("plan", {"site_id": "site-west", "name": "西部周转房片区", "township": "白云镇", "village": "溪口村", "infra_capacity_households": 1})
        self.service.create_resettlement_resource("plan", {"resource_id": "home-east-1", "site_id": "site-east", "kind": "resettlement-home", "area_sqm": "95", "max_household_size": 5, "accessible": True, "commute_minutes": 30, "school_km": "1.2", "clinic_km": "3", "eligible_townships": ["*"]})
        self.service.create_resettlement_resource("plan", {"resource_id": "home-east-2", "site_id": "site-east", "kind": "resettlement-home", "area_sqm": "70", "max_household_size": 4, "accessible": False, "commute_minutes": 40, "school_km": "2", "clinic_km": "4", "eligible_townships": ["*"]})
        self.service.create_resettlement_resource("plan", {"resource_id": "quota-west-1", "site_id": "site-west", "kind": "homestead-quota", "area_sqm": "120", "max_household_size": 6, "accessible": False, "commute_minutes": 55, "school_km": "6", "clinic_km": "8", "eligible_townships": ["白云镇"]})
        self.service.create_resettlement_resource("plan", {"resource_id": "turnover-west-1", "site_id": "site-west", "kind": "turnover-home", "area_sqm": "60", "max_household_size": 4, "accessible": False, "commute_minutes": 50, "school_km": "4", "clinic_km": "6", "eligible_townships": ["*"]})

    def tearDown(self) -> None:
        self.connection.close()

    def application_payload(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "application_id": "app-1",
            "household_id": "hh-1",
            "origin_township": "青石镇",
            "origin_village": "上湾村",
            "household_size": 4,
            "min_area_sqm": "80",
            "requires_accessible": True,
            "max_commute_minutes": 45,
            "school_radius_km": "3",
            "clinic_radius_km": "5",
            "constraints": [
                {"name": "accessible", "enforce": "hard"},
                {"name": "school_radius"},
                {"name": "clinic_radius"},
                {"name": "commute"},
                {"name": "min_area"},
            ],
            "downgrade_scope": {
                "allowed_kinds": ["resettlement-home", "turnover-home", "homestead-quota"],
                "max_area_relax_percent": "10",
                "max_extra_commute_minutes": 15,
                "max_extra_school_km": "2",
                "max_extra_clinic_km": "3",
            },
            "idempotency_key": "app-key-1",
        }
        payload.update(overrides)
        return payload

    def register(self, **overrides: object) -> dict[str, object]:
        return self.service.register_relocation_application("dispatch", self.application_payload(**overrides))

    def test_candidates_rank_exact_match_first_and_report_unmet(self) -> None:
        self.register()
        result = self.service.generate_relocation_candidates("dispatch", "app-1")
        self.assertFalse(result["replayed"])
        self.assertEqual(len(result["candidates"]), 1)
        top = result["candidates"][0]
        self.assertEqual(top["rank"], 1)
        self.assertEqual(top["assignments"][0]["resource_id"], "home-east-1")
        self.assertEqual(top["tradeoffs"], [])
        conditions = {entry["condition"] for entry in result["unmet_conditions"]}
        self.assertIn("accessible", conditions)
        self.assertIn("plot_restriction", conditions)
        replay = self.service.generate_relocation_candidates("dispatch", "app-1")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["candidates"][0]["plan_id"], top["plan_id"])

    def test_soft_constraints_relax_within_downgrade_scope(self) -> None:
        self.register(
            requires_accessible=False,
            min_area_sqm="75",
            constraints=[{"name": "school_radius"}, {"name": "clinic_radius"}, {"name": "commute"}, {"name": "min_area"}],
        )
        result = self.service.generate_relocation_candidates("dispatch", "app-1")
        self.assertEqual(len(result["candidates"]), 2)
        first, second = result["candidates"]
        self.assertEqual(first["assignments"][0]["resource_id"], "home-east-1")
        self.assertEqual(first["tradeoffs"], [])
        self.assertEqual(second["assignments"][0]["resource_id"], "home-east-2")
        self.assertEqual([t["condition"] for t in second["tradeoffs"]], ["min_area"])
        self.assertEqual(second["relaxed_constraints"], ["min_area"])
        conditions = {entry["condition"] for entry in result["unmet_conditions"]}
        self.assertIn("min_area", conditions)

    def test_unlisted_constraint_is_hard(self) -> None:
        needs = RelocationNeeds.from_dict({
            "household_size": 2,
            "min_area_sqm": "30",
            "requires_accessible": False,
            "max_commute_minutes": 45,
            "origin_township": "青石镇",
            "constraints": [{"name": "min_area"}],
            "downgrade_scope": {"max_extra_commute_minutes": 15},
        })
        resources = [
            {"resource_id": "r1", "site_id": "s1", "kind": "turnover-home", "area_sqm": "50", "max_household_size": 4,
             "accessible": 0, "commute_minutes": 50, "school_km": "1", "clinic_km": "1", "eligible_townships": "*",
             "capacity_households": 1, "reserved_households": 0, "occupied_households": 0, "revision": 1, "state": "active"},
        ]
        sites = {"s1": {"site_id": "s1", "state": "active", "infra_capacity_households": 1, "reserved_households": 0, "occupied_households": 0, "revision": 1}}
        result = build_candidates(needs, resources, sites)
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["unmet_conditions"][0]["condition"], "commute")
        again = build_candidates(needs, resources, sites)
        self.assertEqual(result, again)

    def test_large_household_gets_atomic_combo_plan(self) -> None:
        self.register(
            household_size=9,
            requires_accessible=False,
            min_area_sqm="30",
            constraints=[{"name": "min_area"}],
        )
        result = self.service.generate_relocation_candidates("dispatch", "app-1")
        self.assertEqual(len(result["candidates"]), 1)
        combo = result["candidates"][0]
        self.assertEqual([a["resource_id"] for a in combo["assignments"]], ["home-east-1", "home-east-2"])
        confirmed = self.service.confirm_relocation_plan("dispatch", combo["plan_id"], 1, "combo-confirm-1")
        self.assertEqual(confirmed["state"], "confirmed")
        self.assertEqual(len(confirmed["reservations"]), 2)
        self.assertEqual(self.service.resettlement_resource("home-east-1")["reserved_households"], 1)
        self.assertEqual(self.service.resettlement_resource("home-east-2")["reserved_households"], 1)
        self.assertEqual(self.service.resettlement_site("site-east")["reserved_households"], 2)

    def test_version_change_fails_whole_confirmation(self) -> None:
        self.register(
            household_size=9,
            requires_accessible=False,
            min_area_sqm="30",
            constraints=[{"name": "min_area"}],
        )
        result = self.service.generate_relocation_candidates("dispatch", "app-1")
        plan_id = result["candidates"][0]["plan_id"]
        self.service.set_resettlement_resource_state("plan", "home-east-2", "suspended", 1)
        with self.assertRaises(Conflict):
            self.service.confirm_relocation_plan("dispatch", plan_id, 1, "combo-confirm-2")
        self.assertEqual(self.service.resettlement_resource("home-east-1")["reserved_households"], 0)
        self.assertEqual(self.service.resettlement_resource("home-east-2")["reserved_households"], 0)
        self.assertEqual(self.service.resettlement_site("site-east")["reserved_households"], 0)
        plan = self.connection.execute("SELECT state FROM relocation_plans WHERE plan_id=?", (plan_id,)).fetchone()
        self.assertEqual(plan["state"], "offered")

    def test_confirm_replay_does_not_occupy_twice(self) -> None:
        self.register()
        result = self.service.generate_relocation_candidates("dispatch", "app-1")
        plan_id = result["candidates"][0]["plan_id"]
        confirmed = self.service.confirm_relocation_plan("dispatch", plan_id, 1, "confirm-key-1")
        replay = self.service.confirm_relocation_plan("dispatch", plan_id, 1, "confirm-key-1")
        self.assertEqual(confirmed, replay)
        self.assertEqual(self.service.resettlement_resource("home-east-1")["reserved_households"], 1)
        with self.assertRaises(Conflict):
            self.service.confirm_relocation_plan("dispatch", plan_id, 2, "confirm-key-1")
        with self.assertRaises(InvalidState):
            self.service.confirm_relocation_plan("dispatch", plan_id, 1, "confirm-key-2")

    def test_moved_in_household_cannot_be_swapped_and_cancel_keeps_delivered(self) -> None:
        self.register()
        result = self.service.generate_relocation_candidates("dispatch", "app-1")
        plan_id = result["candidates"][0]["plan_id"]
        self.service.confirm_relocation_plan("dispatch", plan_id, 1, "confirm-key-1")
        moved = self.service.move_in_application("dispatch", "app-1", 3)
        self.assertEqual(moved["state"], "moved_in")
        with self.assertRaises(InvalidState):
            self.service.generate_relocation_candidates("dispatch", "app-1")
        cancelled = self.service.cancel_relocation_application("dispatch", "app-1", "cancel-key-1")
        self.assertEqual(cancelled["released_reservations"], [])
        self.assertEqual(len(cancelled["retained_delivered"]), 1)
        self.assertEqual(self.service.resettlement_resource("home-east-1")["occupied_households"], 1)

    def test_cancel_releases_only_undelivered_reservations(self) -> None:
        self.register()
        result = self.service.generate_relocation_candidates("dispatch", "app-1")
        plan_id = result["candidates"][0]["plan_id"]
        self.service.confirm_relocation_plan("dispatch", plan_id, 1, "confirm-key-1")
        cancelled = self.service.cancel_relocation_application("dispatch", "app-1", "cancel-key-1")
        self.assertEqual(len(cancelled["released_reservations"]), 1)
        self.assertEqual(cancelled["retained_delivered"], [])
        self.assertEqual(self.service.resettlement_resource("home-east-1")["reserved_households"], 0)
        self.assertEqual(self.service.resettlement_site("site-east")["reserved_households"], 0)
        replay = self.service.cancel_relocation_application("dispatch", "app-1", "cancel-key-1")
        self.assertEqual(cancelled, replay)

    def test_cancel_before_confirm_discards_offers(self) -> None:
        self.register(
            requires_accessible=False,
            min_area_sqm="75",
            constraints=[{"name": "school_radius"}, {"name": "clinic_radius"}, {"name": "commute"}, {"name": "min_area"}],
        )
        result = self.service.generate_relocation_candidates("dispatch", "app-1")
        self.assertEqual(len(result["candidates"]), 2)
        cancelled = self.service.cancel_relocation_application("dispatch", "app-1", "cancel-key-1")
        self.assertEqual(cancelled["released_reservations"], [])
        self.assertEqual(len(cancelled["discarded_offers"]), 2)
        self.assertEqual(self.service.resettlement_resource("home-east-1")["reserved_households"], 0)

    def test_stale_plan_of_competing_application_fails(self) -> None:
        common = {
            "household_size": 2,
            "requires_accessible": False,
            "min_area_sqm": "30",
            "constraints": [{"name": "min_area"}],
            "downgrade_scope": {"allowed_kinds": ["resettlement-home"]},
        }
        self.register(application_id="app-a", household_id="hh-a", idempotency_key="key-a", **common)
        self.register(application_id="app-b", household_id="hh-b", idempotency_key="key-b", **common)
        plans_a = self.service.generate_relocation_candidates("dispatch", "app-a")
        plans_b = self.service.generate_relocation_candidates("dispatch", "app-b")
        top_a = plans_a["candidates"][0]
        top_b = plans_b["candidates"][0]
        self.assertEqual(top_a["assignments"][0]["resource_id"], "home-east-1")
        self.assertEqual(top_b["assignments"][0]["resource_id"], "home-east-1")
        self.service.confirm_relocation_plan("dispatch", top_a["plan_id"], 1, "confirm-a")
        with self.assertRaises(Conflict):
            self.service.confirm_relocation_plan("dispatch", top_b["plan_id"], 1, "confirm-b")
        refreshed = self.service.generate_relocation_candidates("dispatch", "app-b")
        self.assertFalse(refreshed["replayed"])
        assigned = {a["resource_id"] for c in refreshed["candidates"] for a in c["assignments"]}
        self.assertNotIn("home-east-1", assigned)
        conditions = {entry["condition"] for entry in refreshed["unmet_conditions"]}
        self.assertIn("capacity", conditions)

    def test_registration_replay_and_payload_conflict(self) -> None:
        first = self.register()
        self.assertEqual(first, self.register())
        changed = self.application_payload(min_area_sqm="90")
        with self.assertRaises(Conflict):
            self.service.register_relocation_application("dispatch", changed)

    def test_nothing_feasible_reports_unmet_conditions(self) -> None:
        self.service.create_resettlement_resource("plan", {"resource_id": "quota-east-1", "site_id": "site-east", "kind": "homestead-quota", "area_sqm": "100", "max_household_size": 6, "accessible": False, "commute_minutes": 20, "school_km": "1", "clinic_km": "1", "eligible_townships": ["青石镇"]})
        self.register(
            downgrade_scope={"allowed_kinds": ["homestead-quota"]},
        )
        result = self.service.generate_relocation_candidates("dispatch", "app-1")
        self.assertEqual(result["candidates"], [])
        conditions = {entry["condition"] for entry in result["unmet_conditions"]}
        self.assertIn("accessible", conditions)
        self.assertIn("plot_restriction", conditions)
        application = self.connection.execute("SELECT state FROM relocation_applications WHERE application_id='app-1'").fetchone()
        self.assertEqual(application["state"], "planned")

    def test_infra_capacity_blocks_candidates(self) -> None:
        self.service.set_resettlement_site_state("plan", "site-west", "suspended", 1)
        self.register(
            requires_accessible=False,
            min_area_sqm="30",
            constraints=[{"name": "min_area"}],
            downgrade_scope={"allowed_kinds": ["turnover-home", "homestead-quota"]},
        )
        result = self.service.generate_relocation_candidates("dispatch", "app-1")
        self.assertEqual(result["candidates"], [])
        conditions = {entry["condition"] for entry in result["unmet_conditions"]}
        self.assertIn("site_state", conditions)

    def test_lineage_shows_resource_pedigree(self) -> None:
        self.register()
        result = self.service.generate_relocation_candidates("dispatch", "app-1")
        plan_id = result["candidates"][0]["plan_id"]
        self.service.confirm_relocation_plan("dispatch", plan_id, 1, "confirm-key-1")
        lineage = self.service.relocation_lineage("audit", "app-1")
        self.assertEqual(lineage["application"]["state"], "confirmed")
        self.assertEqual(lineage["plans"][0]["state"], "confirmed")
        reservation = lineage["reservations"][0]
        self.assertEqual(reservation["resource_id"], "home-east-1")
        self.assertEqual(reservation["state"], "reserved")
        self.assertEqual(reservation["resource_revision_frozen"], 1)
        self.assertEqual(reservation["resource_revision_current"], 2)
        event_types = {event["event_type"] for event in lineage["events"]}
        self.assertIn("relocation.plan_confirmed", event_types)

    def test_permissions_are_enforced(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.create_resettlement_site("dispatch", {"site_id": "s-x", "name": "x", "township": "青石镇", "village": "x", "infra_capacity_households": 1})
        with self.assertRaises(Forbidden):
            self.service.register_relocation_application("plan", self.application_payload())
        with self.assertRaises(Forbidden):
            self.service.generate_relocation_candidates("risk", "app-1")
        with self.assertRaises(Forbidden):
            self.service.relocation_lineage("risk", "app-1")
        with self.assertRaises(NotFound):
            self.service.relocation_lineage("audit", "missing")

    def test_application_validation(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.register(constraints=[{"name": "unknown"}])
        with self.assertRaises(ValidationFailed):
            self.register(constraints=[{"name": "commute"}, {"name": "commute"}])
        with self.assertRaises(ValidationFailed):
            self.register(constraints=[{"name": "commute"}], max_commute_minutes=None)
        with self.assertRaises(ValidationFailed):
            self.register(constraints=[{"name": "accessible"}], requires_accessible=False)
        with self.assertRaises(ValidationFailed):
            self.register(downgrade_scope={"allowed_kinds": ["castle"]})

    def test_audit_chain_covers_relocation_events(self) -> None:
        self.register()
        result = self.service.generate_relocation_candidates("dispatch", "app-1")
        self.service.confirm_relocation_plan("dispatch", result["candidates"][0]["plan_id"], 1, "confirm-key-1")
        chain = self.service.audit_chain("audit")
        self.assertTrue(chain["valid"])


class RelocationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service = SupplyService(self.connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
        self.service.create_user("plan", "plan", "planner")
        self.service.create_user("dispatch", "dispatch", "dispatcher")
        self.service.create_user("audit", "audit", "auditor")
        self.app = JsonApplication(self.service)

    def tearDown(self) -> None:
        self.connection.close()

    def post(self, path: str, payload: dict[str, object], actor: str = "dispatch"):
        return self.app.handle("POST", path, {"X-Actor-Id": actor}, json.dumps(payload).encode("utf-8"))

    def get(self, path: str, actor: str = "audit"):
        return self.app.handle("GET", path, {"X-Actor-Id": actor})

    def test_full_relocation_flow_over_http(self) -> None:
        response = self.post("/resettlement/sites", {"site_id": "site-1", "name": "安置小区", "township": "青石镇", "village": "东岗村", "infra_capacity_households": 2}, actor="plan")
        self.assertEqual(response.status, 201)
        response = self.post("/resettlement/resources", {"resource_id": "res-1", "site_id": "site-1", "kind": "resettlement-home", "area_sqm": "90", "max_household_size": 5, "accessible": True, "commute_minutes": 30, "school_km": "1", "clinic_km": "2", "eligible_townships": ["*"]}, actor="plan")
        self.assertEqual(response.status, 201)
        response = self.post("/relocation/applications", {"application_id": "app-1", "household_id": "hh-1", "origin_township": "青石镇", "origin_village": "上湾村", "household_size": 3, "min_area_sqm": "60", "requires_accessible": True, "constraints": [{"name": "accessible", "enforce": "hard"}, {"name": "min_area"}], "idempotency_key": "key-1"})
        self.assertEqual(response.status, 201)
        response = self.post("/relocation/applications/app-1/candidates", {})
        self.assertEqual(response.status, 200)
        self.assertEqual(len(response.body["candidates"]), 1)
        plan_id = response.body["candidates"][0]["plan_id"]
        response = self.post(f"/relocation/plans/{plan_id}/confirm", {"expected_revision": 1, "idempotency_key": "ck-1"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["state"], "confirmed")
        response = self.post("/relocation/applications/app-1/move-in", {"expected_revision": 3})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["state"], "moved_in")
        response = self.get("/relocation/applications/app-1/lineage")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["reservations"][0]["state"], "delivered")
        response = self.post("/relocation/applications/app-1/cancel", {"idempotency_key": "cx-1"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["released_reservations"], [])
        self.assertEqual(len(response.body["retained_delivered"]), 1)

    def test_missing_actor_and_unknown_route(self) -> None:
        response = self.app.handle("POST", "/relocation/applications", {}, b"{}")
        self.assertEqual(response.status, 422)
        response = self.app.handle("GET", "/relocation/unknown", {"X-Actor-Id": "audit"})
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "route_not_found")


if __name__ == "__main__":
    unittest.main()
