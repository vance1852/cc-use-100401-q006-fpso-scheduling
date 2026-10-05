"""储运与提油编排领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
ENTRY_KINDS = {"storage", "production-cut", "waitlist"}


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


def grade_text(value: object, field: str = "grade") -> str:
    return required_text(value, field, 32).upper()


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


def optional_decimal(value: object, field: str, **bounds: Decimal) -> Decimal | None:
    if value is None:
        return None
    return decimal_value(value, field, **bounds)


def grade_set(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)) or not value:
        raise ValidationFailed(f"{field} 必须是非空数组")
    grades = {grade_text(item, f"{field} 元素") for item in value}
    return tuple(sorted(grades))


def time_text(value: object, field: str) -> str:
    text = required_text(value, field, 40)
    try:
        parse_utc(text, field)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc
    return text


@dataclass(frozen=True, slots=True)
class QualityLimit:
    grade: str
    max_water_cut_percent: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "QualityLimit":
        return cls(
            grade=grade_text(raw.get("grade")),
            max_water_cut_percent=decimal_value(
                raw.get("max_water_cut_percent"),
                "max_water_cut_percent",
                minimum=Decimal("0"),
                maximum=Decimal("100"),
            ),
        )


@dataclass(frozen=True, slots=True)
class Tank:
    tank_id: str
    name: str
    compatible_grades: tuple[str, ...]
    max_water_cut_percent: Decimal
    capacity_m3: Decimal
    heel_m3: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Tank":
        capacity = decimal_value(raw.get("capacity_m3"), "capacity_m3", minimum=Decimal("0.001"))
        heel = decimal_value(raw.get("heel_m3", 0), "heel_m3", minimum=Decimal("0"))
        if heel >= capacity:
            raise ValidationFailed("heel_m3 必须小于 capacity_m3")
        return cls(
            tank_id=identifier(raw.get("tank_id"), "tank_id"),
            name=required_text(raw.get("name"), "name"),
            compatible_grades=grade_set(raw.get("compatible_grades"), "compatible_grades"),
            max_water_cut_percent=decimal_value(
                raw.get("max_water_cut_percent"),
                "max_water_cut_percent",
                minimum=Decimal("0"),
                maximum=Decimal("100"),
            ),
            capacity_m3=capacity,
            heel_m3=heel,
        )


@dataclass(frozen=True, slots=True)
class Vessel:
    vessel_id: str
    name: str
    compatible_grades: tuple[str, ...]
    min_cargo_m3: Decimal
    max_cargo_m3: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Vessel":
        minimum = decimal_value(raw.get("min_cargo_m3"), "min_cargo_m3", minimum=Decimal("0"))
        maximum = decimal_value(raw.get("max_cargo_m3"), "max_cargo_m3", minimum=Decimal("0.001"))
        if minimum > maximum:
            raise ValidationFailed("min_cargo_m3 不能大于 max_cargo_m3")
        return cls(
            vessel_id=identifier(raw.get("vessel_id"), "vessel_id"),
            name=required_text(raw.get("name"), "name"),
            compatible_grades=grade_set(raw.get("compatible_grades"), "compatible_grades"),
            min_cargo_m3=minimum,
            max_cargo_m3=maximum,
        )


@dataclass(frozen=True, slots=True)
class BerthWindow:
    window_id: str
    berth_id: str
    starts_at: str
    ends_at: str
    max_wave_height_m: Decimal
    loading_rate_m3h: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "BerthWindow":
        starts_at = time_text(raw.get("starts_at"), "starts_at")
        ends_at = time_text(raw.get("ends_at"), "ends_at")
        if parse_utc(ends_at) <= parse_utc(starts_at):
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        return cls(
            window_id=identifier(raw.get("window_id"), "window_id"),
            berth_id=identifier(raw.get("berth_id"), "berth_id"),
            starts_at=starts_at,
            ends_at=ends_at,
            max_wave_height_m=decimal_value(
                raw.get("max_wave_height_m"), "max_wave_height_m", minimum=Decimal("0")
            ),
            loading_rate_m3h=decimal_value(
                raw.get("loading_rate_m3h"), "loading_rate_m3h", minimum=Decimal("0.001")
            ),
        )


@dataclass(frozen=True, slots=True)
class ProcessingBatch:
    batch_id: str
    grade: str
    water_cut_percent: Decimal
    daily_rate_m3: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ProcessingBatch":
        return cls(
            batch_id=identifier(raw.get("batch_id"), "batch_id"),
            grade=grade_text(raw.get("grade")),
            water_cut_percent=decimal_value(
                raw.get("water_cut_percent"),
                "water_cut_percent",
                minimum=Decimal("0"),
                maximum=Decimal("100"),
            ),
            daily_rate_m3=decimal_value(
                raw.get("daily_rate_m3"), "daily_rate_m3", minimum=Decimal("0")
            ),
        )


@dataclass(frozen=True, slots=True)
class SeaStateVersion:
    version_id: str
    issued_at: str
    source: str
    observations: tuple["SeaStateObservation", ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SeaStateVersion":
        raw_observations = raw.get("observations")
        if not isinstance(raw_observations, list) or not raw_observations:
            raise ValidationFailed("observations 必须是非空数组")
        observations = tuple(
            SeaStateObservation.from_dict(item, index)
            for index, item in enumerate(raw_observations)
        )
        hours = {item.hour_start for item in observations}
        if len(hours) != len(observations):
            raise ValidationFailed("海况观测小时不能重复")
        return cls(
            version_id=identifier(raw.get("version_id"), "version_id"),
            issued_at=time_text(raw.get("issued_at"), "issued_at"),
            source=required_text(raw.get("source"), "source"),
            observations=tuple(sorted(observations, key=lambda item: item.hour_start)),
        )


@dataclass(frozen=True, slots=True)
class SeaStateObservation:
    hour_start: str
    wave_height_m: Decimal

    @classmethod
    def from_dict(cls, raw: object, index: int) -> "SeaStateObservation":
        if not isinstance(raw, Mapping):
            raise ValidationFailed(f"observations[{index}] 必须是对象")
        hour_start = time_text(raw.get("hour_start"), f"observations[{index}].hour_start")
        parsed = parse_utc(hour_start)
        if (parsed.minute, parsed.second, parsed.microsecond) != (0, 0, 0):
            raise ValidationFailed(f"observations[{index}].hour_start 必须是整点小时")
        return cls(
            hour_start=hour_start,
            wave_height_m=decimal_value(
                raw.get("wave_height_m"),
                f"observations[{index}].wave_height_m",
                minimum=Decimal("0"),
            ),
        )


@dataclass(frozen=True, slots=True)
class PlanStorageEntry:
    entry_id: str
    tank_id: str
    batch_id: str | None
    opening_level_m3: Decimal
    rate_m3d: Decimal | None


@dataclass(frozen=True, slots=True)
class PlanWaitEntry:
    entry_id: str
    vessel_id: str
    priority: int
    requested_m3: Decimal


@dataclass(frozen=True, slots=True)
class LiftPlanDraft:
    plan_id: str
    vessel_id: str
    window_id: str
    load_grade: str
    load_target_m3: Decimal
    sea_state_version_id: str
    production_cut_percent: Decimal
    lease_minutes: int
    storage_entries: tuple[PlanStorageEntry, ...]
    wait_entries: tuple[PlanWaitEntry, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "LiftPlanDraft":
        entries = raw.get("entries")
        if not isinstance(entries, list) or not entries:
            raise ValidationFailed("entries 必须是非空数组")
        storage: list[PlanStorageEntry] = []
        waiting: list[PlanWaitEntry] = []
        seen: set[str] = set()
        for index, item in enumerate(entries):
            if not isinstance(item, Mapping):
                raise ValidationFailed(f"entries[{index}] 必须是对象")
            kind = required_text(item.get("kind"), f"entries[{index}].kind", 24)
            if kind not in ENTRY_KINDS:
                raise ValidationFailed(f"entries[{index}].kind 必须是 storage、production-cut 或 waitlist")
            entry_id = identifier(item.get("entry_id"), f"entries[{index}].entry_id")
            if entry_id in seen:
                raise ValidationFailed(f"条目编号 {entry_id} 重复")
            seen.add(entry_id)
            if kind == "storage":
                storage.append(PlanStorageEntry(
                    entry_id=entry_id,
                    tank_id=identifier(item.get("tank_id"), f"entries[{index}].tank_id"),
                    batch_id=None if item.get("batch_id") is None else identifier(item.get("batch_id"), f"entries[{index}].batch_id"),
                    opening_level_m3=decimal_value(
                        item.get("opening_level_m3"),
                        f"entries[{index}].opening_level_m3",
                        minimum=Decimal("0"),
                    ),
                    rate_m3d=optional_decimal(
                        item.get("rate_m3d"), f"entries[{index}].rate_m3d", minimum=Decimal("0")
                    ),
                ))
            elif kind == "waitlist":
                priority = item.get("priority", 100)
                if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 999:
                    raise ValidationFailed(f"entries[{index}].priority 必须是 1 到 999 的整数")
                waiting.append(PlanWaitEntry(
                    entry_id=entry_id,
                    vessel_id=identifier(item.get("vessel_id"), f"entries[{index}].vessel_id"),
                    priority=priority,
                    requested_m3=decimal_value(
                        item.get("requested_m3"),
                        f"entries[{index}].requested_m3",
                        minimum=Decimal("0.001"),
                    ),
                ))
        if not storage:
            raise ValidationFailed("计划至少需要一个 storage 条目")
        cut_entries = [item for item in entries if required_text(item.get("kind"), "kind") == "production-cut"]
        if len(cut_entries) > 1:
            raise ValidationFailed("production-cut 条目最多一个")
        cut_value = Decimal("0")
        if cut_entries:
            cut_value = decimal_value(
                cut_entries[0].get("cut_percent", 0),
                "production-cut.cut_percent",
                minimum=Decimal("0"),
                maximum=Decimal("100"),
            )
        lease_minutes = raw.get("lease_minutes", 30)
        if isinstance(lease_minutes, bool) or not isinstance(lease_minutes, int) or not 1 <= lease_minutes <= 1440:
            raise ValidationFailed("lease_minutes 必须是 1 到 1440 的整数")
        return cls(
            plan_id=identifier(raw.get("plan_id"), "plan_id"),
            vessel_id=identifier(raw.get("vessel_id"), "vessel_id"),
            window_id=identifier(raw.get("window_id"), "window_id"),
            load_grade=grade_text(raw.get("load_grade")),
            load_target_m3=decimal_value(
                raw.get("load_target_m3"), "load_target_m3", minimum=Decimal("0.001")
            ),
            sea_state_version_id=identifier(raw.get("sea_state_version_id"), "sea_state_version_id"),
            production_cut_percent=cut_value,
            lease_minutes=lease_minutes,
            storage_entries=tuple(sorted(storage, key=lambda item: item.entry_id)),
            wait_entries=tuple(waiting),
        )
