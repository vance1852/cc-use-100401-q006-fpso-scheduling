"""确定性的罐容平衡、靠泊窗口、装船模拟、降产换罐建议与候补推进计算。

所有函数都是纯函数：输入目录数据与计划内容，输出可序列化的评估结果，
不访问数据库和时钟，保证同一输入永远得到同一计划分数与容量占用。
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable, Mapping, Sequence


ZERO = Decimal("0")
HUNDRED = Decimal("100")
VOLUME = Decimal("0.001")


def quantize_volume(value: Decimal) -> Decimal:
    return value.quantize(VOLUME, rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value.quantize(VOLUME, rounding=ROUND_HALF_UP), "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _hour_key(value: str) -> str:
    from .clock import parse_utc, utc_text

    return utc_text(parse_utc(value, "hour_start"))


def window_hours(starts_at: str, ends_at: str) -> list[str]:
    """返回靠泊窗口覆盖的整点小时（左闭右开）。"""
    from .clock import parse_utc, utc_text

    start = parse_utc(starts_at)
    end = parse_utc(ends_at)
    if (start.minute, start.second, start.microsecond) != (0, 0, 0):
        raise ValueError("starts_at 必须是整点小时")
    if (end.minute, end.second, end.microsecond) != (0, 0, 0):
        raise ValueError("ends_at 必须是整点小时")
    hours: list[str] = []
    cursor = start
    while cursor < end:
        hours.append(utc_text(cursor))
        cursor += timedelta(hours=1)
    return hours


def _sea_lookup(sea_observations: Iterable[Mapping[str, Any]]) -> dict[str, Decimal]:
    return {_hour_key(str(row["hour_start"])): Decimal(str(row["wave_height_m"])) for row in sea_observations}


def _violation(code: str, message: str, **detail: object) -> dict[str, Any]:
    return {"code": code, "message": message, **detail}


def simulate_tanks(
    *,
    hours: Sequence[str],
    sea: Mapping[str, Decimal],
    wave_limit: Decimal,
    loading_rate: Decimal,
    target: Decimal,
    cut_percent: Decimal,
    storage_entries: Sequence[Mapping[str, Any]],
    loaded_baseline: Decimal = ZERO,
) -> dict[str, Any]:
    """按小时模拟各储罐液位与装船抽取。

    加工来油每小时持续进罐（受降产比例影响），装船仅在海况允许的小时进行，
    抽取按条目编号顺序进行，且不能低于罐底余量。``loaded_baseline`` 为本次
    模拟开始前已经实际装船的绝对量，使累计装船量跨修订保持可比。
    """
    levels = {str(item["entry_id"]): Decimal(str(item["opening_level_m3"])) for item in storage_entries}
    rates = {
        str(item["entry_id"]): (ZERO if item.get("rate_m3d") is None else Decimal(str(item["rate_m3d"])))
        for item in storage_entries
    }
    inflow_factor = max(ZERO, Decimal(1) - cut_percent / HUNDRED)
    tank_rows: dict[str, list[dict[str, str]]] = {str(item["entry_id"]): [] for item in storage_entries}

    def _inflow_up_to(item: Mapping[str, Any], hour_index: int) -> Decimal:
        # 按累计量量化再做差，避免逐小时四舍五入在整日累计时丢量。
        rate = rates[str(item["entry_id"])]
        return quantize_volume(rate * Decimal(hour_index) / Decimal(24) * inflow_factor)

    inflow_cumulative = {str(item["entry_id"]): ZERO for item in storage_entries}
    remaining_segment = target
    segment_loaded = ZERO
    loaded = quantize_volume(loaded_baseline)
    overflow_hours: list[dict[str, Any]] = []
    heel_hours: list[dict[str, Any]] = []
    good_hours = 0
    operational_end: str | None = None
    for hour_index, hour in enumerate(hours, start=1):
        wave = sea.get(hour)
        good = wave is not None and wave <= wave_limit
        sea_gap = wave is None
        if good:
            good_hours += 1
        # 本小时各罐入罐量：累计量化的差值，整日总量无损。
        inflows = {
            entry_id: quantize_volume(_inflow_up_to(item, hour_index) - inflow_cumulative[entry_id])
            for item in storage_entries
            for entry_id in (str(item["entry_id"]),)
        }
        for entry_id, value in inflows.items():
            inflow_cumulative[entry_id] = quantize_volume(inflow_cumulative[entry_id] + value)
        draws = {str(item["entry_id"]): ZERO for item in storage_entries}
        if remaining_segment > ZERO and good:
            want = min(loading_rate, remaining_segment)
            for item in storage_entries:
                entry_id = str(item["entry_id"])
                heel = Decimal(str(item["heel_m3"]))
                available_now = levels[entry_id] + inflows[entry_id] - heel
                take = quantize_volume(min(max(ZERO, want), max(ZERO, available_now)))
                draws[entry_id] += take
                want = quantize_volume(want - take)
                if want == ZERO:
                    break
            hour_take = quantize_volume(sum(draws.values(), ZERO))
            segment_loaded = quantize_volume(segment_loaded + hour_take)
            loaded = quantize_volume(loaded + hour_take)
            remaining_segment = quantize_volume(target - segment_loaded)
            if segment_loaded >= target and operational_end is None:
                operational_end = hour
        for item in storage_entries:
            entry_id = str(item["entry_id"])
            capacity = Decimal(str(item["capacity_m3"]))
            heel = Decimal(str(item["heel_m3"]))
            inflow = inflows[entry_id]
            draw = quantize_volume(draws[entry_id])
            level = quantize_volume(levels[entry_id] + inflow - draw)
            if level > capacity:
                overflow_hours.append({"entry_id": entry_id, "hour_start": hour, "level_m3": decimal_text(level)})
            if level < heel:
                heel_hours.append({"entry_id": entry_id, "hour_start": hour, "level_m3": decimal_text(level)})
            utilization = ZERO if capacity == ZERO else (level / capacity * HUNDRED).quantize(Decimal("0.01"))
            tank_rows[entry_id].append({
                "hour_start": hour,
                "inflow_m3": decimal_text(inflow),
                "draw_m3": decimal_text(draw),
                "level_m3": decimal_text(level),
                "cumulative_loaded_m3": decimal_text(loaded),
                "utilization_percent": format(utilization, "f"),
                "sea_state": "gap" if sea_gap else ("operable" if good else "rough"),
            })
            levels[entry_id] = level
    peak = ZERO
    for rows in tank_rows.values():
        for row in rows:
            peak = max(peak, Decimal(row["level_m3"]))
    return {
        "tank_hours": tank_rows,
        "loaded_m3": loaded,
        "segment_loaded_m3": segment_loaded,
        "shortfall_m3": quantize_volume(max(ZERO, target - segment_loaded)),
        "good_hours": good_hours,
        "operational_end": operational_end,
        "overflow_hours": overflow_hours,
        "heel_hours": heel_hours,
        "peak_level_m3": peak,
    }


def required_production_cut(
    *,
    hours: Sequence[str],
    sea: Mapping[str, Decimal],
    wave_limit: Decimal,
    loading_rate: Decimal,
    target: Decimal,
    storage_entries: Sequence[Mapping[str, Any]],
    loaded_baseline: Decimal = ZERO,
) -> Decimal | None:
    """以 0.1% 为步长搜索防止储罐溢流所需的最小降产比例。"""
    for step in range(0, 1001):
        cut = Decimal(step) / Decimal(10)
        outcome = simulate_tanks(
            hours=hours,
            sea=sea,
            wave_limit=wave_limit,
            loading_rate=loading_rate,
            target=target,
            cut_percent=cut,
            storage_entries=storage_entries,
            loaded_baseline=loaded_baseline,
        )
        if not outcome["overflow_hours"]:
            return cut
    return None


def switch_suggestions(
    *,
    grade: str,
    source_water_cut: Decimal,
    tanks: Sequence[Mapping[str, Any]],
    exclude_tank_ids: Iterable[str],
) -> list[dict[str, Any]]:
    """静态列出可容纳降级/高含水来油的备用罐，按可用罐容从大到小排列。"""
    excluded = set(exclude_tank_ids)
    suggestions: list[dict[str, Any]] = []
    for tank in tanks:
        tank_id = str(tank["tank_id"])
        if tank_id in excluded or not tank.get("active", 1):
            continue
        compatible = grade in list(tank["compatible_grades"])
        water_ok = source_water_cut <= Decimal(str(tank["max_water_cut_percent"]))
        if compatible and water_ok:
            usable = Decimal(str(tank["capacity_m3"])) - Decimal(str(tank["heel_m3"]))
            suggestions.append({
                "tank_id": tank_id,
                "name": str(tank["name"]),
                "usable_capacity_m3": decimal_text(usable),
            })
    suggestions.sort(key=lambda item: (-Decimal(item["usable_capacity_m3"]), item["tank_id"]))
    return suggestions


def evaluate_plan(
    *,
    plan: Mapping[str, Any],
    tanks: Mapping[str, Mapping[str, Any]],
    batches: Mapping[str, Mapping[str, Any]],
    vessels: Mapping[str, Mapping[str, Any]],
    windows: Mapping[str, Mapping[str, Any]],
    sea_versions: Mapping[str, Mapping[str, Any]],
    quality_limits: Mapping[str, Mapping[str, Any]],
    all_tanks: Sequence[Mapping[str, Any]],
    loaded_baseline: Decimal = ZERO,
) -> dict[str, Any]:
    """评估单个计划，返回可行性、违规明细、小时桶占用、建议与可比分数。"""
    violations: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    vessel_id = str(plan["vessel_id"])
    window_id = str(plan["window_id"])
    grade = str(plan["load_grade"]).upper()
    target = Decimal(str(plan["load_target_m3"]))
    proposed_cut = Decimal(str(plan.get("production_cut_percent", 0)))
    storage_entries = list(plan["storage_entries"])
    wait_entries = list(plan.get("wait_entries", []))

    vessel = vessels.get(vessel_id)
    window = windows.get(window_id)
    sea_version = sea_versions.get(str(plan["sea_state_version_id"]))
    if vessel is None:
        raise ValueError(f"提油轮 {vessel_id} 不存在")
    if window is None:
        raise ValueError(f"靠泊窗口 {window_id} 不存在")
    if sea_version is None:
        raise ValueError(f"海况数据版本 {plan['sea_state_version_id']} 不存在")
    if grade not in list(vessel["compatible_grades"]):
        violations.append(_violation("vessel_grade_incompatible", f"提油轮 {vessel_id} 不适装 {grade}", vessel_id=vessel_id))
    quality = quality_limits.get(grade)
    if quality is None:
        violations.append(_violation("quality_limit_missing", f"缺少 {grade} 的质量界限"))
    if loaded_baseline == ZERO and not (
        Decimal(str(vessel["min_cargo_m3"])) <= target <= Decimal(str(vessel["max_cargo_m3"]))
    ):
        violations.append(_violation(
            "vessel_cargo_out_of_range",
            f"计划装船 {target} 超出提油轮载货区间 "
            f"{vessel['min_cargo_m3']}~{vessel['max_cargo_m3']}",
            min_cargo_m3=str(vessel["min_cargo_m3"]),
            max_cargo_m3=str(vessel["max_cargo_m3"]),
        ))
    if loaded_baseline > ZERO and target > Decimal(str(vessel["max_cargo_m3"])):
        violations.append(_violation(
            "vessel_cargo_out_of_range",
            f"剩余装船 {target} 超出提油轮最大载货 {vessel['max_cargo_m3']}",
            max_cargo_m3=str(vessel["max_cargo_m3"]),
        ))

    enriched: list[dict[str, Any]] = []
    used_tanks: set[str] = set()
    for item in storage_entries:
        entry_id = str(item["entry_id"])
        tank = tanks.get(str(item["tank_id"]))
        if tank is None:
            raise ValueError(f"储罐 {item['tank_id']} 不存在")
        used_tanks.add(str(tank["tank_id"]))
        opening = Decimal(str(item["opening_level_m3"]))
        if opening < Decimal(str(tank["heel_m3"])):
            violations.append(_violation("opening_below_heel", f"条目 {entry_id} 初始液位低于罐底余量", entry_id=entry_id))
        if opening > Decimal(str(tank["capacity_m3"])):
            violations.append(_violation("opening_over_capacity", f"条目 {entry_id} 初始液位超过罐容", entry_id=entry_id))
        if grade not in list(tank["compatible_grades"]):
            violations.append(_violation("tank_grade_incompatible", f"储罐 {tank['tank_id']} 不兼容 {grade}", entry_id=entry_id, tank_id=str(tank["tank_id"])))
        batch_water = ZERO
        batch_id = item.get("batch_id")
        if batch_id:
            batch = batches.get(str(batch_id))
            if batch is None:
                raise ValueError(f"加工批次 {batch_id} 不存在")
            if str(batch["grade"]).upper() != grade:
                violations.append(_violation("batch_grade_mismatch", f"批次 {batch_id} 品位与装船品位 {grade} 不一致", entry_id=entry_id))
            batch_water = Decimal(str(batch["water_cut_percent"]))
            if batch_water > Decimal(str(tank["max_water_cut_percent"])):
                violations.append(_violation(
                    "tank_water_exceeded",
                    f"批次 {batch_id} 含水率 {batch_water}% 超过储罐 {tank['tank_id']} 上限 "
                    f"{tank['max_water_cut_percent']}%",
                    entry_id=entry_id,
                    tank_id=str(tank["tank_id"]),
                ))
        if quality is not None and batch_id and batch_water > Decimal(str(quality["max_water_cut_percent"])):
            violations.append(_violation(
                "quality_downgrade_required",
                f"批次 {batch_id} 含水率超出 {grade} 质量界限，需要质量降级",
                entry_id=entry_id,
            ))
        enriched.append({
            "entry_id": entry_id,
            "tank_id": str(tank["tank_id"]),
            "opening_level_m3": decimal_text(opening),
            "rate_m3d": None if item.get("rate_m3d") is None else decimal_text(Decimal(str(item["rate_m3d"]))),
            "capacity_m3": str(tank["capacity_m3"]),
            "heel_m3": str(tank["heel_m3"]),
        })

    hours = window_hours(str(window["starts_at"]), str(window["ends_at"]))
    sea = _sea_lookup(sea_version["observations"])
    missing = [hour for hour in hours if hour not in sea]
    if missing:
        violations.append(_violation("sea_data_gap", f"海况版本缺少 {len(missing)} 个小时的观测", first_missing_hour=missing[0]))
    rough_hours = [hour for hour in hours if hour in sea and sea[hour] > Decimal(str(window["max_wave_height_m"]))]
    wave_limit = Decimal(str(window["max_wave_height_m"]))
    rate = Decimal(str(window["loading_rate_m3h"]))
    good_hours_count = len(hours) - len(set(rough_hours)) - len(missing)
    required_hours = -(-target // rate)  # 向上取整
    window_capacity = quantize_volume(rate * Decimal(good_hours_count))
    if good_hours_count < required_hours:
        violations.append(_violation(
            "window_capacity_shortfall",
            f"窗口可作业小时 {good_hours_count} 不足，至少需要 {required_hours} 小时",
            operable_hours=good_hours_count,
            required_hours=int(required_hours),
        ))
    if window_capacity < target:
        violations.append(_violation(
            "window_volume_shortfall",
            f"窗口可装船 {window_capacity} 立方米，小于计划 {target} 立方米",
            window_capacity_m3=decimal_text(window_capacity),
        ))

    outcome = simulate_tanks(
        hours=hours,
        sea=sea,
        wave_limit=wave_limit,
        loading_rate=rate,
        target=target,
        cut_percent=proposed_cut,
        storage_entries=enriched,
        loaded_baseline=loaded_baseline,
    )
    for item in outcome["overflow_hours"]:
        violations.append(_violation("tank_overflow_risk", f"储罐在 {item['hour_start']} 存在溢流风险", **item))
    for item in outcome["heel_hours"]:
        violations.append(_violation("tank_heel_breach", f"储罐在 {item['hour_start']} 低于罐底余量", **item))
    if outcome["shortfall_m3"] > ZERO:
        violations.append(_violation(
            "insufficient_oil",
            f"未完成装船部分计划 {target} 立方米，罐内及窗口剩余时间来油仅够再装 "
            f"{outcome['segment_loaded_m3']} 立方米，短缺 {outcome['shortfall_m3']} 立方米",
            segment_target_m3=decimal_text(target),
            segment_loadable_m3=decimal_text(outcome["segment_loaded_m3"]),
        ))
    minimum_cut = required_production_cut(
        hours=hours,
        sea=sea,
        wave_limit=wave_limit,
        loading_rate=rate,
        target=target,
        storage_entries=enriched,
        loaded_baseline=loaded_baseline,
    )
    must_switch = minimum_cut is None
    if minimum_cut is not None and proposed_cut + Decimal("0.0001") < minimum_cut:
        warnings.append(_violation(
            "production_cut_advised",
            f"建议降产至 {minimum_cut}% 以避免储罐溢流",
            advised_cut_percent=decimal_text(minimum_cut),
        ))
    if must_switch:
        violations.append(_violation("tank_switch_required", "即使全量降产仍会溢流，必须临时换罐"))
    suggestions = switch_suggestions(
        grade=grade,
        source_water_cut=max(
            (Decimal(str(batches[str(i['batch_id'])]["water_cut_percent"])) for i in storage_entries if i.get("batch_id")),
            default=ZERO,
        ),
        tanks=all_tanks,
        exclude_tank_ids=used_tanks,
    )

    waitlist: list[dict[str, Any]] = []
    for index, item in enumerate(sorted(wait_entries, key=lambda row: (int(row["priority"]), str(row["entry_id"])))):
        wait_vessel = vessels.get(str(item["vessel_id"]))
        promotable = True
        reason = "可在主计划航次取消或窗口释放后推进"
        if wait_vessel is None:
            promotable, reason = False, "提油轮不存在"
        elif grade not in list(wait_vessel["compatible_grades"]):
            promotable, reason = False, f"候补船舶 {item['vessel_id']} 不适装 {grade}"
        else:
            requested = Decimal(str(item["requested_m3"]))
            if not (Decimal(str(wait_vessel["min_cargo_m3"])) <= requested <= Decimal(str(wait_vessel["max_cargo_m3"]))):
                promotable, reason = False, "候补载货量超出该船适配区间"
            elif requested > window_capacity:
                promotable, reason = False, "窗口可作业容量不足以满足候补载货量"
        waitlist.append({
            "entry_id": str(item["entry_id"]),
            "vessel_id": str(item["vessel_id"]),
            "priority": int(item["priority"]),
            "frozen_rank": index + 1,
            "requested_m3": decimal_text(Decimal(str(item["requested_m3"]))),
            "promotable": promotable,
            "reason": reason,
        })

    peak_utilization = ZERO
    for rows in outcome["tank_hours"].values():
        for row in rows:
            peak_utilization = max(peak_utilization, Decimal(row["utilization_percent"]))
    score_points = 0
    if violations:
        score_points += 1_000_000 * len(violations)
    if minimum_cut is not None:
        score_points += int(minimum_cut * 100)
    if peak_utilization > 85:
        score_points += int((peak_utilization - 85) * 10)
    score_points += len(rough_hours) * 500
    score_points += int(outcome["shortfall_m3"] * 10)
    if must_switch:
        score_points += 5000

    tank_buckets: list[dict[str, Any]] = []
    for entry_id, rows in outcome["tank_hours"].items():
        tank_id = next(str(item["tank_id"]) for item in enriched if item["entry_id"] == entry_id)
        for row in rows:
            tank_buckets.append({
                "bucket_kind": "tank",
                "resource_id": tank_id,
                "hour_start": row["hour_start"],
                "reserved_m3": row["level_m3"],
                "exclusive": 0,
            })
    exclusive_buckets: list[dict[str, Any]] = []
    for kind in ("window", "vessel"):
        resource_id = window_id if kind == "window" else vessel_id
        for hour in hours:
            exclusive_buckets.append({
                "bucket_kind": kind,
                "resource_id": resource_id,
                "hour_start": hour,
                "reserved_m3": decimal_text(ZERO),
                "exclusive": 1,
            })

    return {
        "feasible": not violations,
        "violations": violations,
        "warnings": warnings,
        "load_grade": grade,
        "window_hours": len(hours),
        "operable_hours": good_hours_count,
        "rough_hours": len(rough_hours),
        "required_loading_hours": int(required_hours),
        "operational_end": outcome["operational_end"],
        "loaded_baseline_m3": decimal_text(loaded_baseline),
        "segment_target_m3": decimal_text(target),
        "segment_loaded_m3": decimal_text(outcome["segment_loaded_m3"]),
        "loaded_m3": decimal_text(outcome["loaded_m3"]),
        "max_loadable_m3": decimal_text(outcome["loaded_m3"]),
        "shortfall_m3": decimal_text(outcome["shortfall_m3"]),
        "advised_cut_percent": None if minimum_cut is None else decimal_text(minimum_cut),
        "tank_switch_required": must_switch,
        "switch_suggestions": suggestions,
        "tank_hours": {entry_id: rows for entry_id, rows in outcome["tank_hours"].items()},
        "waitlist": waitlist,
        "score": {
            "points": score_points,
            "violations": len(violations),
            "peak_utilization_percent": format(peak_utilization, "f"),
            "advised_cut_percent": None if minimum_cut is None else decimal_text(minimum_cut),
            "rough_hours": len(rough_hours),
        },
        "buckets": tank_buckets + exclusive_buckets,
    }
