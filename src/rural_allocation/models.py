"""乡镇片区调度领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
COMPENSATION_INDEXES = {"PEAK_VALLEY", "MARKET_ASSESSED", "POLICY_GUIDED", "NEGOTIATED", "GOVERNMENT_SET", "CUSTOM"}
PRODUCTS = {"cultivated-land", "homestead", "resettlement-home", "facility-land", "forest-land", "reserve-land"}
ROUTE_KINDS = {"land-pool", "settlement", "household", "storage", "service-site"}
RELOCATION_KINDS = ("resettlement-home", "turnover-home", "homestead-quota")
RELOCATION_CONSTRAINTS = ("accessible", "min_area", "commute", "school_radius", "clinic_radius")
ENFORCE_MODES = ("hard", "soft")


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field} 必须是正整数")
    return value


def boolean_value(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是布尔值")
    return value


def optional_integer(value: object, field: str, *, minimum: int, maximum: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValidationFailed(f"{field} 必须是 {minimum} 到 {maximum} 的整数")
    return value


def optional_decimal(value: object, field: str, *, minimum: Decimal, maximum: Decimal) -> Decimal | None:
    if value is None:
        return None
    return decimal_value(value, field, minimum=minimum, maximum=maximum)


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DD 日期") from exc


@dataclass(frozen=True, slots=True)
class IndexQuote:
    market_index: str
    trade_date: str
    close_cny: Decimal
    source_revision: str
    observed_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "IndexQuote":
        market_index = required_text(raw.get("market_index"), "market_index", 16).upper()
        if market_index not in COMPENSATION_INDEXES - {"CUSTOM"}:
            raise ValidationFailed("market_index 必须是 PEAK_VALLEY、MARKET_ASSESSED、POLICY_GUIDED、NEGOTIATED 或 GOVERNMENT_SET")
        observed_at = required_text(raw.get("observed_at"), "observed_at", 40)
        try:
            parse_utc(observed_at, "observed_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return cls(
            market_index=market_index,
            trade_date=date_text(raw.get("trade_date"), "trade_date"),
            close_cny=decimal_value(raw.get("close_cny"), "close_cny", minimum=Decimal("0.01")),
            source_revision=identifier(raw.get("source_revision"), "source_revision"),
            observed_at=observed_at,
        )


@dataclass(frozen=True, slots=True)
class Facility:
    facility_id: str
    name: str
    kind: str
    timezone: str
    capacity_mu: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Facility":
        kind = required_text(raw.get("kind"), "kind", 24)
        if kind not in ROUTE_KINDS:
            raise ValidationFailed("kind 不是受支持的设施类型")
        timezone = required_text(raw.get("timezone"), "timezone", 64)
        if "/" not in timezone and timezone != "UTC":
            raise ValidationFailed("timezone 必须是 IANA 时区或 UTC")
        return cls(
            facility_id=identifier(raw.get("facility_id"), "facility_id"),
            name=required_text(raw.get("name"), "name"),
            kind=kind,
            timezone=timezone,
            capacity_mu=decimal_value(
                raw.get("capacity_mu"), "capacity_mu", minimum=Decimal("0")
            ),
        )


@dataclass(frozen=True, slots=True)
class Route:
    route_id: str
    origin_id: str
    destination_id: str
    product: str
    daily_capacity: Decimal
    loss_basis_points: int
    transit_hours: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Route":
        product = required_text(raw.get("product"), "product", 32)
        if product not in PRODUCTS:
            raise ValidationFailed("product 不是受支持的土地类型")
        loss = raw.get("loss_basis_points", 0)
        if isinstance(loss, bool) or not isinstance(loss, int) or not 0 <= loss <= 1000:
            raise ValidationFailed("loss_basis_points 必须是 0 到 1000 的整数")
        origin = identifier(raw.get("origin_id"), "origin_id")
        destination = identifier(raw.get("destination_id"), "destination_id")
        if origin == destination:
            raise ValidationFailed("地块资源池起点和终点不能相同")
        return cls(
            route_id=identifier(raw.get("route_id"), "route_id"),
            origin_id=origin,
            destination_id=destination,
            product=product,
            daily_capacity=decimal_value(
                raw.get("daily_capacity"), "daily_capacity", minimum=Decimal("0.001")
            ),
            loss_basis_points=loss,
            transit_hours=positive_integer(raw.get("transit_hours"), "transit_hours"),
        )


@dataclass(frozen=True, slots=True)
class InventoryLot:
    lot_id: str
    facility_id: str
    product: str
    grade: str
    quantity_mu: Decimal
    unit_cost_cny: Decimal
    received_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "InventoryLot":
        product = required_text(raw.get("product"), "product", 32)
        if product not in PRODUCTS:
            raise ValidationFailed("product 不是受支持的土地类型")
        received_at = required_text(raw.get("received_at"), "received_at", 40)
        try:
            parse_utc(received_at, "received_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return cls(
            lot_id=identifier(raw.get("lot_id"), "lot_id"),
            facility_id=identifier(raw.get("facility_id"), "facility_id"),
            product=product,
            grade=required_text(raw.get("grade"), "grade", 32).upper(),
            quantity_mu=decimal_value(
                raw.get("quantity_mu"), "quantity_mu", minimum=Decimal("0.001")
            ),
            unit_cost_cny=decimal_value(
                raw.get("unit_cost_cny"), "unit_cost_cny", minimum=Decimal("0")
            ),
            received_at=received_at,
        )


@dataclass(frozen=True, slots=True)
class NominationRequest:
    nomination_id: str
    route_id: str
    shipper_id: str
    service_date: str
    requested_mu: Decimal
    priority: int
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "NominationRequest":
        priority = raw.get("priority", 100)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 999:
            raise ValidationFailed("priority 必须是 1 到 999 的整数")
        return cls(
            nomination_id=identifier(raw.get("nomination_id"), "nomination_id"),
            route_id=identifier(raw.get("route_id"), "route_id"),
            shipper_id=identifier(raw.get("shipper_id"), "shipper_id"),
            service_date=date_text(raw.get("service_date"), "service_date"),
            requested_mu=decimal_value(
                raw.get("requested_mu"), "requested_mu", minimum=Decimal("0.001")
            ),
            priority=priority,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class SupplyScenario:
    scenario_id: str
    name: str
    market_index_drop_percent: Decimal
    route_capacity_changes: Mapping[str, Decimal]
    demand_changes: Mapping[str, Decimal]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SupplyScenario":
        route_changes = raw.get("route_capacity_changes", {})
        demand_changes = raw.get("demand_changes", {})
        if not isinstance(route_changes, Mapping) or not isinstance(demand_changes, Mapping):
            raise ValidationFailed("情景变化必须是对象")
        parsed_routes = {
            identifier(key, "route_capacity_changes 键"): decimal_value(
                value, f"route_capacity_changes.{key}", minimum=Decimal("-100"), maximum=Decimal("500")
            )
            for key, value in route_changes.items()
        }
        parsed_demand = {
            identifier(key, "demand_changes 键"): decimal_value(
                value, f"demand_changes.{key}", minimum=Decimal("-100"), maximum=Decimal("500")
            )
            for key, value in demand_changes.items()
        }
        return cls(
            scenario_id=identifier(raw.get("scenario_id"), "scenario_id"),
            name=required_text(raw.get("name"), "name"),
            market_index_drop_percent=decimal_value(
                raw.get("market_index_drop_percent", 0),
                "market_index_drop_percent",
                minimum=Decimal("-500"),
                maximum=Decimal("100"),
            ),
            route_capacity_changes=parsed_routes,
            demand_changes=parsed_demand,
        )


@dataclass(frozen=True, slots=True)
class ResettlementSiteInput:
    site_id: str
    name: str
    township: str
    village: str
    infra_capacity_households: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResettlementSiteInput":
        capacity = raw.get("infra_capacity_households")
        if isinstance(capacity, bool) or not isinstance(capacity, int) or not 0 <= capacity <= 100000:
            raise ValidationFailed("infra_capacity_households 必须是 0 到 100000 的整数")
        return cls(
            site_id=identifier(raw.get("site_id"), "site_id"),
            name=required_text(raw.get("name"), "name"),
            township=required_text(raw.get("township"), "township", 64),
            village=required_text(raw.get("village"), "village", 64),
            infra_capacity_households=capacity,
        )


@dataclass(frozen=True, slots=True)
class ResettlementResourceInput:
    resource_id: str
    site_id: str
    kind: str
    area_sqm: Decimal
    max_household_size: int
    accessible: bool
    commute_minutes: int
    school_km: Decimal
    clinic_km: Decimal
    eligible_townships: tuple[str, ...]
    capacity_households: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResettlementResourceInput":
        kind = required_text(raw.get("kind"), "kind", 32)
        if kind not in RELOCATION_KINDS:
            raise ValidationFailed("kind 必须是 resettlement-home、turnover-home 或 homestead-quota")
        townships_raw = raw.get("eligible_townships", ["*"])
        if not isinstance(townships_raw, list) or not townships_raw:
            raise ValidationFailed("eligible_townships 必须是非空数组")
        townships: list[str] = []
        for entry in townships_raw:
            township = required_text(entry, "eligible_townships", 64)
            if township in townships:
                raise ValidationFailed("eligible_townships 不能重复")
            townships.append(township)
        if "*" in townships and len(townships) > 1:
            raise ValidationFailed("eligible_townships 使用 * 时不能再列具体乡镇")
        capacity = raw.get("capacity_households", 1)
        if isinstance(capacity, bool) or not isinstance(capacity, int) or not 1 <= capacity <= 500:
            raise ValidationFailed("capacity_households 必须是 1 到 500 的整数")
        return cls(
            resource_id=identifier(raw.get("resource_id"), "resource_id"),
            site_id=identifier(raw.get("site_id"), "site_id"),
            kind=kind,
            area_sqm=decimal_value(raw.get("area_sqm"), "area_sqm", minimum=Decimal("1"), maximum=Decimal("100000")),
            max_household_size=positive_integer(raw.get("max_household_size"), "max_household_size"),
            accessible=boolean_value(raw.get("accessible", False), "accessible"),
            commute_minutes=positive_integer(raw.get("commute_minutes"), "commute_minutes"),
            school_km=decimal_value(raw.get("school_km"), "school_km", minimum=Decimal("0"), maximum=Decimal("200")),
            clinic_km=decimal_value(raw.get("clinic_km"), "clinic_km", minimum=Decimal("0"), maximum=Decimal("200")),
            eligible_townships=tuple(townships),
            capacity_households=capacity,
        )


@dataclass(frozen=True, slots=True)
class RelocationConstraint:
    name: str
    enforce: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "enforce": self.enforce}


@dataclass(frozen=True, slots=True)
class DowngradeScope:
    allowed_kinds: tuple[str, ...]
    max_area_relax_percent: Decimal
    max_extra_commute_minutes: int
    max_extra_school_km: Decimal
    max_extra_clinic_km: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "DowngradeScope":
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise ValidationFailed("downgrade_scope 必须是对象")
        kinds_raw = raw.get("allowed_kinds", list(RELOCATION_KINDS))
        if not isinstance(kinds_raw, list) or not kinds_raw:
            raise ValidationFailed("downgrade_scope.allowed_kinds 必须是非空数组")
        kinds: list[str] = []
        for entry in kinds_raw:
            kind = required_text(entry, "downgrade_scope.allowed_kinds", 32)
            if kind not in RELOCATION_KINDS:
                raise ValidationFailed("downgrade_scope.allowed_kinds 含不支持的安置资源类型")
            if kind in kinds:
                raise ValidationFailed("downgrade_scope.allowed_kinds 不能重复")
            kinds.append(kind)
        extra_commute = raw.get("max_extra_commute_minutes", 0)
        if isinstance(extra_commute, bool) or not isinstance(extra_commute, int) or not 0 <= extra_commute <= 180:
            raise ValidationFailed("downgrade_scope.max_extra_commute_minutes 必须是 0 到 180 的整数")
        return cls(
            allowed_kinds=tuple(kinds),
            max_area_relax_percent=decimal_value(
                raw.get("max_area_relax_percent", 0),
                "downgrade_scope.max_area_relax_percent",
                minimum=Decimal("0"),
                maximum=Decimal("50"),
            ),
            max_extra_commute_minutes=extra_commute,
            max_extra_school_km=decimal_value(
                raw.get("max_extra_school_km", 0),
                "downgrade_scope.max_extra_school_km",
                minimum=Decimal("0"),
                maximum=Decimal("30"),
            ),
            max_extra_clinic_km=decimal_value(
                raw.get("max_extra_clinic_km", 0),
                "downgrade_scope.max_extra_clinic_km",
                minimum=Decimal("0"),
                maximum=Decimal("30"),
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed_kinds": list(self.allowed_kinds),
            "max_area_relax_percent": format(self.max_area_relax_percent, "f"),
            "max_extra_commute_minutes": self.max_extra_commute_minutes,
            "max_extra_school_km": format(self.max_extra_school_km, "f"),
            "max_extra_clinic_km": format(self.max_extra_clinic_km, "f"),
        }


@dataclass(frozen=True, slots=True)
class RelocationNeeds:
    household_size: int
    min_area_sqm: Decimal
    requires_accessible: bool
    max_commute_minutes: int | None
    school_radius_km: Decimal | None
    clinic_radius_km: Decimal | None
    origin_township: str
    constraints: tuple[RelocationConstraint, ...]
    downgrade: DowngradeScope

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RelocationNeeds":
        household_size = raw.get("household_size")
        if isinstance(household_size, bool) or not isinstance(household_size, int) or not 1 <= household_size <= 20:
            raise ValidationFailed("household_size 必须是 1 到 20 的整数")
        requires_accessible = boolean_value(raw.get("requires_accessible", False), "requires_accessible")
        max_commute = optional_integer(raw.get("max_commute_minutes"), "max_commute_minutes", minimum=1, maximum=600)
        school_radius = optional_decimal(
            raw.get("school_radius_km"), "school_radius_km", minimum=Decimal("0.1"), maximum=Decimal("100")
        )
        clinic_radius = optional_decimal(
            raw.get("clinic_radius_km"), "clinic_radius_km", minimum=Decimal("0.1"), maximum=Decimal("100")
        )
        constraints_raw = raw.get("constraints")
        if not isinstance(constraints_raw, list) or not constraints_raw:
            raise ValidationFailed("constraints 必须是非空数组，按重要程度排序")
        seen: set[str] = set()
        constraints: list[RelocationConstraint] = []
        for entry in constraints_raw:
            if not isinstance(entry, Mapping):
                raise ValidationFailed("constraints 元素必须是对象")
            name = required_text(entry.get("name"), "constraints.name", 32)
            if name not in RELOCATION_CONSTRAINTS:
                raise ValidationFailed(f"constraints.name 不支持 {name}")
            if name in seen:
                raise ValidationFailed("constraints 不能重复登记同名约束")
            seen.add(name)
            enforce = required_text(entry.get("enforce", "soft"), "constraints.enforce", 8)
            if enforce not in ENFORCE_MODES:
                raise ValidationFailed("constraints.enforce 必须是 hard 或 soft")
            constraints.append(RelocationConstraint(name, enforce))
        if "accessible" in seen and not requires_accessible:
            raise ValidationFailed("登记 accessible 约束时 requires_accessible 必须为 true")
        if "commute" in seen and max_commute is None:
            raise ValidationFailed("登记 commute 约束必须提供 max_commute_minutes")
        if "school_radius" in seen and school_radius is None:
            raise ValidationFailed("登记 school_radius 约束必须提供 school_radius_km")
        if "clinic_radius" in seen and clinic_radius is None:
            raise ValidationFailed("登记 clinic_radius 约束必须提供 clinic_radius_km")
        return cls(
            household_size=household_size,
            min_area_sqm=decimal_value(raw.get("min_area_sqm"), "min_area_sqm", minimum=Decimal("1"), maximum=Decimal("100000")),
            requires_accessible=requires_accessible,
            max_commute_minutes=max_commute,
            school_radius_km=school_radius,
            clinic_radius_km=clinic_radius,
            origin_township=required_text(raw.get("origin_township"), "origin_township", 64),
            constraints=tuple(constraints),
            downgrade=DowngradeScope.from_dict(raw.get("downgrade_scope")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "household_size": self.household_size,
            "min_area_sqm": format(self.min_area_sqm, "f"),
            "requires_accessible": self.requires_accessible,
            "max_commute_minutes": self.max_commute_minutes,
            "school_radius_km": None if self.school_radius_km is None else format(self.school_radius_km, "f"),
            "clinic_radius_km": None if self.clinic_radius_km is None else format(self.clinic_radius_km, "f"),
            "origin_township": self.origin_township,
            "constraints": [constraint.as_dict() for constraint in self.constraints],
            "downgrade_scope": self.downgrade.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class RelocationApplicationInput:
    application_id: str
    household_id: str
    origin_village: str
    needs: RelocationNeeds
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RelocationApplicationInput":
        return cls(
            application_id=identifier(raw.get("application_id"), "application_id"),
            household_id=identifier(raw.get("household_id"), "household_id"),
            origin_village=required_text(raw.get("origin_village"), "origin_village", 64),
            needs=RelocationNeeds.from_dict(raw),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )
