"""跨村安置资源替代编排的确定性候选生成。

纯函数模块：输入经办人登记的家庭需求（有顺序的约束、最低面积、
就学就医半径、可接受的降级范围）以及冻结的房源、地块限制和基础
设施容量快照，输出排序后的候选方案、每个候选的取舍说明和全局
未满足条件。不访问数据库，便于离线复算与审计。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from .models import RELOCATION_CONSTRAINTS, RelocationNeeds
from .planning import decimal_text


ZERO = Decimal("0")
HUNDRED = Decimal("100")
MAX_CANDIDATES = 3
MAX_UNITS_PER_PLAN = 2
MAX_UNMET_RESOURCES = 5


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


@dataclass(frozen=True, slots=True)
class UnitCandidate:
    resource: Mapping[str, Any]
    site: Mapping[str, Any]
    relax_vector: tuple[int, ...]
    kind_rank: int
    tradeoffs: tuple[dict[str, Any], ...]

    @property
    def resource_id(self) -> str:
        return str(self.resource["resource_id"])


def remaining_households(row: Mapping[str, Any]) -> int:
    return int(row["capacity_households"]) - int(row["reserved_households"]) - int(row["occupied_households"])


def infra_remaining_households(site: Mapping[str, Any]) -> int:
    return int(site["infra_capacity_households"]) - int(site["reserved_households"]) - int(site["occupied_households"])


def _violation(name: str, needs: RelocationNeeds, resource: Mapping[str, Any]) -> dict[str, Any] | None:
    resource_id = str(resource["resource_id"])
    if name == "accessible":
        if needs.requires_accessible and not bool(resource["accessible"]):
            return {
                "condition": "accessible",
                "resource_id": resource_id,
                "requested": "无障碍房源",
                "offered": "普通房源",
                "detail": "家庭需要无障碍房源，该房源不具备无障碍条件",
            }
        return None
    if name == "min_area":
        area = _decimal(resource["area_sqm"])
        if area < needs.min_area_sqm:
            return {
                "condition": "min_area",
                "resource_id": resource_id,
                "requested": decimal_text(needs.min_area_sqm),
                "offered": decimal_text(area),
                "detail": f"面积 {decimal_text(area)}㎡ 低于登记最低面积 {decimal_text(needs.min_area_sqm)}㎡",
            }
        return None
    if name == "commute":
        commute = int(resource["commute_minutes"])
        if needs.max_commute_minutes is not None and commute > needs.max_commute_minutes:
            return {
                "condition": "commute",
                "resource_id": resource_id,
                "requested": str(needs.max_commute_minutes),
                "offered": str(commute),
                "detail": f"通勤 {commute} 分钟超出登记上限 {needs.max_commute_minutes} 分钟",
            }
        return None
    if name == "school_radius":
        distance = _decimal(resource["school_km"])
        if needs.school_radius_km is not None and distance > needs.school_radius_km:
            return {
                "condition": "school_radius",
                "resource_id": resource_id,
                "requested": decimal_text(needs.school_radius_km),
                "offered": decimal_text(distance),
                "detail": f"最近学校 {decimal_text(distance)}km 超出就学半径 {decimal_text(needs.school_radius_km)}km",
            }
        return None
    if name == "clinic_radius":
        distance = _decimal(resource["clinic_km"])
        if needs.clinic_radius_km is not None and distance > needs.clinic_radius_km:
            return {
                "condition": "clinic_radius",
                "resource_id": resource_id,
                "requested": decimal_text(needs.clinic_radius_km),
                "offered": decimal_text(distance),
                "detail": f"最近医疗点 {decimal_text(distance)}km 超出就医半径 {decimal_text(needs.clinic_radius_km)}km",
            }
        return None
    raise ValueError(f"未知家庭约束 {name}")


def _within_scope(name: str, needs: RelocationNeeds, resource: Mapping[str, Any]) -> bool:
    scope = needs.downgrade
    if name == "accessible":
        return True
    if name == "min_area":
        floor = needs.min_area_sqm * (HUNDRED - scope.max_area_relax_percent) / HUNDRED
        return _decimal(resource["area_sqm"]) >= floor
    if name == "commute":
        assert needs.max_commute_minutes is not None
        return int(resource["commute_minutes"]) - needs.max_commute_minutes <= scope.max_extra_commute_minutes
    if name == "school_radius":
        assert needs.school_radius_km is not None
        return _decimal(resource["school_km"]) - needs.school_radius_km <= scope.max_extra_school_km
    if name == "clinic_radius":
        assert needs.clinic_radius_km is not None
        return _decimal(resource["clinic_km"]) - needs.clinic_radius_km <= scope.max_extra_clinic_km
    raise ValueError(f"未知家庭约束 {name}")


def _tradeoff(name: str, needs: RelocationNeeds, resource: Mapping[str, Any]) -> dict[str, Any]:
    scope = needs.downgrade
    resource_id = str(resource["resource_id"])
    if name == "accessible":
        detail = "无障碍房源不足，按降级范围改用普通房源"
        requested, offered = "无障碍房源", "普通房源"
    elif name == "min_area":
        area = _decimal(resource["area_sqm"])
        requested, offered = decimal_text(needs.min_area_sqm), decimal_text(area)
        detail = (
            f"面积 {decimal_text(area)}㎡ 低于最低面积 {decimal_text(needs.min_area_sqm)}㎡，"
            f"在可接受面积降级 {decimal_text(scope.max_area_relax_percent)}% 范围内"
        )
    elif name == "commute":
        commute = int(resource["commute_minutes"])
        assert needs.max_commute_minutes is not None
        requested, offered = str(needs.max_commute_minutes), str(commute)
        detail = (
            f"通勤 {commute} 分钟超出上限 {needs.max_commute_minutes} 分钟，"
            f"在可接受加时 {scope.max_extra_commute_minutes} 分钟范围内"
        )
    elif name == "school_radius":
        distance = _decimal(resource["school_km"])
        assert needs.school_radius_km is not None
        requested, offered = decimal_text(needs.school_radius_km), decimal_text(distance)
        detail = (
            f"就学距离 {decimal_text(distance)}km 超出半径 {decimal_text(needs.school_radius_km)}km，"
            f"在可接受放宽 {decimal_text(scope.max_extra_school_km)}km 范围内"
        )
    elif name == "clinic_radius":
        distance = _decimal(resource["clinic_km"])
        assert needs.clinic_radius_km is not None
        requested, offered = decimal_text(needs.clinic_radius_km), decimal_text(distance)
        detail = (
            f"就医距离 {decimal_text(distance)}km 超出半径 {decimal_text(needs.clinic_radius_km)}km，"
            f"在可接受放宽 {decimal_text(scope.max_extra_clinic_km)}km 范围内"
        )
    else:
        raise ValueError(f"未知家庭约束 {name}")
    return {"condition": name, "resource_id": resource_id, "requested": requested, "offered": offered, "detail": detail}


def evaluate_unit(
    needs: RelocationNeeds,
    resource: Mapping[str, Any],
    site: Mapping[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """返回 (硬性未满足, 可接受降级取舍, 超出降级范围) 三类结果。"""
    resource_id = str(resource["resource_id"])
    failures: list[dict[str, Any]] = []
    relaxations: list[dict[str, Any]] = []
    overages: list[dict[str, Any]] = []
    if resource["state"] != "active":
        failures.append({"condition": "resource_state", "resource_id": resource_id, "detail": "房源当前不可编排"})
        return failures, relaxations, overages
    if str(resource["kind"]) not in needs.downgrade.allowed_kinds:
        failures.append({
            "condition": "kind_not_allowed",
            "resource_id": resource_id,
            "detail": f"资源类型 {resource['kind']} 不在家庭可接受的降级范围内",
        })
        return failures, relaxations, overages
    if remaining_households(resource) < 1:
        failures.append({"condition": "capacity", "resource_id": resource_id, "detail": "房源剩余容量不足"})
        return failures, relaxations, overages
    if site is None or site["state"] != "active":
        failures.append({"condition": "site_state", "resource_id": resource_id, "detail": "所属安置点当前不可编排"})
        return failures, relaxations, overages
    if infra_remaining_households(site) < 1:
        failures.append({
            "condition": "infra_capacity",
            "resource_id": resource_id,
            "detail": f"安置点 {site['site_id']} 基础设施容量不足",
        })
        return failures, relaxations, overages
    eligible = resource["eligible_townships"]
    eligible_list = [item.strip() for item in str(eligible).split(",") if item.strip()]
    if "*" not in eligible_list and needs.origin_township not in eligible_list:
        failures.append({
            "condition": "plot_restriction",
            "resource_id": resource_id,
            "detail": f"地块限制不允许 {needs.origin_township} 迁出户使用该资源",
        })
        return failures, relaxations, overages
    enforce = {constraint.name: constraint.enforce for constraint in needs.constraints}
    for name in RELOCATION_CONSTRAINTS:
        violation = _violation(name, needs, resource)
        if violation is None:
            continue
        mode = enforce.get(name, "hard")
        if mode == "hard":
            failures.append(violation)
        elif _within_scope(name, needs, resource):
            relaxations.append(_tradeoff(name, needs, resource))
        else:
            violation["detail"] = violation["detail"] + "，且超出可接受降级范围"
            overages.append(violation)
    return failures, relaxations, overages


def _unit_score(unit: UnitCandidate) -> tuple[object, ...]:
    resource = unit.resource
    return (
        unit.relax_vector,
        unit.kind_rank,
        int(resource["commute_minutes"]),
        -_decimal(resource["area_sqm"]),
        str(resource["resource_id"]),
    )


def _assignment(unit: UnitCandidate) -> dict[str, Any]:
    return {
        "resource_id": str(unit.resource["resource_id"]),
        "site_id": str(unit.site["site_id"]),
        "resource_revision": int(unit.resource["revision"]),
        "site_revision": int(unit.site["revision"]),
        "households": 1,
    }


def _combo_candidate(units: Sequence[UnitCandidate], needs: RelocationNeeds) -> dict[str, Any]:
    relaxed: list[str] = []
    tradeoffs: list[dict[str, Any]] = []
    for unit in units:
        for tradeoff in unit.tradeoffs:
            if tradeoff["condition"] not in relaxed:
                relaxed.append(tradeoff["condition"])
        tradeoffs.extend(unit.tradeoffs)
    return {
        "assignments": [_assignment(unit) for unit in units],
        "tradeoffs": tradeoffs,
        "relaxed_constraints": relaxed,
    }


def _combo_score(units: Sequence[UnitCandidate]) -> tuple[object, ...]:
    length = max(len(unit.relax_vector) for unit in units)
    merged = tuple(
        max(unit.relax_vector[index] if index < len(unit.relax_vector) else 0 for unit in units)
        for index in range(length)
    )
    return (
        merged,
        len(units),
        max(unit.kind_rank for unit in units),
        max(int(unit.resource["commute_minutes"]) for unit in units),
        -sum(_decimal(unit.resource["area_sqm"]) for unit in units),
        tuple(str(unit.resource["resource_id"]) for unit in units),
    )


def _aggregate_unmet(entries: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for entry in entries:
        condition = str(entry["condition"])
        bucket = grouped.setdefault(condition, {"condition": condition, "detail": str(entry["detail"]), "resources": []})
        resource_id = entry.get("resource_id")
        if resource_id is not None and len(bucket["resources"]) < MAX_UNMET_RESOURCES:
            bucket["resources"].append(str(resource_id))
    return [grouped[condition] for condition in sorted(grouped)]


def build_candidates(
    needs: RelocationNeeds,
    resources: Sequence[Mapping[str, Any]],
    sites: Mapping[str, Mapping[str, Any]],
    limit: int = MAX_CANDIDATES,
) -> dict[str, Any]:
    """基于冻结快照生成候选方案，结果对相同输入完全确定。"""
    unmet: list[dict[str, Any]] = []
    units: list[UnitCandidate] = []
    for resource in sorted(resources, key=lambda row: str(row["resource_id"])):
        site = sites.get(str(resource["site_id"]))
        failures, relaxations, overages = evaluate_unit(needs, resource, site)
        unmet.extend(failures)
        unmet.extend(overages)
        if failures or overages or site is None:
            continue
        kind_rank = needs.downgrade.allowed_kinds.index(str(resource["kind"]))
        vector = tuple(
            1 if any(tradeoff["condition"] == constraint.name for tradeoff in relaxations) else 0
            for constraint in needs.constraints
        )
        units.append(UnitCandidate(resource, site, vector, kind_rank, tuple(relaxations)))
    singles = [unit for unit in units if int(unit.resource["max_household_size"]) >= needs.household_size]
    size_blocked = [unit for unit in units if int(unit.resource["max_household_size"]) < needs.household_size]
    candidates: list[dict[str, Any]] = []
    if singles:
        for unit in sorted(singles, key=_unit_score)[:limit]:
            candidates.append(_combo_candidate([unit], needs))
    else:
        for unit in size_blocked:
            unmet.append({
                "condition": "household_size",
                "resource_id": str(unit.resource["resource_id"]),
                "detail": f"单套最多容纳 {unit.resource['max_household_size']} 人，家庭人口 {needs.household_size} 人",
            })
        by_site: dict[str, list[UnitCandidate]] = {}
        for unit in size_blocked:
            by_site.setdefault(str(unit.site["site_id"]), []).append(unit)
        combos: list[tuple[UnitCandidate, ...]] = []
        for site_id in sorted(by_site):
            site_units = sorted(by_site[site_id], key=_unit_score)
            site = sites[site_id]
            picked: list[UnitCandidate] = []
            covered = 0
            for unit in site_units:
                if len(picked) >= MAX_UNITS_PER_PLAN or len(picked) >= infra_remaining_households(site):
                    break
                picked.append(unit)
                covered += int(unit.resource["max_household_size"])
                if covered >= needs.household_size:
                    break
            if covered >= needs.household_size:
                combos.append(tuple(picked))
            else:
                unmet.append({
                    "condition": "household_size",
                    "resource_id": None,
                    "detail": f"安置点 {site_id} 房源组合后仍无法容纳家庭人口 {needs.household_size} 人",
                })
        for combo in sorted(combos, key=_combo_score)[:limit]:
            candidates.append(_combo_candidate(list(combo), needs))
    ranked = []
    for rank, candidate in enumerate(candidates, start=1):
        ranked.append({"rank": rank, **candidate})
    return {"candidates": ranked, "unmet_conditions": _aggregate_unmet(unmet)}
