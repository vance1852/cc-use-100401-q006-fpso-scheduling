from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from lifting_orchestration.api import JsonApplication
from lifting_orchestration.clock import FrozenClock
from lifting_orchestration.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from lifting_orchestration.scheduling import (
    evaluate_plan,
    quantize_volume,
    simulate_tanks,
    window_hours,
)
from lifting_orchestration.service import LiftingService


BASE_TIME = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def sea_payload(day: str, *, rough=()) -> dict:
    return {
        "version_id": f"ss-{day}",
        "issued_at": f"2026-09-{day}T00:00:00Z",
        "source": "南海预报中心",
        "observations": [
            {"hour_start": f"2026-09-{day}T{h:02d}:00:00Z",
             "wave_height_m": "4.0" if h in rough else "1.0"}
            for h in range(24)
        ],
    }


class SchedulingFunctionTests(unittest.TestCase):
    def test_window_hours_are_half_open_and_reject_partial_hours(self) -> None:
        hours = window_hours("2026-09-25T00:00:00Z", "2026-09-25T03:00:00Z")
        self.assertEqual(hours, [
            "2026-09-25T00:00:00Z", "2026-09-25T01:00:00Z", "2026-09-25T02:00:00Z",
        ])
        with self.assertRaises(ValueError):
            window_hours("2026-09-25T00:30:00Z", "2026-09-25T03:00:00Z")

    def test_simulation_does_not_load_in_rough_hours_and_respects_heel(self) -> None:
        hours = window_hours("2026-09-25T00:00:00Z", "2026-09-25T04:00:00Z")
        sea = {hour: Decimal("4") for hour in hours}
        sea["2026-09-25T02:00:00Z"] = Decimal("1")
        entry = {"entry_id": "e1", "tank_id": "T1", "opening_level_m3": "5000",
                 "capacity_m3": "100000", "heel_m3": "2000", "rate_m3d": "0"}
        outcome = simulate_tanks(
            hours=hours, sea=sea, wave_limit=Decimal("2.5"), loading_rate=Decimal("3000"),
            target=Decimal("3000"), cut_percent=Decimal("0"), storage_entries=[entry],
        )
        self.assertEqual(outcome["good_hours"], 1)
        self.assertEqual(outcome["segment_loaded_m3"], Decimal("3000.000"))
        rows = outcome["tank_hours"]["e1"]
        self.assertTrue(all(row["draw_m3"] == "0.000" for row in rows[:2]))
        self.assertEqual(rows[2]["draw_m3"], "3000.000")
        self.assertEqual(outcome["shortfall_m3"], Decimal("0.000"))

    def test_heel_caps_available_oil_and_reports_shortfall(self) -> None:
        hours = window_hours("2026-09-25T00:00:00Z", "2026-09-25T02:00:00Z")
        sea = {hour: Decimal("1") for hour in hours}
        entry = {"entry_id": "e1", "tank_id": "T1", "opening_level_m3": "3000",
                 "capacity_m3": "100000", "heel_m3": "2000", "rate_m3d": "0"}
        outcome = simulate_tanks(
            hours=hours, sea=sea, wave_limit=Decimal("2.5"), loading_rate=Decimal("5000"),
            target=Decimal("5000"), cut_percent=Decimal("0"), storage_entries=[entry],
        )
        self.assertEqual(outcome["segment_loaded_m3"], Decimal("1000.000"))
        self.assertEqual(outcome["shortfall_m3"], Decimal("4000.000"))


class LiftingServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(BASE_TIME)
        self.service = LiftingService(self.connection, self.clock)
        for user_id, role, party in (
            ("plan", "planner", "operator"),
            ("ops-platform", "platform", "platform"),
            ("ops-ship", "vessel", "ship"),
            ("dispatch", "dispatcher", "operator"),
            ("audit", "auditor", "audit"),
        ):
            self.service.create_user(user_id, user_id, role, party)
        self._catalog()

    def tearDown(self) -> None:
        self.connection.close()

    def _catalog(self) -> None:
        self.service.register_quality_limit("plan", {"grade": "BRENT", "max_water_cut_percent": "1"})
        self.service.register_quality_limit("plan", {"grade": "BRENT-SOUR", "max_water_cut_percent": "2"})
        self.service.register_tank("plan", {"tank_id": "T1", "name": "南舱", "compatible_grades": ["BRENT"],
                                            "max_water_cut_percent": "2", "capacity_m3": "120000", "heel_m3": "2000"})
        self.service.register_tank("plan", {"tank_id": "T2", "name": "北舱", "compatible_grades": ["BRENT"],
                                            "max_water_cut_percent": "2", "capacity_m3": "80000", "heel_m3": "1000"})
        self.service.register_tank("plan", {"tank_id": "T3", "name": "缓冲罐",
                                            "compatible_grades": ["BRENT", "BRENT-SOUR"],
                                            "max_water_cut_percent": "5", "capacity_m3": "90000", "heel_m3": "1000"})
        self.service.register_vessel("plan", {"vessel_id": "ship-a", "name": "远谭湖号",
                                              "compatible_grades": ["BRENT", "BRENT-SOUR"],
                                              "min_cargo_m3": "60000", "max_cargo_m3": "110000"})
        self.service.register_vessel("plan", {"vessel_id": "ship-b", "name": "鲲鹏号",
                                              "compatible_grades": ["BRENT"],
                                              "min_cargo_m3": "20000", "max_cargo_m3": "80000"})
        self.service.register_vessel("plan", {"vessel_id": "ship-c", "name": "南洋号",
                                              "compatible_grades": ["BRENT-SOUR"],
                                              "min_cargo_m3": "20000", "max_cargo_m3": "90000"})
        self.service.register_window("plan", {"window_id": "win-25", "berth_id": "berth-1",
                                              "starts_at": "2026-09-25T00:00:00Z",
                                              "ends_at": "2026-09-26T00:00:00Z",
                                              "max_wave_height_m": "2.5", "loading_rate_m3h": "5000"})
        self.service.register_window("plan", {"window_id": "win-26", "berth_id": "berth-1",
                                              "starts_at": "2026-09-26T00:00:00Z",
                                              "ends_at": "2026-09-27T00:00:00Z",
                                              "max_wave_height_m": "2.5", "loading_rate_m3h": "5000"})
        self.service.register_batch("plan", {"batch_id": "b-a", "grade": "BRENT",
                                             "water_cut_percent": "0.8", "daily_rate_m3": "2000"})
        self.service.register_batch("plan", {"batch_id": "b-sour", "grade": "BRENT-SOUR",
                                             "water_cut_percent": "1.5", "daily_rate_m3": "1000"})
        self.service.register_sea_state_version("plan", sea_payload("25"))
        self.service.register_sea_state_version("plan", sea_payload("26"))

    def _entries(self, **overrides) -> list[dict]:
        entries = [
            {"kind": "storage", "entry_id": "e-t1", "tank_id": "T1", "batch_id": "b-a",
             "opening_level_m3": "90000", "rate_m3d": "2000"},
            {"kind": "storage", "entry_id": "e-t2", "tank_id": "T2", "batch_id": "b-a",
             "opening_level_m3": "20000", "rate_m3d": "1000"},
        ]
        entries[0].update(overrides)
        return entries

    def _draft(self, plan_id: str, **overrides) -> dict:
        payload = {
            "plan_id": plan_id, "vessel_id": "ship-a", "window_id": "win-25", "load_grade": "BRENT",
            "load_target_m3": "100000", "sea_state_version_id": "ss-25", "lease_minutes": 60,
            "entries": self._entries(),
        }
        payload.update(overrides)
        return self.service.draft_plan("plan", payload)

    def _seal(self, plan_id: str) -> dict:
        self.service.reserve_plan("dispatch", plan_id)
        self.service.confirm_plan("ops-platform", plan_id, "platform")
        self.service.confirm_plan("ops-ship", plan_id, "vessel")
        return self.service.seal_plan("dispatch", plan_id)

    # ------------------------------------------------------------- 评估规则

    def test_violations_cover_compatibility_cargo_window_and_water_cut(self) -> None:
        plan = self._draft("P-bad", vessel_id="ship-c", load_target_m3="95000",
                           entries=[{"kind": "storage", "entry_id": "e-t3", "tank_id": "T3",
                                     "batch_id": "b-sour", "opening_level_m3": "90000",
                                     "rate_m3d": "1000"}])
        codes = {v["code"] for v in plan["evaluation"]["violations"]}
        self.assertIn("vessel_grade_incompatible", codes)
        self.assertIn("vessel_cargo_out_of_range", codes)
        self.assertIn("batch_grade_mismatch", codes)
        self.assertIn("quality_downgrade_required", codes)

    def test_missing_sea_hours_are_violations(self) -> None:
        plan = self._draft(
            "P-gap",
            window_id="win-26",
            sea_state_version_id="ss-25",
        )
        codes = {v["code"] for v in plan["evaluation"]["violations"]}
        self.assertIn("sea_data_gap", codes)

    def test_plans_are_comparable_by_score(self) -> None:
        self._draft("P-good")
        self._draft("P-tight", entries=[
            {"kind": "storage", "entry_id": "e-t1", "tank_id": "T1", "batch_id": "b-a",
             "opening_level_m3": "115000", "rate_m3d": "48000"},
            {"kind": "storage", "entry_id": "e-t2", "tank_id": "T2", "batch_id": "b-a",
             "opening_level_m3": "70000", "rate_m3d": "12000"},
        ])
        ranking = self.service.compare_plans("plan", "win-25")["plans"]
        self.assertEqual([p["plan_id"] for p in ranking], ["P-good", "P-tight"])
        self.assertGreater(Decimal(ranking[1]["advised_cut_percent"]), 0)

    # ------------------------------------------------------------- 预留封存

    def test_lease_exclusivity_blocks_second_plan_on_window_and_vessel(self) -> None:
        self._draft("P1")
        self._draft("P2")
        self.service.reserve_plan("dispatch", "P1")
        with self.assertRaises(Conflict):
            self.service.reserve_plan("dispatch", "P2")

    def test_expired_lease_releases_resources_and_plan_lapses(self) -> None:
        self._draft("P1")
        self.service.reserve_plan("dispatch", "P1")
        self.clock.advance(minutes=61)
        self._draft("P2", window_id="win-26", sea_state_version_id="ss-26")
        # 新预留触发过期清理：P1 失效，其窗口/船舶被释放（这里 P2 用不同窗口，直接验证 P1 状态）
        self.service.reserve_plan("dispatch", "P2")
        self.assertEqual(self.service.plan("P1")["state"], "lapsed")

    def test_confirmation_requires_matching_party(self) -> None:
        self._draft("P1")
        self.service.reserve_plan("dispatch", "P1")
        with self.assertRaises(Forbidden):
            self.service.confirm_plan("ops-ship", "P1", "platform")
        with self.assertRaises(InvalidState):
            self.service.seal_plan("dispatch", "P1")
        self.service.confirm_plan("ops-platform", "P1", "platform")
        self.service.confirm_plan("ops-ship", "P1", "vessel")
        sealed = self.service.seal_plan("dispatch", "P1")
        self.assertEqual(sealed["state"], "sealed")
        with self.assertRaises(InvalidState):
            self.service.seal_plan("dispatch", "P1")

    def test_concurrent_seal_only_one_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lift.sqlite3"
            from lifting_orchestration.storage import connect
            # 主线程完成目录登记、预留与双方确认，两个线程只竞争封存动作。
            setup_conn = connect(path)
            setup = LiftingService(setup_conn, FrozenClock(BASE_TIME))
            for user_id, role, party in (
                ("plan", "planner", "operator"),
                ("ops-platform", "platform", "platform"),
                ("ops-ship", "vessel", "ship"),
                ("dispatch", "dispatcher", "operator"),
            ):
                setup.create_user(user_id, user_id, role, party)
            setup.register_quality_limit("plan", {"grade": "BRENT", "max_water_cut_percent": "1"})
            setup.register_tank("plan", {"tank_id": "T1", "name": "南舱", "compatible_grades": ["BRENT"],
                                         "max_water_cut_percent": "2", "capacity_m3": "120000",
                                         "heel_m3": "2000"})
            setup.register_vessel("plan", {"vessel_id": "ship-a", "name": "远谭湖号",
                                           "compatible_grades": ["BRENT"],
                                           "min_cargo_m3": "60000", "max_cargo_m3": "110000"})
            setup.register_window("plan", {"window_id": "win-25", "berth_id": "berth-1",
                                           "starts_at": "2026-09-25T00:00:00Z",
                                           "ends_at": "2026-09-26T00:00:00Z",
                                           "max_wave_height_m": "2.5", "loading_rate_m3h": "5000"})
            setup.register_batch("plan", {"batch_id": "b-a", "grade": "BRENT",
                                          "water_cut_percent": "0.8", "daily_rate_m3": "2000"})
            setup.register_sea_state_version("plan", sea_payload("25"))
            setup.draft_plan("plan", {
                "plan_id": "P1", "vessel_id": "ship-a", "window_id": "win-25",
                "load_grade": "BRENT", "load_target_m3": "100000",
                "sea_state_version_id": "ss-25", "lease_minutes": 60,
                "entries": [{"kind": "storage", "entry_id": "e-t1", "tank_id": "T1",
                             "batch_id": "b-a", "opening_level_m3": "100000",
                             "rate_m3d": "2000"}],
            })
            setup.reserve_plan("dispatch", "P1")
            setup.confirm_plan("ops-platform", "P1", "platform")
            setup.confirm_plan("ops-ship", "P1", "vessel")
            setup_conn.close()

            results: list[str] = []

            def worker() -> None:
                connection = connect(path)
                service = LiftingService(connection, FrozenClock(BASE_TIME))
                barrier.wait()
                try:
                    service.seal_plan("dispatch", "P1")
                    results.append("ok")
                except Exception as exc:  # noqa: BLE001
                    results.append(type(exc).__name__)
                connection.close()

            barrier = threading.Barrier(2)
            threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(sorted(results), ["InvalidState", "ok"])

    # ------------------------------------------------------------- 封存修订

    def _seal_for_amendments(self) -> None:
        self.clock.current = datetime(2026, 9, 24, 20, 0, tzinfo=timezone.utc)
        self._draft("P1")
        self._seal("P1")
        self.clock.current = datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)

    def test_partial_loading_releases_only_unfinished_buckets_and_explains(self) -> None:
        self._seal_for_amendments()
        result = self.service.record_partial_loading("dispatch", "P1", "40000")
        explanation = result["capacity_explanation"]
        self.assertEqual(result["revision"], 2)
        self.assertTrue(explanation["released"])
        self.assertTrue(explanation["occupied"])
        self.assertTrue(explanation["net_by_resource"])
        # 生效小时之前的桶保持 active
        past = self.connection.execute(
            "SELECT COUNT(*) AS c FROM plan_buckets WHERE plan_id='P1' AND state='active' "
            "AND hour_start<'2026-09-25T08:00:00Z'"
        ).fetchone()["c"]
        self.assertGreater(past, 0)
        net = {row["resource_id"]: row for row in explanation["net_by_resource"]
               if row["bucket_kind"] == "window"}
        self.assertEqual(net["win-25"]["net_released_m3"], "0.000")
        # 22、23 时海况转差不能装船，20:00 时累计 20 个可作业小时，正好装完 100000。
        self.clock.current = datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc)
        self.service.record_partial_loading("dispatch", "P1", "100000")
        active = self.connection.execute(
            "SELECT COUNT(*) AS c FROM plan_buckets WHERE plan_id='P1' AND state='active'"
        ).fetchone()["c"]
        self.assertEqual(active, 0)
        self.assertEqual(self.service.plan("P1")["state"], "completed")

    def test_partial_loading_cannot_exceed_physical_rate(self) -> None:
        self._seal_for_amendments()
        with self.assertRaises(ValidationFailed):
            self.service.record_partial_loading("dispatch", "P1", "45000")

    def test_amendments_rejected_before_window_and_after_completion(self) -> None:
        self._seal_for_amendments()
        self.clock.current = datetime(2026, 9, 24, 23, 0, tzinfo=timezone.utc)
        with self.assertRaises(InvalidState):
            self.service.record_production_cut("dispatch", "P1", "10")
        self.clock.current = datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc)
        self.service.record_partial_loading("dispatch", "P1", "100000")
        with self.assertRaises(InvalidState):
            self.service.record_production_cut("dispatch", "P1", "10")

    def test_tank_switch_validates_compatibility(self) -> None:
        self._seal_for_amendments()
        with self.assertRaises(NotFound):
            self.service.switch_tank("dispatch", "P1", "missing", "T3")
        self.service.record_partial_loading("dispatch", "P1", "40000")
        # 高含水来油改入缓冲罐：油品随罐转移，新罐期初液位取原罐当前液位，修订仅重排未完成部分。
        current_level = next(
            e["opening_level_m3"] for e in self.service.plan("P1")["entries"]
            if e["entry_id"] == "e-t2"
        )
        result = self.service.switch_tank("dispatch", "P1", "e-t2", "T3", current_level)
        self.assertEqual(result["revision"], 3)
        entries = {e["entry_id"]: e for e in self.service.plan("P1")["entries"]
                   if e["kind"] == "storage" and e["state"] == "active"}
        self.assertNotIn("e-t2", entries)
        self.assertIn("e-t2-sw2", entries)

    def test_tank_switch_rejects_grade_incompatible_target(self) -> None:
        self._seal_for_amendments()
        self.service.record_partial_loading("dispatch", "P1", "40000")
        # T1/T2/T3 都兼容 BRENT；再造一个只兼容其他品位的罐
        self.service.register_tank("plan", {"tank_id": "T9", "name": "专用罐",
                                            "compatible_grades": ["BRENT-SOUR"],
                                            "max_water_cut_percent": "5",
                                            "capacity_m3": "50000", "heel_m3": "0"})
        with self.assertRaises(ValidationFailed):
            self.service.switch_tank("dispatch", "P1", "e-t1", "T9", "1000")

    def test_downgrade_requires_vessel_and_quality_limit(self) -> None:
        self._seal_for_amendments()
        with self.assertRaises(ValidationFailed):
            self.service.downgrade_quality("dispatch", "P1", "NOT-A-GRADE")
        self.service.record_partial_loading("dispatch", "P1", "40000")
        # T1/T2 只兼容 BRENT，未换罐直接降级必须被拒绝。
        with self.assertRaises(InvalidState):
            self.service.downgrade_quality("dispatch", "P1", "BRENT-SOUR", "b-sour")
        # 先把未完成部分换到兼容 BRENT-SOUR 的 T3/T4，再降级。
        self.service.register_tank("plan", {"tank_id": "T4", "name": "降级罐",
                                            "compatible_grades": ["BRENT", "BRENT-SOUR"],
                                            "max_water_cut_percent": "5",
                                            "capacity_m3": "90000", "heel_m3": "0"})
        for entry_id, to_tank in (("e-t1", "T4"), ("e-t2", "T3")):
            level = next(e["opening_level_m3"] for e in self.service.plan("P1")["entries"]
                         if e["entry_id"] == entry_id)
            self.service.switch_tank("dispatch", "P1", entry_id, to_tank, level)
        result = self.service.downgrade_quality("dispatch", "P1", "BRENT-SOUR", "b-sour")
        self.assertEqual(result["evaluation"]["load_grade"], "BRENT-SOUR")

    def test_cancel_voyage_promotes_frozen_priority_skipping_unsuitable(self) -> None:
        self.service.draft_plan("plan", {
            "plan_id": "P3", "vessel_id": "ship-a", "window_id": "win-26", "load_grade": "BRENT",
            "load_target_m3": "100000", "sea_state_version_id": "ss-26", "lease_minutes": 60,
            "entries": self._entries() + [
                {"kind": "waitlist", "entry_id": "wait-c", "vessel_id": "ship-c",
                 "priority": 5, "requested_m3": "60000"},
                {"kind": "waitlist", "entry_id": "wait-b", "vessel_id": "ship-b",
                 "priority": 10, "requested_m3": "50000"},
            ],
        })
        self._seal("P3")
        self.clock.current = datetime(2026, 9, 26, 2, 0, tzinfo=timezone.utc)
        cancelled = self.service.cancel_voyage("dispatch", "P3")
        promoted = cancelled["promoted"]
        self.assertEqual(promoted["entry_id"], "wait-b")
        self.assertEqual(promoted["frozen_rank"], 2)
        self.assertEqual(self.service.plan("P3")["state"], "cancelled")
        promoted_plan = self.service.plan(promoted["plan_id"])
        self.assertEqual(promoted_plan["state"], "reserved")
        self.assertEqual(promoted_plan["vessel_id"], "ship-b")
        # 航次取消释放窗口独占，候补计划才能占用同一窗口
        self.assertTrue(cancelled["capacity_explanation"]["released"])

    def test_audit_chain_detects_tampering(self) -> None:
        self._draft("P1")
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE lifting_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])


class LiftingApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(LiftingService(self.connection, FrozenClock(BASE_TIME)))

    def tearDown(self) -> None:
        self.connection.close()

    def test_health_and_actor_header(self) -> None:
        self.assertEqual(self.app.handle("GET", "/health").status, 200)
        response = self.app.handle("GET", "/plans/compare")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_permission_and_json_error_shape(self) -> None:
        self.app.service.create_user("audit", "审计", "auditor", "audit")
        response = self.app.handle("POST", "/tanks", {"X-Actor-Id": "audit"},
                                   body=json.dumps({"tank_id": "T1", "name": "罐",
                                                    "compatible_grades": ["BRENT"],
                                                    "max_water_cut_percent": "2",
                                                    "capacity_m3": "1000", "heel_m3": "0"}).encode())
        self.assertEqual(response.status, 403)
        self.assertEqual(response.body["error"]["code"], "forbidden")
        bad = self.app.handle("POST", "/users", {"X-Actor-Id": "audit"}, body=b"not-json")
        self.assertEqual(bad.status, 422)
        self.assertEqual(bad.body["error"]["code"], "validation_failed")


if __name__ == "__main__":
    unittest.main()
