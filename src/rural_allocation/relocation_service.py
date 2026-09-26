"""跨村安置资源替代编排的事务用例。

与 SupplyService 共用同一 SQLite 连接、账号体系与审计哈希链：
- 候选方案在单个 IMMEDIATE 事务内基于冻结快照生成；
- 确认时对方案、房源和安置点容量逐一做乐观版本校验，任一版本变化即整单失败；
- 预留（reserved）与入住（occupied）分离，取消只释放尚未交付的预留；
- 所有写操作按 (scope, idempotency_key) 记录请求摘要与响应，重放不重复占用。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .clock import SystemClock, utc_text
from .models import identifier as parse_identifier
from .planning import canonical_json, digest
from .relocation_models import (
    RESOURCE_KIND_LABELS,
    HouseholdApplication,
    RelocationResource,
    RelocationSite,
)
from .relocation_planning import frozen_snapshot_digest, generate_candidates
from .storage import transaction
from .service import ROLE_PERMISSIONS, SupplyService


RELOCATION_PERMISSIONS = {
    "planner": {"relocation.catalog.write"},
    "dispatcher": {
        "relocation.application.write",
        "relocation.plan.run",
        "relocation.plan.confirm",
        "relocation.plan.deliver",
        "relocation.application.cancel",
    },
}


class RelocationService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        # 复用 SupplyService 的账号、权限与审计哈希链，不重复初始化模式。
        self._supply = SupplyService(connection, self.clock, initialize_schema=False)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _require(self, actor_id: str, permission: str) -> sqlite3.Row:
        user = self._supply._user(actor_id)
        granted = ROLE_PERMISSIONS.get(user["role"], set()) | RELOCATION_PERMISSIONS.get(
            user["role"], set()
        )
        if permission not in granted:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(self, entity_type: str, entity_id: str, event_type: str, actor_id: str, payload: Mapping[str, Any]) -> None:
        self._supply._audit(entity_type, entity_id, event_type, actor_id, payload)

    def _replay(self, scope: str, key: str, raw: Mapping[str, Any]) -> dict[str, Any] | None:
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM supply_idempotency WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if stored is None:
            return None
        if stored["request_sha256"] != request_digest:
            raise Conflict("幂等键对应不同的业务请求内容")
        return json.loads(stored["response_json"])

    def _remember(self, scope: str, key: str, raw: Mapping[str, Any], response: Mapping[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
            "VALUES(?,?,?,?,?)",
            (scope, key, digest(raw), canonical_json(response), self._now()),
        )

    # -- 基础设施目录 -------------------------------------------------------

    def create_site(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "relocation.catalog.write")
        site = RelocationSite.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO relocation_sites(site_id,name,township,capacity_units,occupied_units,"
                    "revision,created_by,created_at) VALUES(?,?,?,?,0,1,?,?)",
                    (site.site_id, site.name, site.township, site.capacity_units, actor_id, self._now()),
                )
                self._audit("relocation_site", site.site_id, "relocation.site.created", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("安置点编号已经存在") from exc
        return {
            "site_id": site.site_id,
            "name": site.name,
            "township": site.township,
            "capacity_units": site.capacity_units,
            "occupied_units": 0,
            "revision": 1,
        }

    def register_resource(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "relocation.catalog.write")
        resource = RelocationResource.from_dict(raw)
        if resource.site_id is not None and self._site(resource.site_id) is None:
            raise NotFound("安置点不存在")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO relocation_resources(resource_id,kind,site_id,township,village,area_sqm,beds,"
                    "accessible,school_km,medical_km,commute_km,quality_tier,restrictions_json,state,revision,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'available',1,?,?)",
                    (
                        resource.resource_id,
                        resource.kind,
                        resource.site_id,
                        resource.township,
                        resource.village,
                        format(resource.area_sqm, "f"),
                        resource.beds,
                        1 if resource.accessible else 0,
                        format(resource.school_km, "f"),
                        format(resource.medical_km, "f"),
                        format(resource.commute_km, "f"),
                        resource.quality_tier,
                        canonical_json(dict(resource.restrictions)),
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("relocation_resource", resource.resource_id, "relocation.resource.registered", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("安置资源编号冲突或安置点不存在") from exc
        return self.resource(resource.resource_id)

    def resource(self, resource_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM relocation_resources WHERE resource_id=?", (resource_id,)
        ).fetchone()
        if row is None:
            raise NotFound("安置资源不存在")
        return self._resource_dict(row)

    def _site(self, site_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM relocation_sites WHERE site_id=?", (site_id,)
        ).fetchone()

    @staticmethod
    def _resource_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["accessible"] = bool(row["accessible"])
        data["restrictions"] = json.loads(row["restrictions_json"])
        data["kind_label"] = RESOURCE_KIND_LABELS[row["kind"]]
        return data

    # -- 家庭申请 -----------------------------------------------------------

    def register_application(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "relocation.application.write")
        application = HouseholdApplication.from_dict(raw)
        response: dict[str, Any] = {
            "household_id": application.household_id,
            "state": "registered",
            "revision": 1,
            "constraints": [item.to_dict() for item in application.constraints],
            "preference_order": list(application.preference_order),
            "max_downgrade": application.max_downgrade,
        }
        with transaction(self.connection, immediate=True):
            replay = self._replay("relocation.application", application.idempotency_key, raw)
            if replay is not None:
                return replay
            try:
                self.connection.execute(
                    "INSERT INTO relocation_applications(household_id,head_name,origin_village,member_count,"
                    "definition_json,state,revision,idempotency_key,registered_by,registered_at) "
                    "VALUES(?,?,?,?,?,'registered',1,?,?,?)",
                    (
                        application.household_id,
                        application.head_name,
                        application.origin_village,
                        application.member_count,
                        canonical_json(dict(raw)),
                        application.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self._remember("relocation.application", application.idempotency_key, raw, response)
            except sqlite3.IntegrityError as exc:
                raise Conflict("家庭申请编号或幂等键冲突") from exc
            self._audit(
                "relocation_application",
                application.household_id,
                "relocation.application.registered",
                actor_id,
                {"constraints": response["constraints"], "preference_order": response["preference_order"]},
            )
        return response

    def _application_row(self, household_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM relocation_applications WHERE household_id=?", (household_id,)
        ).fetchone()
        if row is None:
            raise NotFound("家庭安置申请不存在")
        return row

    # -- 候选方案生成（冻结快照） -------------------------------------------

    def generate_plan(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "relocation.plan.run")
        plan_id = self._identifier(raw, "plan_id")
        household_id = self._identifier(raw, "household_id")
        idempotency_key = self._identifier(raw, "idempotency_key")
        with transaction(self.connection, immediate=True):
            replay = self._replay("relocation.plan", idempotency_key, raw)
            if replay is not None:
                return replay
            application_row = self._application_row(household_id)
            if application_row["state"] in ("confirmed", "occupied"):
                raise InvalidState("家庭已有确认方案或已经入住，不能重新编排")
            if application_row["state"] == "cancelled":
                raise InvalidState("家庭申请已取消")
            application = HouseholdApplication.from_dict(json.loads(application_row["definition_json"]))

            # 重新编排时先作废同一家庭尚未确认的旧方案（旧方案尚未占用任何资源）。
            self.connection.execute(
                "UPDATE relocation_plans SET state='cancelled',cancelled_at=? "
                "WHERE household_id=? AND state='proposed'",
                (self._now(), household_id),
            )

            resources = [
                self._resource_dict(row)
                for row in self.connection.execute(
                    "SELECT * FROM relocation_resources WHERE state='available' ORDER BY resource_id"
                ).fetchall()
            ]
            sites = [dict(row) for row in self.connection.execute("SELECT * FROM relocation_sites ORDER BY site_id").fetchall()]
            snapshot = generate_candidates(application.to_snapshot(), resources, sites)
            snapshot_sha256 = frozen_snapshot_digest(application.to_snapshot(), resources, sites)
            now = self._now()
            try:
                self.connection.execute(
                    "INSERT INTO relocation_plans(plan_id,household_id,snapshot_sha256,result_json,state,"
                    "revision,idempotency_key,created_by,created_at) VALUES(?,?,?,?,'proposed',1,?,?,?)",
                    (
                        plan_id,
                        household_id,
                        snapshot_sha256,
                        canonical_json(snapshot),
                        idempotency_key,
                        actor_id,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("方案编号或幂等键冲突") from exc
            self.connection.executemany(
                "INSERT INTO relocation_plan_resources(plan_id,candidate_id,resource_id,"
                "expected_resource_revision,expected_site_revision) VALUES(?,?,?,?,?)",
                [
                    (
                        plan_id,
                        candidate["candidate_id"],
                        candidate["resource_id"],
                        candidate["resource_revision"],
                        candidate["site_revision"],
                    )
                    for candidate in snapshot["candidates"]
                ],
            )
            response = {
                "plan_id": plan_id,
                "household_id": household_id,
                "state": "proposed",
                "revision": 1,
                "snapshot_sha256": snapshot_sha256,
                "generated_at": now,
                "feasible": snapshot["feasible"],
                "candidates": snapshot["candidates"],
                "rejected": snapshot["rejected"],
                "unmet_conditions": snapshot["unmet_conditions"],
            }
            self._remember("relocation.plan", idempotency_key, raw, response)
            self._audit(
                "relocation_plan",
                plan_id,
                "relocation.plan.generated",
                actor_id,
                {
                    "household_id": household_id,
                    "snapshot_sha256": snapshot_sha256,
                    "candidate_count": len(snapshot["candidates"]),
                    "rejected_count": len(snapshot["rejected"]),
                },
            )
        return response

    # -- 原子确认 -----------------------------------------------------------

    def confirm_plan(self, actor_id: str, plan_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "relocation.plan.confirm")
        candidate_id = self._identifier(raw, "candidate_id")
        idempotency_key = self._identifier(raw, "idempotency_key")
        expected_revision = self._revision(raw)
        replay_key = f"{plan_id}:{idempotency_key}"
        replay_body = {"plan_id": plan_id, **raw}
        with transaction(self.connection, immediate=True):
            replay = self._replay("relocation.confirm", replay_key, replay_body)
            if replay is not None:
                return replay
            plan = self.connection.execute(
                "SELECT * FROM relocation_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if plan is None:
                raise NotFound("安置方案不存在")
            if plan["state"] != "proposed":
                raise InvalidState("方案不是待确认状态，可能已经确认、作废或取消")
            if plan["revision"] != expected_revision:
                raise InvalidState(f"方案版本已变化（期望 r{expected_revision}，当前 r{plan['revision']}），整单确认失败")

            link = self.connection.execute(
                "SELECT * FROM relocation_plan_resources WHERE plan_id=? AND candidate_id=?",
                (plan_id, candidate_id),
            ).fetchone()
            if link is None:
                raise NotFound("候选不在该方案的冻结快照中")

            resource = self.connection.execute(
                "SELECT * FROM relocation_resources WHERE resource_id=?",
                (link["resource_id"],),
            ).fetchone()
            if resource is None or resource["revision"] != link["expected_resource_revision"]:
                raise InvalidState("房源版本自方案生成后已变化，整单确认失败")
            if resource["state"] != "available":
                raise InvalidState(f"房源当前状态为 {resource['state']}，不再可占用，整单确认失败")

            site = None
            if link["expected_site_revision"] is not None:
                site = self._site(resource["site_id"])
                if site is None or site["revision"] != link["expected_site_revision"]:
                    raise InvalidState("安置点基础设施容量版本已变化，整单确认失败")
                if site["occupied_units"] >= site["capacity_units"]:
                    raise InvalidState("安置点基础设施容量已满，整单确认失败")

            now = self._now()
            resource_cursor = self.connection.execute(
                "UPDATE relocation_resources SET state='reserved',household_id=?,revision=revision+1 "
                "WHERE resource_id=? AND revision=? AND state='available'",
                (plan["household_id"], resource["resource_id"], resource["revision"]),
            )
            if resource_cursor.rowcount != 1:
                raise InvalidState("房源占用失败，整单确认失败")
            if site is not None:
                site_cursor = self.connection.execute(
                    "UPDATE relocation_sites SET occupied_units=occupied_units+1,revision=revision+1 "
                    "WHERE site_id=? AND revision=? AND occupied_units<capacity_units",
                    (site["site_id"], site["revision"]),
                )
                if site_cursor.rowcount != 1:
                    raise InvalidState("基础设施容量占用失败，整单确认失败")
            plan_cursor = self.connection.execute(
                "UPDATE relocation_plans SET state='confirmed',chosen_candidate_id=?,revision=revision+1,"
                "confirmed_at=? WHERE plan_id=? AND revision=? AND state='proposed'",
                (candidate_id, now, plan_id, plan["revision"]),
            )
            if plan_cursor.rowcount != 1:
                raise InvalidState("方案确认失败，整单回滚")
            self.connection.execute(
                "UPDATE relocation_applications SET state='confirmed',revision=revision+1 "
                "WHERE household_id=? AND state='registered'",
                (plan["household_id"],),
            )
            occupancy_cursor = self.connection.execute(
                "INSERT INTO relocation_occupancies(plan_id,household_id,resource_id,site_id,state,"
                "parent_occupancy_id,resource_revision,site_revision,reserved_at,created_by) "
                "VALUES(?,?,?,?,'reserved',NULL,?,?,?,?)",
                (
                    plan_id,
                    plan["household_id"],
                    resource["resource_id"],
                    resource["site_id"],
                    resource["revision"] + 1,
                    None if site is None else site["revision"] + 1,
                    now,
                    actor_id,
                ),
            )
            occupancy_id = int(occupancy_cursor.lastrowid)
            response = {
                "plan_id": plan_id,
                "household_id": plan["household_id"],
                "state": "confirmed",
                "revision": plan["revision"] + 1,
                "chosen_candidate_id": candidate_id,
                "confirmed_at": now,
                "resource_genealogy": self._genealogy_locked(occupancy_id),
            }
            self._remember("relocation.confirm", replay_key, {"plan_id": plan_id, **raw}, response)
            self._audit(
                "relocation_plan",
                plan_id,
                "relocation.plan.confirmed",
                actor_id,
                {
                    "household_id": plan["household_id"],
                    "candidate_id": candidate_id,
                    "resource_id": resource["resource_id"],
                    "site_id": resource["site_id"],
                    "occupancy_id": occupancy_id,
                    "snapshot_sha256": plan["snapshot_sha256"],
                },
            )
        return response

    # -- 交付入住 -----------------------------------------------------------

    def deliver_plan(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._require(actor_id, "relocation.plan.deliver")
        with transaction(self.connection, immediate=True):
            plan = self.connection.execute(
                "SELECT * FROM relocation_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if plan is None:
                raise NotFound("安置方案不存在")
            if plan["state"] == "occupied":
                occupancy = self.connection.execute(
                    "SELECT * FROM relocation_occupancies WHERE plan_id=? ORDER BY occupancy_id DESC LIMIT 1",
                    (plan_id,),
                ).fetchone()
                return {"plan_id": plan_id, "state": "occupied", "revision": plan["revision"], "resource_genealogy": self._genealogy_locked(occupancy["occupancy_id"])}
            if plan["state"] != "confirmed":
                raise InvalidState("只有已确认方案可以办理交付入住")
            occupancy = self.connection.execute(
                "SELECT * FROM relocation_occupancies WHERE plan_id=? AND state='reserved'",
                (plan_id,),
            ).fetchone()
            now = self._now()
            cursor = self.connection.execute(
                "UPDATE relocation_resources SET state='occupied',revision=revision+1 "
                "WHERE resource_id=? AND state='reserved' AND household_id=?",
                (occupancy["resource_id"], plan["household_id"]),
            )
            if cursor.rowcount != 1:
                raise InvalidState("预留房源状态异常，不能交付")
            self.connection.execute(
                "UPDATE relocation_occupancies SET state='occupied',occupied_at=? WHERE occupancy_id=?",
                (now, occupancy["occupancy_id"]),
            )
            self.connection.execute(
                "UPDATE relocation_plans SET state='occupied',revision=revision+1 WHERE plan_id=? AND state='confirmed'",
                (plan_id,),
            )
            self.connection.execute(
                "UPDATE relocation_applications SET state='occupied',revision=revision+1 "
                "WHERE household_id=? AND state='confirmed'",
                (plan["household_id"],),
            )
            self._audit(
                "relocation_plan",
                plan_id,
                "relocation.plan.delivered",
                actor_id,
                {"household_id": plan["household_id"], "resource_id": occupancy["resource_id"], "occupancy_id": occupancy["occupancy_id"]},
            )
            response = {
                "plan_id": plan_id,
                "state": "occupied",
                "revision": plan["revision"] + 1,
                "occupied_at": now,
                "resource_genealogy": self._genealogy_locked(occupancy["occupancy_id"]),
            }
        return response

    # -- 取消：只释放尚未交付的预留 -----------------------------------------

    def cancel_application(self, actor_id: str, household_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "relocation.application.cancel")
        idempotency_key = self._identifier(raw, "idempotency_key")
        replay_body = {"household_id": household_id, **raw}
        with transaction(self.connection, immediate=True):
            replay = self._replay("relocation.cancel", idempotency_key, replay_body)
            if replay is not None:
                return replay
            application = self._application_row(household_id)
            if application["state"] == "cancelled":
                raise InvalidState("家庭申请已经取消")
            active_occupancies = self.connection.execute(
                "SELECT * FROM relocation_occupancies WHERE household_id=? AND state IN ('reserved','occupied')",
                (household_id,),
            ).fetchall()
            delivered = [row for row in active_occupancies if row["state"] == "occupied"]
            if delivered:
                # 已经入住的家庭不能被自动换房：已交付资源不参与取消。
                raise InvalidState("家庭已经入住，已交付资源不能释放或自动换房")
            now = self._now()
            released: list[dict[str, Any]] = []
            for occupancy in active_occupancies:
                resource = self.connection.execute(
                    "SELECT * FROM relocation_resources WHERE resource_id=?",
                    (occupancy["resource_id"],),
                ).fetchone()
                self.connection.execute(
                    "UPDATE relocation_resources SET state='available',household_id=NULL,revision=revision+1 "
                    "WHERE resource_id=? AND state='reserved'",
                    (occupancy["resource_id"],),
                )
                if occupancy["site_id"] is not None:
                    self.connection.execute(
                        "UPDATE relocation_sites SET occupied_units=occupied_units-1,revision=revision+1 "
                        "WHERE site_id=? AND occupied_units>0",
                        (occupancy["site_id"],),
                    )
                self.connection.execute(
                    "UPDATE relocation_occupancies SET state='released',released_at=? WHERE occupancy_id=? AND state='reserved'",
                    (now, occupancy["occupancy_id"]),
                )
                released.append(
                    {
                        "occupancy_id": occupancy["occupancy_id"],
                        "resource_id": occupancy["resource_id"],
                        "kind": resource["kind"],
                        "kind_label": RESOURCE_KIND_LABELS[resource["kind"]],
                        "site_id": occupancy["site_id"],
                        "resource_revision": resource["revision"] + 1,
                    }
                )
            self.connection.execute(
                "UPDATE relocation_plans SET state='cancelled',cancelled_at=? "
                "WHERE household_id=? AND state IN ('proposed','confirmed')",
                (now, household_id),
            )
            self.connection.execute(
                "UPDATE relocation_applications SET state='cancelled',revision=revision+1 WHERE household_id=?",
                (household_id,),
            )
            response = {
                "household_id": household_id,
                "state": "cancelled",
                "revision": application["revision"] + 1,
                "released_resources": released,
                "retained_resources": [],
                "cancelled_at": now,
                "notice": "仅释放尚未交付的预留；已入住资源不释放",
            }
            self._remember("relocation.cancel", idempotency_key, {"household_id": household_id, **raw}, response)
            self._audit(
                "relocation_application",
                household_id,
                "relocation.application.cancelled",
                actor_id,
                {"released": [item["resource_id"] for item in released], "retained": []},
            )
        return response

    # -- 查询：方案与资源谱系 -----------------------------------------------

    def plan_detail(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._supply._user(actor_id)
        plan = self.connection.execute("SELECT * FROM relocation_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if plan is None:
            raise NotFound("安置方案不存在")
        result = json.loads(plan["result_json"])
        response = {
            "plan_id": plan["plan_id"],
            "household_id": plan["household_id"],
            "state": plan["state"],
            "revision": plan["revision"],
            "snapshot_sha256": plan["snapshot_sha256"],
            "created_at": plan["created_at"],
            "confirmed_at": plan["confirmed_at"],
            "cancelled_at": plan["cancelled_at"],
            "chosen_candidate_id": plan["chosen_candidate_id"],
            "feasible": result["feasible"],
            "candidates": result["candidates"],
            "rejected": result["rejected"],
            "unmet_conditions": result["unmet_conditions"],
        }
        if plan["chosen_candidate_id"] is not None:
            occupancy = self.connection.execute(
                "SELECT * FROM relocation_occupancies WHERE plan_id=? ORDER BY occupancy_id LIMIT 1",
                (plan_id,),
            ).fetchone()
            response["resource_genealogy"] = None if occupancy is None else self._genealogy_locked(occupancy["occupancy_id"])
        return response

    def household_genealogy(self, actor_id: str, household_id: str) -> dict[str, Any]:
        self._supply._user(actor_id)
        self._application_row(household_id)
        rows = self.connection.execute(
            "SELECT occupancy_id FROM relocation_occupancies "
            "WHERE household_id=? AND parent_occupancy_id IS NULL ORDER BY occupancy_id",
            (household_id,),
        ).fetchall()
        roots = [self._genealogy_locked(row["occupancy_id"]) for row in rows]
        return {"household_id": household_id, "lineages": roots}

    def _genealogy_locked(self, occupancy_id: int) -> dict[str, Any]:
        nodes: list[dict[str, Any]] = []
        current_id: int | None = occupancy_id
        while current_id is not None:
            row = self.connection.execute(
                "SELECT o.*,r.kind,r.township,r.village FROM relocation_occupancies o "
                "JOIN relocation_resources r ON r.resource_id=o.resource_id WHERE o.occupancy_id=?",
                (current_id,),
            ).fetchone()
            if row is None:
                break
            site_revision = row["site_revision"]
            site_snapshot = None
            if row["site_id"] is not None:
                site_row = self._site(row["site_id"])
                site_snapshot = None if site_row is None else {
                    "site_id": site_row["site_id"],
                    "name": site_row["name"],
                    "township": site_row["township"],
                    "capacity_units": site_row["capacity_units"],
                    "occupied_units": site_row["occupied_units"],
                    "revision_at_occupation": site_revision,
                    "current_revision": site_row["revision"],
                }
            nodes.append(
                {
                    "occupancy_id": row["occupancy_id"],
                    "household_id": row["household_id"],
                    "plan_id": row["plan_id"],
                    "state": row["state"],
                    "resource": {
                        "resource_id": row["resource_id"],
                        "kind": row["kind"],
                        "kind_label": RESOURCE_KIND_LABELS[row["kind"]],
                        "township": row["township"],
                        "village": row["village"],
                        "site_id": row["site_id"],
                        "revision_at_occupation": row["resource_revision"],
                    },
                    "site": site_snapshot,
                    "parent_occupancy_id": row["parent_occupancy_id"],
                    "reserved_at": row["reserved_at"],
                    "occupied_at": row["occupied_at"],
                    "released_at": row["released_at"],
                }
            )
            current_id = row["parent_occupancy_id"]
        lineage = list(reversed(nodes))
        return {
            "household_id": nodes[0]["household_id"],
            "current": nodes[0],
            "lineage": lineage,
            "auto_reassignment": False,
            "notice": "已入住家庭不允许自动换房；谱系仅记录人工交付链路",
        }

    @staticmethod
    def _identifier(raw: Mapping[str, Any], field: str) -> str:
        return parse_identifier(raw.get(field), field)

    @staticmethod
    def _revision(raw: Mapping[str, Any]) -> int:
        value = raw.get("expected_revision")
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValidationFailed("expected_revision 必须是正整数")
        return value
