"""跨村安置候选方案的确定性生成与评分。

规划过程不访问数据库：服务层在单个事务里冻结房源、地块限制和基础设施
容量快照后调用本模块，因此同一份快照永远得到同一组候选与排序。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .planning import decimal_text, digest
from .relocation_models import CONSTRAINT_LABELS, RESOURCE_KIND_LABELS


@dataclass(frozen=True, slots=True)
class Violation:
    code: str
    required: bool
    actual: str
    limit: str | None
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "label": CONSTRAINT_LABELS.get(self.code, self.code),
            "required": self.required,
            "actual": self.actual,
            "limit": self.limit,
            "message": self.message,
        }


def _constraint(constraints: Mapping[str, Mapping[str, Any]], code: str) -> Mapping[str, Any] | None:
    return constraints.get(code)


def _distance_violation(
    code: str,
    constraint: Mapping[str, Any] | None,
    actual: Decimal,
) -> Violation | None:
    if constraint is None:
        return None
    limit = Decimal(str(constraint["limit"]))
    if actual <= limit:
        return None
    label = CONSTRAINT_LABELS[code]
    return Violation(
        code=code,
        required=bool(constraint["required"]),
        actual=decimal_text(actual),
        limit=decimal_text(limit),
        message=f"{label} {decimal_text(actual)} 公里超过上限 {decimal_text(limit)} 公里",
    )


def evaluate_resource(
    application: Mapping[str, Any],
    resource: Mapping[str, Any],
    site: Mapping[str, Any] | None,
) -> list[Violation]:
    """返回资源的全部未满足条件；空列表表示完全合规矩。"""
    constraints = {item["code"]: item for item in application["constraints"]}
    violations: list[Violation] = []

    size_rule = _constraint(constraints, "household_size")
    if size_rule is not None and int(resource["beds"]) < int(application["member_count"]):
        violations.append(
            Violation(
                "household_size",
                bool(size_rule["required"]),
                f"可住 {resource['beds']} 人",
                f"不少于 {application['member_count']} 人",
                f"床位 {resource['beds']} 不能容纳家庭人口 {application['member_count']}",
            )
        )

    access_rule = _constraint(constraints, "accessible")
    if access_rule is not None and not bool(resource["accessible"]):
        violations.append(
            Violation(
                "accessible",
                bool(access_rule["required"]),
                "无无障碍条件",
                "需要无障碍",
                "房源不具备无障碍条件",
            )
        )

    area_rule = _constraint(constraints, "min_area")
    if area_rule is not None:
        area = Decimal(str(resource["area_sqm"]))
        area_limit = Decimal(str(area_rule["limit"]))
        if area < area_limit:
            violations.append(
                Violation(
                    "min_area",
                    bool(area_rule["required"]),
                    f"{decimal_text(area)} 平方米",
                    f"不低于 {decimal_text(area_limit)} 平方米",
                    "面积低于家庭最低面积要求",
                )
            )

    for code, value in (
        ("school_radius", Decimal(str(resource["school_km"]))),
        ("medical_radius", Decimal(str(resource["medical_km"]))),
        ("commute_radius", Decimal(str(resource["commute_km"]))),
    ):
        violation = _distance_violation(code, _constraint(constraints, code), value)
        if violation is not None:
            violations.append(violation)

    preference = list(application["preference_order"])
    position = preference.index(resource["kind"]) if resource["kind"] in preference else None
    max_downgrade = int(application["max_downgrade"])
    if position is None:
        violations.append(
            Violation(
                "resource_kind",
                True,
                RESOURCE_KIND_LABELS[resource["kind"]],
                "/".join(RESOURCE_KIND_LABELS[item] for item in preference),
                "资源类型不在家庭登记的替代序列内",
            )
        )
    elif position > max_downgrade:
        violations.append(
            Violation(
                "downgrade",
                True,
                f"第 {position + 1} 顺位（{RESOURCE_KIND_LABELS[resource['kind']]}）",
                f"最多接受降级 {max_downgrade} 级",
                "资源降级幅度超出家庭可接受范围",
            )
        )

    if resource["kind"] == "homestead-quota":
        eligible = list(resource.get("restrictions", {}).get("eligible_villages", []))
        if eligible and application["origin_village"] not in eligible:
            violations.append(
                Violation(
                    "plot_restriction",
                    True,
                    f"仅限 {','.join(eligible)}",
                    f"原籍村 {application['origin_village']}",
                    "宅基地指标的地块限制不覆盖该家庭原籍村",
                )
            )

    if site is not None and int(site["occupied_units"]) >= int(site["capacity_units"]):
        violations.append(
            Violation(
                "site_capacity",
                True,
                f"已入住 {site['occupied_units']} 户",
                f"容量 {site['capacity_units']} 户",
                "安置点基础设施容量已满，不能再安排家庭",
            )
        )

    return violations


def _score(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        int(candidate["preference_rank"]),
        len(candidate["compromises"]),
        int(candidate["quality_tier"]),
        Decimal(str(candidate["area_sqm"])) * Decimal("-1"),
        Decimal(str(candidate["school_km"])),
        Decimal(str(candidate["medical_km"])),
        Decimal(str(candidate["commute_km"])),
        str(candidate["resource_id"]),
    )


def generate_candidates(
    application: Mapping[str, Any],
    resources: Sequence[Mapping[str, Any]],
    sites: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """基于冻结快照生成候选、淘汰原因与未满足条件汇总。

    sites 快照项需包含 site_id、capacity_units、occupied_units、revision。
    """
    site_by_id = {item["site_id"]: item for item in sites}
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for resource in resources:
        site = None
        if resource.get("site_id"):
            site = site_by_id.get(resource["site_id"])
        violations = evaluate_resource(application, resource, site)
        hard = [item for item in violations if item.required]
        soft = [item for item in violations if not item.required]
        if hard:
            rejected.append(
                {
                    "resource_id": resource["resource_id"],
                    "kind": resource["kind"],
                    "kind_label": RESOURCE_KIND_LABELS[resource["kind"]],
                    "township": resource["township"],
                    "village": resource["village"],
                    "reasons": [item.as_dict() for item in violations],
                }
            )
            continue
        preference = list(application["preference_order"])
        preference_rank = preference.index(resource["kind"])
        accepted.append(
            {
                "resource_id": resource["resource_id"],
                "kind": resource["kind"],
                "kind_label": RESOURCE_KIND_LABELS[resource["kind"]],
                "site_id": resource.get("site_id"),
                "township": resource["township"],
                "village": resource["village"],
                "area_sqm": decimal_text(Decimal(str(resource["area_sqm"]))),
                "beds": int(resource["beds"]),
                "accessible": bool(resource["accessible"]),
                "school_km": decimal_text(Decimal(str(resource["school_km"]))),
                "medical_km": decimal_text(Decimal(str(resource["medical_km"]))),
                "commute_km": decimal_text(Decimal(str(resource["commute_km"]))),
                "quality_tier": int(resource["quality_tier"]),
                "preference_rank": preference_rank,
                "downgrade_level": preference_rank,
                "compromises": [item.as_dict() for item in soft],
                "resource_revision": int(resource["revision"]),
                "site_revision": None if site is None else int(site["revision"]),
            }
        )

    accepted.sort(key=_score)
    candidates: list[dict[str, Any]] = []
    for rank, item in enumerate(accepted, start=1):
        candidate = {"candidate_id": f"c{rank}", "rank": rank, **item}
        candidates.append(candidate)

    rejected.sort(key=lambda item: str(item["resource_id"]))
    unmet = _unmet_conditions(application, candidates, rejected)
    return {
        "household_id": application["household_id"],
        "feasible": bool(candidates),
        "candidates": candidates,
        "rejected": rejected,
        "unmet_conditions": unmet,
    }


def _unmet_conditions(
    application: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    rejected: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    unmet: list[dict[str, Any]] = []
    for index, kind in enumerate(application["preference_order"]):
        if any(item["kind"] == kind for item in candidates):
            continue
        reasons = sorted(
            {
                reason["code"]
                for item in rejected
                if item["kind"] == kind
                for reason in item["reasons"]
            }
        )
        unmet.append(
            {
                "code": "resource_kind",
                "label": f"第 {index + 1} 顺位资源（{RESOURCE_KIND_LABELS[kind]}）",
                "satisfied": False,
                "required": True,
                "message": f"{RESOURCE_KIND_LABELS[kind]}没有可用候选",
                "blocking_reasons": reasons,
            }
        )

    # 硬性条件不合规的房源已在 rejected 中逐条说明；这里只汇总需要家庭确认妥协的可选项。
    for constraint in application["constraints"]:
        if constraint["required"] or not candidates:
            continue
        code = constraint["code"]
        affected = [
            item["candidate_id"]
            for item in candidates
            if any(compromise["code"] == code for compromise in item["compromises"])
        ]
        if affected:
            unmet.append(
                {
                    "code": code,
                    "label": CONSTRAINT_LABELS[code],
                    "satisfied": False,
                    "required": False,
                    "affected_candidates": affected,
                    "message": f"候选 {', '.join(affected)} 在{CONSTRAINT_LABELS[code]}上低于期望值，需要家庭确认妥协",
                }
            )
    return unmet


def frozen_snapshot_digest(
    application: Mapping[str, Any],
    resources: Sequence[Mapping[str, Any]],
    sites: Sequence[Mapping[str, Any]],
) -> str:
    """对冻结快照（含版本号）取摘要；确认时以行级版本比对为准。"""
    body = {
        "application": application,
        "resources": sorted(
            (
                {
                    "resource_id": item["resource_id"],
                    "state": item["state"],
                    "revision": item["revision"],
                    "kind": item["kind"],
                    "site_id": item.get("site_id"),
                    "area_sqm": decimal_text(Decimal(str(item["area_sqm"]))),
                    "beds": int(item["beds"]),
                    "accessible": bool(item["accessible"]),
                    "school_km": decimal_text(Decimal(str(item["school_km"]))),
                    "medical_km": decimal_text(Decimal(str(item["medical_km"]))),
                    "commute_km": decimal_text(Decimal(str(item["commute_km"]))),
                    "quality_tier": int(item["quality_tier"]),
                    "restrictions": item.get("restrictions", {}),
                }
                for item in resources
            ),
            key=lambda item: item["resource_id"],
        ),
        "sites": sorted(
            (
                {
                    "site_id": item["site_id"],
                    "capacity_units": int(item["capacity_units"]),
                    "occupied_units": int(item["occupied_units"]),
                    "revision": int(item["revision"]),
                }
                for item in sites
            ),
            key=lambda item: item["site_id"],
        ),
    }
    return digest(body)
