from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone

from rural_allocation.api import JsonApplication
from rural_allocation.clock import FrozenClock
from rural_allocation.errors import Conflict, Forbidden, InvalidState
from rural_allocation.relocation_models import HouseholdApplication
from rural_allocation.relocation_planning import generate_candidates
from rural_allocation.relocation_service import RelocationService
from rural_allocation.service import SupplyService


def application(member_count: int = 4, max_downgrade: int = 1, commute_required: bool = False) -> dict:
    constraints = [
        {"code": "household_size", "required": True},
        {"code": "accessible", "required": True},
        {"code": "min_area", "required": True, "limit": "60"},
        {"code": "school_radius", "required": True, "limit": "2"},
        {"code": "medical_radius", "required": True, "limit": "3"},
        {"code": "commute_radius", "required": commute_required, "limit": "5"},
    ]
    return {
        "household_id": "hh-1",
        "head_name": "张危改",
        "origin_village": "中心村",
        "member_count": member_count,
        "constraints": constraints,
        "preference_order": ["resettlement-home", "turnover-home", "homestead-quota"],
        "max_downgrade": max_downgrade,
        "idempotency_key": "app-key-1",
    }


def resource(resource_id: str, kind: str, **overrides: object) -> dict:
    base: dict[str, object] = {
        "resource_id": resource_id,
        "kind": kind,
        "site_id": "site-1" if kind != "homestead-quota" else None,
        "township": "本镇",
        "village": "中心村",
        "area_sqm": "70",
        "beds": 4,
        "accessible": True,
        "school_km": "1",
        "medical_km": "2",
        "commute_km": "3",
        "quality_tier": 1,
        "revision": 1,
        "state": "available",
        "restrictions": {},
    }
    base.update(overrides)
    return base


class CandidatePlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = HouseholdApplication.from_dict(application()).to_snapshot()
        self.sites = [{"site_id": "site-1", "capacity_units": 2, "occupied_units": 0, "revision": 1}]

    def test_preference_order_ranks_candidates_and_records_rejection(self) -> None:
        result = generate_candidates(
            self.app,
            [
                resource("r-home", "resettlement-home"),
                resource("r-turn", "turnover-home", quality_tier=2),
                resource("r-small", "turnover-home", area_sqm="40", accessible=False),
            ],
            self.sites,
        )
        self.assertTrue(result["feasible"])
        self.assertEqual([c["resource_id"] for c in result["candidates"]], ["r-home", "r-turn"])
        self.assertEqual(result["candidates"][0]["preference_rank"], 0)
        self.assertEqual(result["candidates"][1]["downgrade_level"], 1)
        rejected = {item["resource_id"]: item["reasons"] for item in result["rejected"]}
        self.assertIn("r-small", rejected)
        self.assertEqual({reason["code"] for reason in rejected["r-small"]}, {"accessible", "min_area"})

    def test_downgrade_beyond_accepted_range_is_hard_rejected(self) -> None:
        result = generate_candidates(
            self.app,
            [resource("r-plot", "homestead-quota", accessible=True, school_km="1", medical_km="1", commute_km="2", area_sqm="100", beds=4)],
            self.sites,
        )
        self.assertFalse(result["feasible"])
        self.assertEqual(result["rejected"][0]["reasons"][0]["code"], "downgrade")

    def test_optional_constraint_becomes_compromise_not_rejection(self) -> None:
        app = HouseholdApplication.from_dict(application()).to_snapshot()
        result = generate_candidates(
            app,
            [resource("r-turn", "turnover-home", commute_km="9")],
            self.sites,
        )
        self.assertTrue(result["feasible"])
        self.assertEqual(result["candidates"][0]["compromises"][0]["code"], "commute_radius")
        codes = {item["code"] for item in result["unmet_conditions"]}
        self.assertIn("commute_radius", codes)

    def test_full_site_capacity_blocks_candidate(self) -> None:
        sites = [{"site_id": "site-1", "capacity_units": 1, "occupied_units": 1, "revision": 3}]
        result = generate_candidates(self.app, [resource("r-home", "resettlement-home")], sites)
        self.assertFalse(result["feasible"])
        self.assertEqual(result["rejected"][0]["reasons"][0]["code"], "site_capacity")

    def test_plot_restriction_rejects_ineligible_village(self) -> None:
        app = HouseholdApplication.from_dict(application(max_downgrade=2)).to_snapshot()
        result = generate_candidates(
            app,
            [resource("r-plot", "homestead-quota", accessible=True, school_km="1", medical_km="1",
                      commute_km="2", area_sqm="100", beds=4,
                      restrictions={"eligible_villages": ["其他村"], "self_build_only": True})],
            self.sites,
        )
        self.assertFalse(result["feasible"])
        self.assertEqual(result["rejected"][0]["reasons"][0]["code"], "plot_restriction")


class RelocationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))
        self.supply = SupplyService(self.connection, self.clock)
        self.service = RelocationService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
            self.supply.create_user(user_id, user_id, role)
        self.service.create_site("plan", {"site_id": "site-a", "name": "本镇安置点", "township": "本镇", "capacity_units": 1})
        self.service.create_site("plan", {"site_id": "site-b", "name": "邻镇安置点", "township": "邻镇", "capacity_units": 5})

    def tearDown(self) -> None:
        self.connection.close()

    def _resource(self, resource_id: str, kind: str, site_id: str | None, **overrides: object) -> dict:
        payload = resource(resource_id, kind, site_id=site_id, **overrides)
        payload.pop("revision")
        payload.pop("state")
        self.service.register_resource("plan", payload)
        return payload

    def _application(self, household_id: str, key: str, **overrides: object) -> dict:
        payload = application(**overrides)
        payload["household_id"] = household_id
        payload["idempotency_key"] = key
        self.service.register_application("dispatch", payload)
        return payload

    def _plan(self, plan_id: str, household_id: str, key: str) -> dict:
        return self.service.generate_plan(
            "dispatch",
            {"plan_id": plan_id, "household_id": household_id, "idempotency_key": key},
        )

    def test_application_replay_returns_same_result_and_payload_conflict(self) -> None:
        payload = self._application("hh-1", "key-1")
        again = self.service.register_application("dispatch", payload)
        self.assertEqual(again["state"], "registered")
        changed = dict(payload, member_count=5)
        with self.assertRaises(Conflict):
            self.service.register_application("dispatch", changed)

    def test_confirm_occupies_resource_and_site_atomically(self) -> None:
        self._resource("home-a", "resettlement-home", "site-a")
        self._application("hh-1", "key-1")
        plan = self._plan("plan-1", "hh-1", "plan-key-1")
        candidate = plan["candidates"][0]["candidate_id"]
        confirmed = self.service.confirm_plan(
            "dispatch", "plan-1",
            {"candidate_id": candidate, "expected_revision": 1, "idempotency_key": "confirm-1"},
        )
        self.assertEqual(confirmed["state"], "confirmed")
        self.assertEqual(confirmed["revision"], 2)
        stored = self.service.resource("home-a")
        self.assertEqual(stored["state"], "reserved")
        self.assertEqual(stored["household_id"], "hh-1")
        site = self.connection.execute("SELECT * FROM relocation_sites WHERE site_id='site-a'").fetchone()
        self.assertEqual(site["occupied_units"], 1)
        self.assertEqual(site["revision"], 2)
        genealogy = confirmed["resource_genealogy"]
        self.assertEqual(genealogy["current"]["resource"]["resource_id"], "home-a")
        self.assertEqual(genealogy["current"]["state"], "reserved")
        self.assertEqual(len(genealogy["lineage"]), 1)

    def test_confirm_replay_does_not_double_occupy(self) -> None:
        self._resource("home-a", "resettlement-home", "site-a")
        self._application("hh-1", "key-1")
        self._plan("plan-1", "hh-1", "plan-key-1")
        confirm_payload = {"candidate_id": "c1", "expected_revision": 1, "idempotency_key": "confirm-1"}
        first = self.service.confirm_plan("dispatch", "plan-1", confirm_payload)
        second = self.service.confirm_plan("dispatch", "plan-1", confirm_payload)
        self.assertEqual(first, second)
        occupancies = self.connection.execute(
            "SELECT count(*) c FROM relocation_occupancies WHERE plan_id='plan-1'"
        ).fetchone()
        self.assertEqual(occupancies["c"], 1)
        self.assertEqual(self.service.resource("home-a")["revision"], 2)

    def test_any_version_change_fails_whole_confirmation(self) -> None:
        self._resource("home-a", "resettlement-home", "site-a")
        self._application("hh-1", "key-1")
        self._plan("plan-1", "hh-1", "plan-key-1")
        # 方案生成后房源版本被其他业务改变（例如信息修订）。
        self.connection.execute(
            "UPDATE relocation_resources SET revision=2,quality_tier=2 WHERE resource_id='home-a'"
        )
        with self.assertRaises(InvalidState) as ctx:
            self.service.confirm_plan(
                "dispatch", "plan-1",
                {"candidate_id": "c1", "expected_revision": 1, "idempotency_key": "confirm-stale"},
            )
        self.assertIn("房源版本", str(ctx.exception))
        # 整单失败：没有任何预留或谱系落库。
        self.assertEqual(self.service.resource("home-a")["state"], "available")
        self.assertEqual(
            self.connection.execute("SELECT count(*) c FROM relocation_occupancies").fetchone()["c"], 0
        )
        site = self.connection.execute("SELECT * FROM relocation_sites WHERE site_id='site-a'").fetchone()
        self.assertEqual(site["occupied_units"], 0)
        self.assertEqual(site["revision"], 1)

    def test_resource_taken_by_another_household_whole_confirm_rolls_back(self) -> None:
        self._resource("home-a", "resettlement-home", "site-a")
        self._application("hh-1", "key-1")
        self._application("hh-2", "key-2")
        self._plan("plan-1", "hh-1", "plan-key-1")
        self._plan("plan-2", "hh-2", "plan-key-2")
        self.service.confirm_plan(
            "dispatch", "plan-1",
            {"candidate_id": "c1", "expected_revision": 1, "idempotency_key": "confirm-1"},
        )
        with self.assertRaises(InvalidState):
            self.service.confirm_plan(
                "dispatch", "plan-2",
                {"candidate_id": "c1", "expected_revision": 1, "idempotency_key": "confirm-2"},
            )
        self.assertEqual(
            self.connection.execute("SELECT count(*) c FROM relocation_occupancies").fetchone()["c"], 1
        )

    def test_site_capacity_full_blocks_confirm_without_partial_occupation(self) -> None:
        self._resource("home-a", "resettlement-home", "site-a")
        self._application("hh-1", "key-1")
        self._plan("plan-1", "hh-1", "plan-key-1")
        # 容量被其他系统占用至满，版本同时推进。
        self.connection.execute(
            "UPDATE relocation_sites SET occupied_units=1,revision=2 WHERE site_id='site-a'"
        )
        with self.assertRaises(InvalidState):
            self.service.confirm_plan(
                "dispatch", "plan-1",
                {"candidate_id": "c1", "expected_revision": 1, "idempotency_key": "confirm-full"},
            )
        self.assertEqual(self.service.resource("home-a")["state"], "available")

    def test_cancel_releases_only_undelivered_reservation_and_replay_is_safe(self) -> None:
        self._resource("home-a", "resettlement-home", "site-a")
        self._application("hh-1", "key-1")
        self._plan("plan-1", "hh-1", "plan-key-1")
        self.service.confirm_plan(
            "dispatch", "plan-1",
            {"candidate_id": "c1", "expected_revision": 1, "idempotency_key": "confirm-1"},
        )
        cancel_payload = {"idempotency_key": "cancel-1"}
        first = self.service.cancel_application("dispatch", "hh-1", cancel_payload)
        self.assertEqual(first["state"], "cancelled")
        self.assertEqual(first["released_resources"][0]["resource_id"], "home-a")
        self.assertEqual(self.service.resource("home-a")["state"], "available")
        self.assertIsNone(self.service.resource("home-a")["household_id"])
        site = self.connection.execute("SELECT * FROM relocation_sites WHERE site_id='site-a'").fetchone()
        self.assertEqual(site["occupied_units"], 0)
        # 重放取消不能重复释放（容量不能被扣成负数）。
        second = self.service.cancel_application("dispatch", "hh-1", cancel_payload)
        self.assertEqual(first, second)
        site_after = self.connection.execute("SELECT * FROM relocation_sites WHERE site_id='site-a'").fetchone()
        self.assertEqual(site_after["occupied_units"], 0)
        self.assertEqual(site_after["revision"], 3)

    def test_delivered_household_cannot_cancel_or_replan(self) -> None:
        self._resource("home-a", "resettlement-home", "site-a")
        self._application("hh-1", "key-1")
        self._plan("plan-1", "hh-1", "plan-key-1")
        self.service.confirm_plan(
            "dispatch", "plan-1",
            {"candidate_id": "c1", "expected_revision": 1, "idempotency_key": "confirm-1"},
        )
        self.service.deliver_plan("dispatch", "plan-1")
        with self.assertRaises(InvalidState):
            self.service.cancel_application("dispatch", "hh-1", {"idempotency_key": "cancel-1"})
        with self.assertRaises(InvalidState):
            self._plan("plan-1b", "hh-1", "plan-key-1b")
        # 重复交付保持幂等。
        again = self.service.deliver_plan("dispatch", "plan-1")
        self.assertEqual(again["state"], "occupied")

    def test_regenerating_plan_cancels_previous_proposal(self) -> None:
        self._resource("home-a", "resettlement-home", "site-a")
        self._application("hh-1", "key-1")
        first = self._plan("plan-1", "hh-1", "plan-key-1")
        second = self._plan("plan-2", "hh-1", "plan-key-2")
        self.assertTrue(second["feasible"])
        old = self.connection.execute("SELECT state FROM relocation_plans WHERE plan_id='plan-1'").fetchone()
        self.assertEqual(old["state"], "cancelled")
        # 旧方案不能再确认。
        with self.assertRaises(InvalidState):
            self.service.confirm_plan(
                "dispatch", "plan-1",
                {"candidate_id": first["candidates"][0]["candidate_id"], "expected_revision": 1, "idempotency_key": "old"},
            )

    def test_cross_township_substitution_is_offered_in_rank_order(self) -> None:
        self._resource("home-local", "resettlement-home", "site-a", area_sqm="55", accessible=False)
        self._resource("turnover-neighbor", "turnover-home", "site-b", township="邻镇", village="河西村",
                       area_sqm="80", school_km="1.5", medical_km="2.5", commute_km="4")
        self._application("hh-1", "key-1")
        plan = self._plan("plan-1", "hh-1", "plan-key-1")
        self.assertEqual([c["resource_id"] for c in plan["candidates"]], ["turnover-neighbor"])
        self.assertEqual(plan["candidates"][0]["downgrade_level"], 1)
        self.assertIn("home-local", {item["resource_id"] for item in plan["rejected"]})

    def test_permissions_are_enforced(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.create_site("dispatch", {"site_id": "x", "name": "x", "township": "t", "capacity_units": 1})
        with self.assertRaises(Forbidden):
            self.service.register_application("audit", application())
        with self.assertRaises(Forbidden):
            self.service.generate_plan("risk", {"plan_id": "p", "household_id": "h", "idempotency_key": "k"})

    def test_api_routes_relocation_flow(self) -> None:
        self._resource("home-a", "resettlement-home", "site-a")
        app = JsonApplication(self.supply, self.service)
        created = app.handle("POST", "/relocation/applications", {"X-Actor-Id": "dispatch"},
                             json.dumps(application(), ensure_ascii=False).encode("utf-8"))
        self.assertEqual(created.status, 201)
        plan = app.handle("POST", "/relocation/plans", {"X-Actor-Id": "dispatch"},
                          json.dumps(
                              {"plan_id": "plan-1", "household_id": "hh-1", "idempotency_key": "plan-key-1"},
                              ensure_ascii=False).encode("utf-8"))
        self.assertEqual(plan.status, 201)
        detail = app.handle("GET", "/relocation/plans/plan-1", {"X-Actor-Id": "audit"})
        self.assertEqual(detail.status, 200)
        self.assertTrue(detail.body["feasible"])


if __name__ == "__main__":
    unittest.main()
