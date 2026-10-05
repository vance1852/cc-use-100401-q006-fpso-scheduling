"""贯通目录登记、计划比较、租约预留、双确认封存、部分装船、降产、临时换罐、
质量降级、航次取消与候补推进的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import LiftingService


def _sea(version_day: str, *, rough_hours: tuple[int, ...] = ()) -> dict[str, object]:
    return {
        "version_id": f"ss-{version_day}",
        "issued_at": f"2026-09-{version_day}T00:00:00Z",
        "source": "南海预报中心",
        "supersedes_version_id": None,
        "observations": [
            {
                "hour_start": f"2026-09-{version_day}T{hour:02d}:00:00Z",
                "wave_height_m": "3.8" if hour in rough_hours else "1.2",
            }
            for hour in range(24)
        ],
    }


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
    service = LiftingService(connection, clock)

    for user_id, role, party in (
        ("plan", "planner", "operator"),
        ("ops-platform", "platform", "platform"),
        ("ops-ship", "vessel", "ship"),
        ("dispatch", "dispatcher", "operator"),
        ("audit", "auditor", "audit"),
    ):
        service.create_user(user_id, user_id, role, party)

    # 质量界限：常规合格原油含水上限 1%，降级品位 2%。
    service.register_quality_limit("plan", {"grade": "BRENT", "max_water_cut_percent": "1"})
    service.register_quality_limit("plan", {"grade": "BRENT-SOUR", "max_water_cut_percent": "2"})

    # 储罐：T1/T2 在用，T3/T4 作为临时换罐备用。
    service.register_tank("plan", {"tank_id": "T1", "name": "南舱一级沉降罐", "compatible_grades": ["BRENT"],
                                   "max_water_cut_percent": "2", "capacity_m3": "120000", "heel_m3": "2000"})
    service.register_tank("plan", {"tank_id": "T2", "name": "北舱调合罐", "compatible_grades": ["BRENT"],
                                   "max_water_cut_percent": "2", "capacity_m3": "80000", "heel_m3": "1000"})
    service.register_tank("plan", {"tank_id": "T3", "name": "高含水缓冲罐", "compatible_grades": ["BRENT", "BRENT-SOUR"],
                                   "max_water_cut_percent": "3", "capacity_m3": "90000", "heel_m3": "1000"})
    service.register_tank("plan", {"tank_id": "T4", "name": "降级品位专用罐", "compatible_grades": ["BRENT", "BRENT-SOUR"],
                                   "max_water_cut_percent": "5", "capacity_m3": "120000", "heel_m3": "0"})

    service.register_vessel("plan", {"vessel_id": "ship-a", "name": "远谭湖号", "compatible_grades": ["BRENT", "BRENT-SOUR"],
                                     "min_cargo_m3": "60000", "max_cargo_m3": "110000"})
    service.register_vessel("plan", {"vessel_id": "ship-b", "name": "鲲鹏号", "compatible_grades": ["BRENT"],
                                     "min_cargo_m3": "20000", "max_cargo_m3": "80000"})
    service.register_vessel("plan", {"vessel_id": "ship-c", "name": "南洋号", "compatible_grades": ["BRENT-SOUR"],
                                     "min_cargo_m3": "30000", "max_cargo_m3": "90000"})

    service.register_window("plan", {"window_id": "win-0925", "berth_id": "berth-1",
                                     "starts_at": "2026-09-25T00:00:00Z", "ends_at": "2026-09-26T00:00:00Z",
                                     "max_wave_height_m": "2.5", "loading_rate_m3h": "5000"})
    service.register_window("plan", {"window_id": "win-0927", "berth_id": "berth-1",
                                     "starts_at": "2026-09-27T00:00:00Z", "ends_at": "2026-09-28T00:00:00Z",
                                     "max_wave_height_m": "2.5", "loading_rate_m3h": "5000"})

    service.register_batch("plan", {"batch_id": "batch-a1", "grade": "BRENT", "water_cut_percent": "0.8",
                                    "daily_rate_m3": "2000"})
    service.register_batch("plan", {"batch_id": "batch-a2", "grade": "BRENT", "water_cut_percent": "0.6",
                                    "daily_rate_m3": "1000"})
    service.register_batch("plan", {"batch_id": "batch-b2", "grade": "BRENT-SOUR", "water_cut_percent": "1.5",
                                    "daily_rate_m3": "1000"})

    service.register_sea_state_version("plan", _sea("25", rough_hours=(22, 23)))
    service.register_sea_state_version("plan", _sea("27", rough_hours=(1,)))

    base_entries = [
        {"kind": "storage", "entry_id": "e-t1", "tank_id": "T1", "batch_id": "batch-a1",
         "opening_level_m3": "90000", "rate_m3d": "2000"},
        {"kind": "storage", "entry_id": "e-t2", "tank_id": "T2", "batch_id": "batch-a2",
         "opening_level_m3": "20000", "rate_m3d": "1000"},
    ]

    # 计划 P1：可行方案。计划 P2：高来油速率 + 高液位，存在溢流风险，需要降产。
    service.draft_plan("plan", {
        "plan_id": "P1", "vessel_id": "ship-a", "window_id": "win-0925", "load_grade": "BRENT",
        "load_target_m3": "100000", "sea_state_version_id": "ss-25", "lease_minutes": 60,
        "entries": base_entries,
    })
    service.draft_plan("plan", {
        "plan_id": "P2", "vessel_id": "ship-a", "window_id": "win-0925", "load_grade": "BRENT",
        "load_target_m3": "100000", "sea_state_version_id": "ss-25", "lease_minutes": 60,
        "entries": [
            {"kind": "storage", "entry_id": "e-t1", "tank_id": "T1", "batch_id": "batch-a1",
             "opening_level_m3": "115000", "rate_m3d": "48000"},
            {"kind": "storage", "entry_id": "e-t2", "tank_id": "T2", "batch_id": "batch-a2",
             "opening_level_m3": "70000", "rate_m3d": "12000"},
        ],
    })
    comparison = service.compare_plans("plan", "win-0925")
    assert comparison["plans"][0]["plan_id"] == "P1", comparison

    # P3：09-27 航次，挂两个候补：ship-c 优先级更高但不适装常规油，ship-b 可推进。
    service.draft_plan("plan", {
        "plan_id": "P3", "vessel_id": "ship-a", "window_id": "win-0927", "load_grade": "BRENT",
        "load_target_m3": "100000", "sea_state_version_id": "ss-27", "lease_minutes": 60,
        "entries": [
            {"kind": "storage", "entry_id": "e-t1", "tank_id": "T1", "batch_id": "batch-a1",
             "opening_level_m3": "90000", "rate_m3d": "2000"},
            {"kind": "storage", "entry_id": "e-t2", "tank_id": "T2", "batch_id": "batch-a2",
             "opening_level_m3": "20000", "rate_m3d": "1000"},
            {"kind": "waitlist", "entry_id": "wait-c", "vessel_id": "ship-c",
             "priority": 5, "requested_m3": "60000"},
            {"kind": "waitlist", "entry_id": "wait-b", "vessel_id": "ship-b",
             "priority": 10, "requested_m3": "50000"},
        ],
    })

    # P1：租约预留 → 平台与船方分别确认 → 封存。
    service.reserve_plan("dispatch", "P1")
    service.confirm_plan("ops-platform", "P1", "platform")
    service.confirm_plan("ops-ship", "P1", "vessel")
    service.seal_plan("dispatch", "P1")

    # P3 同样封存，演示另一窗口的航次取消与候补推进。
    service.reserve_plan("dispatch", "P3")
    service.confirm_plan("ops-platform", "P3", "platform")
    service.confirm_plan("ops-ship", "P3", "vessel")
    service.seal_plan("dispatch", "P3")

    revisions: list[dict[str, object]] = []

    def _track(result: dict[str, object]) -> dict[str, object]:
        explanation = result["capacity_explanation"]
        revisions.append({
            "event_id": result["event_id"],
            "revision": result["revision"],
            "released_buckets": len(explanation["released"]),
            "occupied_buckets": len(explanation["occupied"]),
            "messages": explanation["messages"],
        })
        return result

    # 窗口开始后 8 小时：已装 40000（8 个可作业小时 × 5000 立方米/小时），登记部分装船。
    clock.current = datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)
    _track(service.record_partial_loading("dispatch", "P1", "40000"))
    # 海况转差预期，提前降产 15%。
    _track(service.record_production_cut("dispatch", "P1", "15"))

    # 临时换罐：用当前修订条目的期初液位（生效时刻液位），把 T1/T2 的未完成部分分别转到 T4/T3。
    def _level(plan_id: str, entry_id: str) -> str:
        for entry in service.plan(plan_id)["entries"]:
            if entry["entry_id"] == entry_id and entry["kind"] == "storage":
                return entry["opening_level_m3"]
        raise KeyError(entry_id)

    level_t1 = _level("P1", "e-t1")
    level_t2 = _level("P1", "e-t2")
    _track(service.switch_tank("dispatch", "P1", "e-t1", "T4", level_t1))
    _track(service.switch_tank("dispatch", "P1", "e-t2", "T3", level_t2))
    # 质量降级到 BRENT-SOUR，未完成部分按新品位与新批次重新校验。
    _track(service.downgrade_quality("dispatch", "P1", "BRENT-SOUR", "batch-b2"))
    # 20:00 时累计 20 个可作业小时（22、23 时大浪不能作业），装船全部完成。
    clock.current = datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc)
    finished = _track(service.record_partial_loading("dispatch", "P1", "100000"))

    # 09-27 窗口内航次取消，候补按冻结优先级推进（wait-c 不适装被跳过，wait-b 取得租约）。
    clock.current = datetime(2026, 9, 27, 2, 0, tzinfo=timezone.utc)
    cancelled = service.cancel_voyage("dispatch", "P3")
    promoted = cancelled["promoted"]
    events_p1 = service.plan_events("audit", "P1")
    capacity = service.capacity_view("audit", "win-0925")
    audit = service.audit_chain("audit")
    result = {
        "status": "ok",
        "comparison_ranking": [item["plan_id"] for item in comparison["plans"]],
        "p2_advised_cut_percent": comparison["plans"][1]["advised_cut_percent"],
        "sealed_plan": "P1",
        "final_revision": finished["revision"],
        "revisions": revisions,
        "cancelled_plan": "P3",
        "promoted_waitlist_entry": promoted["entry_id"],
        "promoted_plan_id": promoted["plan_id"],
        "promoted_frozen_rank": promoted["frozen_rank"],
        "p1_event_count": len(events_p1["events"]),
        "win_0925_active_buckets_after_completion": len(capacity["view"]),
        "audit": audit,
        "workspace": workspace.name,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行储运与提油编排离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
