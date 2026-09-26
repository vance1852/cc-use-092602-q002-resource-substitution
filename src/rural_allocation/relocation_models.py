"""跨村安置资源替代编排的领域输入契约。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from .errors import ValidationFailed
from .models import decimal_value, identifier, positive_integer, required_text


RESOURCE_KINDS = ("turnover-home", "resettlement-home", "homestead-quota")
RESOURCE_KIND_LABELS = {
    "turnover-home": "周转房",
    "resettlement-home": "集中安置房",
    "homestead-quota": "宅基地指标",
}
CONSTRAINT_CODES = (
    "accessible",
    "min_area",
    "school_radius",
    "medical_radius",
    "commute_radius",
    "household_size",
)
CONSTRAINT_LABELS = {
    "accessible": "无障碍",
    "min_area": "最低面积",
    "school_radius": "就学半径",
    "medical_radius": "就医半径",
    "commute_radius": "通勤半径",
    "household_size": "家庭人口",
    "downgrade": "资源降级",
    "resource_kind": "资源类型",
    "plot_restriction": "地块限制",
    "site_capacity": "基础设施容量",
}


def _kind(value: object, field: str) -> str:
    result = required_text(value, field, 32)
    if result not in RESOURCE_KINDS:
        raise ValidationFailed("kind 必须是 turnover-home、resettlement-home 或 homestead-quota")
    return result


@dataclass(frozen=True, slots=True)
class HouseholdConstraint:
    """有顺序的家庭约束；required=False 时为可妥协条件，记入未满足条件但不淘汰资源。"""

    code: str
    required: bool
    limit: Decimal | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "required": self.required,
            "limit": None if self.limit is None else format(self.limit, "f"),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], index: int) -> "HouseholdConstraint":
        if not isinstance(raw, Mapping):
            raise ValidationFailed(f"constraints[{index}] 必须是对象")
        code = required_text(raw.get("code"), f"constraints[{index}].code", 32)
        if code not in CONSTRAINT_CODES:
            raise ValidationFailed(f"constraints[{index}].code 不是受支持的家庭约束")
        required = raw.get("required", True)
        if not isinstance(required, bool):
            raise ValidationFailed(f"constraints[{index}].required 必须是布尔值")
        limit = None
        if code in {"min_area", "school_radius", "medical_radius", "commute_radius"}:
            field = f"constraints[{index}].limit"
            limit = decimal_value(raw.get("limit"), field, minimum=Decimal("0"))
        return cls(code=code, required=required, limit=limit)


@dataclass(frozen=True, slots=True)
class RelocationSite:
    """集中安置点的基础设施容量（房源之外的容量约束）。"""

    site_id: str
    name: str
    township: str
    capacity_units: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RelocationSite":
        return cls(
            site_id=identifier(raw.get("site_id"), "site_id"),
            name=required_text(raw.get("name"), "name"),
            township=required_text(raw.get("township"), "township"),
            capacity_units=positive_integer(raw.get("capacity_units"), "capacity_units"),
        )


@dataclass(frozen=True, slots=True)
class RelocationResource:
    """周转房、集中安置房或宅基地指标；三类资源在条件满足时可互相替代。"""

    resource_id: str
    kind: str
    site_id: str | None
    township: str
    village: str
    area_sqm: Decimal
    beds: int
    accessible: bool
    school_km: Decimal
    medical_km: Decimal
    commute_km: Decimal
    quality_tier: int
    restrictions: Mapping[str, Any]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RelocationResource":
        kind = _kind(raw.get("kind"), "kind")
        site_value = raw.get("site_id")
        site_id = None if site_value in (None, "") else identifier(site_value, "site_id")
        if kind == "homestead-quota" and site_id is not None:
            raise ValidationFailed("宅基地指标不挂靠集中安置点容量")
        if kind != "homestead-quota" and site_id is None:
            raise ValidationFailed("周转房和集中安置房必须挂靠安置点以校验基础设施容量")
        tier = raw.get("quality_tier", 1)
        if isinstance(tier, bool) or not isinstance(tier, int) or tier < 1:
            raise ValidationFailed("quality_tier 必须是不小于 1 的整数")
        restrictions_raw = raw.get("restrictions", {})
        if not isinstance(restrictions_raw, Mapping):
            raise ValidationFailed("restrictions 必须是对象")
        restrictions: dict[str, Any] = {}
        if kind == "homestead-quota":
            eligible = restrictions_raw.get("eligible_villages", [])
            if not isinstance(eligible, list) or any(not isinstance(item, str) or not item.strip() for item in eligible):
                raise ValidationFailed("restrictions.eligible_villages 必须是字符串列表")
            restrictions["eligible_villages"] = [item.strip() for item in eligible]
            self_build = restrictions_raw.get("self_build_only", True)
            if not isinstance(self_build, bool):
                raise ValidationFailed("restrictions.self_build_only 必须是布尔值")
            restrictions["self_build_only"] = self_build
        elif restrictions_raw:
            raise ValidationFailed("只有宅基地指标可以携带地块限制")
        return cls(
            resource_id=identifier(raw.get("resource_id"), "resource_id"),
            kind=kind,
            site_id=site_id,
            township=required_text(raw.get("township"), "township"),
            village=required_text(raw.get("village"), "village"),
            area_sqm=decimal_value(raw.get("area_sqm"), "area_sqm", minimum=Decimal("0.01")),
            beds=positive_integer(raw.get("beds"), "beds"),
            accessible=_bool(raw.get("accessible", False), "accessible"),
            school_km=decimal_value(raw.get("school_km"), "school_km", minimum=Decimal("0")),
            medical_km=decimal_value(raw.get("medical_km"), "medical_km", minimum=Decimal("0")),
            commute_km=decimal_value(raw.get("commute_km"), "commute_km", minimum=Decimal("0")),
            quality_tier=tier,
            restrictions=restrictions,
        )


def _bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是布尔值")
    return value


@dataclass(frozen=True, slots=True)
class HouseholdApplication:
    """危房家庭的过渡安置申请：有序约束、面积/半径限制和可接受降级范围。"""

    household_id: str
    head_name: str
    origin_village: str
    member_count: int
    constraints: tuple[HouseholdConstraint, ...]
    preference_order: tuple[str, ...]
    max_downgrade: int
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "HouseholdApplication":
        household_id = identifier(raw.get("household_id"), "household_id")
        constraints = cls._constraints(raw)
        preference_raw = raw.get("preference_order")
        if not isinstance(preference_raw, list) or not preference_raw:
            raise ValidationFailed("preference_order 必须是非空资源类型列表")
        preference: list[str] = []
        for index, item in enumerate(preference_raw):
            kind = _kind(item, f"preference_order[{index}]")
            if kind in preference:
                raise ValidationFailed("preference_order 不能重复")
            preference.append(kind)
        max_downgrade = raw.get("max_downgrade", 0)
        if isinstance(max_downgrade, bool) or not isinstance(max_downgrade, int) or not 0 <= max_downgrade < len(preference):
            raise ValidationFailed("max_downgrade 必须是 0 到偏好序列长度减一的整数")
        return cls(
            household_id=household_id,
            head_name=required_text(raw.get("head_name"), "head_name"),
            origin_village=required_text(raw.get("origin_village"), "origin_village"),
            member_count=positive_integer(raw.get("member_count"), "member_count"),
            constraints=tuple(constraints),
            preference_order=tuple(preference),
            max_downgrade=max_downgrade,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )

    @staticmethod
    def _constraints(raw: Mapping[str, Any]) -> list[HouseholdConstraint]:
        if "constraints" in raw:
            constraints_raw = raw["constraints"]
            if not isinstance(constraints_raw, list) or not constraints_raw:
                raise ValidationFailed("constraints 必须是非空列表")
            constraints = [
                HouseholdConstraint.from_dict(item, index)
                for index, item in enumerate(constraints_raw)
            ]
            if len({item.code for item in constraints}) != len(constraints):
                raise ValidationFailed("constraints 中存在重复约束")
            return constraints
        constraints = [
            HouseholdConstraint("household_size", True, None),
        ]
        if _bool(raw.get("accessible_required", False), "accessible_required"):
            constraints.append(HouseholdConstraint("accessible", True, None))
        constraints.append(
            HouseholdConstraint(
                "min_area",
                True,
                decimal_value(raw.get("min_area_sqm"), "min_area_sqm", minimum=Decimal("0.01")),
            )
        )
        constraints.append(
            HouseholdConstraint(
                "school_radius",
                True,
                decimal_value(raw.get("school_radius_km"), "school_radius_km", minimum=Decimal("0")),
            )
        )
        constraints.append(
            HouseholdConstraint(
                "medical_radius",
                True,
                decimal_value(raw.get("medical_radius_km"), "medical_radius_km", minimum=Decimal("0")),
            )
        )
        if raw.get("commute_radius_km") is not None:
            constraints.append(
                HouseholdConstraint(
                    "commute_radius",
                    True,
                    decimal_value(raw.get("commute_radius_km"), "commute_radius_km", minimum=Decimal("0")),
                )
            )
        return constraints

    def constraint_map(self) -> dict[str, HouseholdConstraint]:
        return {item.code: item for item in self.constraints}

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "household_id": self.household_id,
            "head_name": self.head_name,
            "origin_village": self.origin_village,
            "member_count": self.member_count,
            "constraints": [item.to_dict() for item in self.constraints],
            "preference_order": list(self.preference_order),
            "max_downgrade": self.max_downgrade,
        }
