"""储运与提油编排用例：目录登记、计划评估、租约预留、双确认封存、修订与候补推进。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    BerthWindow,
    LiftPlanDraft,
    ProcessingBatch,
    QualityLimit,
    SeaStateVersion,
    Tank,
    Vessel,
)
from .scheduling import (
    ZERO,
    canonical_json,
    decimal_text,
    digest,
    evaluate_plan,
    quantize_volume,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {"catalog.write", "plan.draft", "plan.compare", "plan.read"},
    "platform": {"plan.confirm", "plan.read"},
    "vessel": {"plan.confirm", "plan.read"},
    "dispatcher": {"plan.reserve", "plan.seal", "plan.amend", "plan.read", "capacity.read"},
    "auditor": {"plan.read", "capacity.read", "report.read", "audit.read"},
}

PARTY_BY_SIDE = {"platform": "platform", "vessel": "ship"}


class LiftingService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ------------------------------------------------------------------ 基础

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _hour_now(self) -> str:
        return utc_text(self.clock.now().replace(minute=0, second=0, microsecond=0))

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM lifting_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM lifting_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO lifting_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str, party: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if party not in {"platform", "ship", "operator", "audit"}:
            raise ValidationFailed("未知方别")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO lifting_users(user_id,display_name,role,party,created_at) VALUES(?,?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, party, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role, "party": party}

    # --------------------------------------------------------------- 目录数据

    def register_quality_limit(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        limit = QualityLimit.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO quality_limits(grade,max_water_cut_percent,created_by,created_at) VALUES(?,?,?,?)",
                    (limit.grade, decimal_text(limit.max_water_cut_percent), actor_id, self._now()),
                )
                self._audit("quality_limit", limit.grade, "quality.registered", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("该品位质量界限已经登记") from exc
        return {"grade": limit.grade, "max_water_cut_percent": decimal_text(limit.max_water_cut_percent)}

    def register_tank(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        tank = Tank.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO tanks(tank_id,name,compatible_grades,max_water_cut_percent,capacity_m3,"
                    "heel_m3,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        tank.tank_id, tank.name, canonical_json(list(tank.compatible_grades)),
                        decimal_text(tank.max_water_cut_percent), decimal_text(tank.capacity_m3),
                        decimal_text(tank.heel_m3), actor_id, self._now(),
                    ),
                )
                self._audit("tank", tank.tank_id, "tank.registered", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("储罐编号已经存在") from exc
        return self.tank(tank.tank_id)

    def register_vessel(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        vessel = Vessel.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO vessels(vessel_id,name,compatible_grades,min_cargo_m3,max_cargo_m3,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        vessel.vessel_id, vessel.name, canonical_json(list(vessel.compatible_grades)),
                        decimal_text(vessel.min_cargo_m3), decimal_text(vessel.max_cargo_m3),
                        actor_id, self._now(),
                    ),
                )
                self._audit("vessel", vessel.vessel_id, "vessel.registered", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("提油轮编号已经存在") from exc
        return self.vessel(vessel.vessel_id)

    def register_window(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        window = BerthWindow.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO berth_windows(window_id,berth_id,starts_at,ends_at,max_wave_height_m,"
                    "loading_rate_m3h,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        window.window_id, window.berth_id, window.starts_at, window.ends_at,
                        decimal_text(window.max_wave_height_m), decimal_text(window.loading_rate_m3h),
                        actor_id, self._now(),
                    ),
                )
                self._audit("window", window.window_id, "window.registered", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("靠泊窗口编号已经存在") from exc
        return self.window(window.window_id)

    def register_batch(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        batch = ProcessingBatch.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO processing_batches(batch_id,grade,water_cut_percent,daily_rate_m3,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        batch.batch_id, batch.grade, decimal_text(batch.water_cut_percent),
                        decimal_text(batch.daily_rate_m3), actor_id, self._now(),
                    ),
                )
                self._audit("batch", batch.batch_id, "batch.registered", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("加工批次编号已经存在") from exc
        return self.batch(batch.batch_id)

    def register_sea_state_version(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        version = SeaStateVersion.from_dict(raw)
        supersedes = raw.get("supersedes_version_id")
        if supersedes is not None:
            if self.connection.execute(
                "SELECT 1 FROM sea_state_versions WHERE version_id=?", (supersedes,)
            ).fetchone() is None:
                raise ValidationFailed("被取代的海况数据版本不存在")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO sea_state_versions(version_id,issued_at,source,supersedes_version_id,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (version.version_id, version.issued_at, version.source, supersedes, actor_id, self._now()),
                )
                self.connection.executemany(
                    "INSERT INTO sea_state_observations(version_id,hour_start,wave_height_m) VALUES(?,?,?)",
                    [
                        (version.version_id, item.hour_start, decimal_text(item.wave_height_m))
                        for item in version.observations
                    ],
                )
                self._audit("sea_state", version.version_id, "sea_state.versioned", actor_id,
                            {"observations": len(version.observations), "supersedes": supersedes})
        except sqlite3.IntegrityError as exc:
            raise Conflict("海况数据版本编号或观测小时冲突") from exc
        return {"version_id": version.version_id, "observations": len(version.observations)}

    def tank(self, tank_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM tanks WHERE tank_id=?", (tank_id,)).fetchone()
        if row is None:
            raise NotFound("储罐不存在")
        result = dict(row)
        result["compatible_grades"] = json.loads(result["compatible_grades"])
        return result

    def vessel(self, vessel_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM vessels WHERE vessel_id=?", (vessel_id,)).fetchone()
        if row is None:
            raise NotFound("提油轮不存在")
        result = dict(row)
        result["compatible_grades"] = json.loads(result["compatible_grades"])
        return result

    def window(self, window_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM berth_windows WHERE window_id=?", (window_id,)).fetchone()
        if row is None:
            raise NotFound("靠泊窗口不存在")
        return dict(row)

    def batch(self, batch_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM processing_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFound("加工批次不存在")
        return dict(row)

    def _catalog(self) -> dict[str, dict[str, dict[str, Any]]]:
        tanks = {row["tank_id"]: self.tank(row["tank_id"]) for row in self.connection.execute(
            "SELECT tank_id FROM tanks ORDER BY tank_id").fetchall()}
        vessels = {row["vessel_id"]: self.vessel(row["vessel_id"]) for row in self.connection.execute(
            "SELECT vessel_id FROM vessels ORDER BY vessel_id").fetchall()}
        windows = {row["window_id"]: dict(row) for row in self.connection.execute(
            "SELECT * FROM berth_windows ORDER BY window_id").fetchall()}
        batches = {row["batch_id"]: dict(row) for row in self.connection.execute(
            "SELECT * FROM processing_batches ORDER BY batch_id").fetchall()}
        quality = {
            row["grade"]: {"grade": row["grade"], "max_water_cut_percent": row["max_water_cut_percent"]}
            for row in self.connection.execute("SELECT * FROM quality_limits").fetchall()
        }
        sea: dict[str, dict[str, Any]] = {}
        for row in self.connection.execute("SELECT * FROM sea_state_versions ORDER BY version_id").fetchall():
            observations = [
                {"hour_start": item["hour_start"], "wave_height_m": item["wave_height_m"]}
                for item in self.connection.execute(
                    "SELECT hour_start,wave_height_m FROM sea_state_observations WHERE version_id=? ORDER BY hour_start",
                    (row["version_id"],),
                ).fetchall()
            ]
            sea[row["version_id"]] = {"version_id": row["version_id"], "observations": observations}
        return {"tanks": tanks, "vessels": vessels, "windows": windows,
                "batches": batches, "quality": quality, "sea": sea}

    # --------------------------------------------------------------- 计划草稿

    def _draft_content(self, draft: LiftPlanDraft) -> dict[str, Any]:
        tank_ids = [item.tank_id for item in draft.storage_entries]
        if len(set(tank_ids)) != len(tank_ids):
            raise ValidationFailed("同一储罐在计划中不能出现多个条目，换罐请使用修订")
        return {
            "plan_id": draft.plan_id,
            "vessel_id": draft.vessel_id,
            "window_id": draft.window_id,
            "load_grade": draft.load_grade,
            "load_target_m3": decimal_text(draft.load_target_m3),
            "sea_state_version_id": draft.sea_state_version_id,
            "production_cut_percent": decimal_text(draft.production_cut_percent),
            "lease_minutes": draft.lease_minutes,
            "storage_entries": [
                {
                    "entry_id": item.entry_id,
                    "tank_id": item.tank_id,
                    "batch_id": item.batch_id,
                    "opening_level_m3": decimal_text(item.opening_level_m3),
                    "rate_m3d": None if item.rate_m3d is None else decimal_text(item.rate_m3d),
                }
                for item in draft.storage_entries
            ],
            "wait_entries": [
                {
                    "entry_id": item.entry_id,
                    "vessel_id": item.vessel_id,
                    "priority": item.priority,
                    "requested_m3": decimal_text(item.requested_m3),
                }
                for item in draft.wait_entries
            ],
        }

    def _evaluate(self, content: Mapping[str, Any], *, window_override: Mapping[str, Any] | None = None) -> dict[str, Any]:
        catalog = self._catalog()
        windows = dict(catalog["windows"])
        if window_override is not None:
            windows[window_override["window_id"]] = dict(window_override)
        try:
            return evaluate_plan(
                plan=content,
                tanks=catalog["tanks"],
                batches=catalog["batches"],
                vessels=catalog["vessels"],
                windows=windows,
                sea_versions=catalog["sea"],
                quality_limits=catalog["quality"],
                all_tanks=[row for row in catalog["tanks"].values() if row.get("active", 1)],
                loaded_baseline=Decimal(str(content.get("loaded_baseline_m3", 0))),
            )
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc

    def _insert_entries(self, plan_id: str, revision: int, content: Mapping[str, Any]) -> None:
        for item in content["storage_entries"]:
            self.connection.execute(
                "INSERT INTO plan_entries(plan_id,revision,entry_id,kind,tank_id,batch_id,"
                "opening_level_m3,rate_m3d,state) VALUES(?,?,?,?,?,?,?,?, 'active')",
                (plan_id, revision, item["entry_id"], "storage", item["tank_id"], item["batch_id"],
                 item["opening_level_m3"], item["rate_m3d"]),
            )
        self.connection.execute(
            "INSERT INTO plan_entries(plan_id,revision,entry_id,kind,cut_percent,state) "
            "VALUES(?,?,?,?,?, 'active')",
            (plan_id, revision, "production-cut", "production-cut", content["production_cut_percent"]),
        )
        for item in content.get("wait_entries", []):
            self.connection.execute(
                "INSERT INTO plan_entries(plan_id,revision,entry_id,kind,vessel_id_ref,priority,"
                "requested_m3,state) VALUES(?,?,?,?,?,?,?, 'active')",
                (plan_id, revision, item["entry_id"], "waitlist", item["vessel_id"],
                 item["priority"], item["requested_m3"]),
            )

    def draft_plan(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "plan.draft")
        draft = LiftPlanDraft.from_dict(raw)
        content = self._draft_content(draft)
        evaluation = self._evaluate(content)
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO lift_plans(plan_id,vessel_id,window_id,load_grade,load_target_m3,"
                    "sea_state_version_id,production_cut_percent,state,revision,lease_minutes,"
                    "evaluation_json,created_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?, 'draft', 1, ?,?,?,?,?)",
                    (
                        draft.plan_id, draft.vessel_id, draft.window_id, draft.load_grade,
                        decimal_text(draft.load_target_m3), draft.sea_state_version_id,
                        decimal_text(draft.production_cut_percent), draft.lease_minutes,
                        canonical_json(evaluation), actor_id, now, now,
                    ),
                )
                self._insert_entries(draft.plan_id, 1, content)
                self._audit("plan", draft.plan_id, "plan.drafted", actor_id,
                            {"feasible": evaluation["feasible"], "score": evaluation["score"]["points"]})
        except sqlite3.IntegrityError as exc:
            raise Conflict("计划编号冲突或引用的目录资源不存在") from exc
        return self.plan(draft.plan_id)

    def compare_plans(self, actor_id: str, window_id: str | None = None) -> dict[str, Any]:
        self._require(actor_id, "plan.compare")
        rows = self.connection.execute(
            "SELECT * FROM lift_plans WHERE state IN ('draft','reserved') ORDER BY plan_id"
        ).fetchall() if window_id is None else self.connection.execute(
            "SELECT * FROM lift_plans WHERE state IN ('draft','reserved') AND window_id=? ORDER BY plan_id",
            (window_id,),
        ).fetchall()
        plans: list[dict[str, Any]] = []
        for row in rows:
            evaluation = json.loads(row["evaluation_json"])
            plans.append({
                "plan_id": row["plan_id"],
                "state": row["state"],
                "window_id": row["window_id"],
                "vessel_id": row["vessel_id"],
                "load_grade": row["load_grade"],
                "load_target_m3": row["load_target_m3"],
                "sea_state_version_id": row["sea_state_version_id"],
                "feasible": evaluation["feasible"],
                "violations": len(evaluation["violations"]),
                "score": evaluation["score"],
                "advised_cut_percent": evaluation["advised_cut_percent"],
                "tank_switch_required": evaluation["tank_switch_required"],
            })
        plans.sort(key=lambda item: (item["score"]["points"], item["plan_id"]))
        return {"window_id": window_id, "plans": plans, "comparable_at": self._now()}

    # --------------------------------------------------------------- 租约预留

    def _expire_leases(self) -> list[str]:
        now = self._now()
        rows = self.connection.execute(
            "SELECT plan_id FROM lift_plans WHERE state='reserved' AND lease_expires_at IS NOT NULL "
            "AND lease_expires_at<?",
            (now,),
        ).fetchall()
        expired = [row["plan_id"] for row in rows]
        for plan_id in expired:
            old = self.connection.execute(
                "SELECT plan_id,created_by FROM lift_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
            self.connection.execute(
                "UPDATE plan_buckets SET state='released' WHERE plan_id=? AND state='active'",
                (plan_id,),
            )
            self.connection.execute(
                "UPDATE lift_plans SET state='lapsed',updated_at=? WHERE plan_id=? AND state='reserved'",
                (now, plan_id),
            )
            self.connection.execute(
                "INSERT INTO plan_events(plan_id,revision,event_type,effective_from,actor_id,"
                "detail_json,explanation_json,created_at) VALUES(?,?,'lease.lapsed',?,?,?,?,?)",
                (plan_id, 1, self._hour_now(), old["created_by"], canonical_json({}),
                 canonical_json({"messages": [f"租约超过保留期限，计划 {plan_id} 自动失效"]}), now),
            )
        return expired

    def _bucket_conflict(self, kind: str, resource_id: str, hour: str, new_m3: Decimal) -> str | None:
        if kind in ("window", "vessel"):
            row = self.connection.execute(
                "SELECT plan_id FROM plan_buckets WHERE bucket_kind=? AND resource_id=? AND hour_start=? "
                "AND state='active' LIMIT 1",
                (kind, resource_id, hour),
            ).fetchone()
            return None if row is None else row["plan_id"]
        total = self.connection.execute(
            "SELECT COALESCE(SUM(CAST(reserved_m3 AS REAL)),0) AS total FROM plan_buckets "
            "WHERE bucket_kind='tank' AND resource_id=? AND hour_start=? AND state='active'",
            (resource_id, hour),
        ).fetchone()["total"]
        capacity = Decimal(str(self.tank(resource_id)["capacity_m3"]))
        if Decimal(str(total)) + new_m3 > capacity:
            row = self.connection.execute(
                "SELECT plan_id FROM plan_buckets WHERE bucket_kind='tank' AND resource_id=? AND hour_start=? "
                "AND state='active' ORDER BY bucket_id LIMIT 1",
                (resource_id, hour),
            ).fetchone()
            return row["plan_id"]
        return None

    def _insert_buckets(self, plan_id: str, revision: int, evaluation: Mapping[str, Any]) -> None:
        for bucket in evaluation["buckets"]:
            kind = bucket["bucket_kind"]
            resource_id = bucket["resource_id"]
            hour = bucket["hour_start"]
            reserved = Decimal(str(bucket["reserved_m3"]))
            holder = self._bucket_conflict(kind, resource_id, hour, reserved)
            if holder is not None and holder != plan_id:
                raise Conflict(
                    f"{kind} 资源 {resource_id} 在 {hour} 已被计划 {holder} 的有效租约占用"
                )
            self.connection.execute(
                "INSERT INTO plan_buckets(plan_id,revision,bucket_kind,resource_id,hour_start,"
                "reserved_m3,is_exclusive,state) VALUES(?,?,?,?,?,?,?, 'active')",
                (plan_id, revision, kind, resource_id, hour,
                 decimal_text(reserved), int(bucket["exclusive"])),
            )

    def _lease_view(self, evaluation: Mapping[str, Any]) -> dict[str, Any]:
        tank_peaks: dict[str, Decimal] = {}
        hours_by_resource: dict[str, set[str]] = {}
        for bucket in evaluation["buckets"]:
            key = f"{bucket['bucket_kind']}:{bucket['resource_id']}"
            hours_by_resource.setdefault(key, set()).add(bucket["hour_start"])
            if bucket["bucket_kind"] == "tank":
                tank_peaks[key] = max(tank_peaks.get(key, ZERO), Decimal(str(bucket["reserved_m3"])))
        occupied = []
        for key in sorted(hours_by_resource):
            kind, resource_id = key.split(":", 1)
            item = {"bucket_kind": kind, "resource_id": resource_id,
                    "hours": len(hours_by_resource[key])}
            if key in tank_peaks:
                item["peak_reserved_m3"] = decimal_text(tank_peaks[key])
            occupied.append(item)
        return {"occupied": occupied}

    def reserve_plan(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._require(actor_id, "plan.reserve")
        plan = self._plan_row(plan_id)
        if plan["state"] != "draft":
            raise InvalidState("只有草稿计划可以预留资源")
        content = self._content_from_rows(plan)
        evaluation = self._evaluate(content)
        if not evaluation["feasible"]:
            raise InvalidState(json.dumps(
                {"message": "计划存在硬性冲突，不能预留资源", "violations": evaluation["violations"]},
                ensure_ascii=False,
            ))
        now = self._now()
        expires = utc_text(parse_utc(now) + timedelta(minutes=plan["lease_minutes"]))
        with transaction(self.connection, immediate=True):
            self._expire_leases()
            try:
                self._insert_buckets(plan_id, 1, evaluation)
            except sqlite3.IntegrityError as exc:
                raise Conflict("靠泊窗口或提油轮已被其他有效租约独占") from exc
            self.connection.execute(
                "UPDATE lift_plans SET state='reserved',lease_expires_at=?,evaluation_json=?,"
                "updated_at=? WHERE plan_id=? AND state='draft'",
                (expires, canonical_json(evaluation), now, plan_id),
            )
            view = self._lease_view(evaluation)
            explanation = {
                "released": [],
                **view,
                "net": view["occupied"],
                "messages": [
                    f"租约预留靠泊窗口 {plan['window_id']} 与提油轮 {plan['vessel_id']}，"
                    f"共 {evaluation['window_hours']} 个窗口小时（可作业 {evaluation['operable_hours']} 小时）",
                    f"建议降产 {evaluation['advised_cut_percent']}% 以防罐容溢流"
                    if evaluation["advised_cut_percent"] not in (None, "0.000") else "无需降产即可避免罐容溢流",
                ],
                "capacity_view_after": self._capacity_view_sql(plan["window_id"]),
            }
            self._record_event(plan_id, 1, "plan.reserved", self._hour_now(), actor_id,
                               {"lease_expires_at": expires}, explanation)
            self._audit("plan", plan_id, "plan.reserved", actor_id,
                        {"lease_expires_at": expires, "occupied_resources": len(view["occupied"])})
        return self.plan(plan_id)

    # --------------------------------------------------------------- 确认封存

    def confirm_plan(self, actor_id: str, plan_id: str, side: str) -> dict[str, Any]:
        self._require(actor_id, "plan.confirm")
        if side not in PARTY_BY_SIDE:
            raise ValidationFailed("side 必须是 platform 或 vessel")
        user = self._user(actor_id)
        if user["party"] != PARTY_BY_SIDE[side]:
            raise Forbidden(f"只有{'平台方' if side == 'platform' else '船方'}可以代表该方确认")
        plan = self._plan_row(plan_id)
        if plan["state"] not in ("reserved", "sealed"):
            raise InvalidState("只有已预留资源的计划可以确认")
        if self._lease_expired(plan):
            raise InvalidState("租约已过期，请重新预留资源")
        column = "platform_confirmed_by" if side == "platform" else "vessel_confirmed_by"
        if plan[column] is not None:
            return self.plan(plan_id)
        now = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                f"UPDATE lift_plans SET {column}=?,{column.replace('_by', '_at')}=?,updated_at=? "
                "WHERE plan_id=?",
                (actor_id, now, now, plan_id),
            )
            self._record_event(plan_id, plan["revision"], f"plan.confirmed.{side}",
                               self._hour_now(), actor_id, {"side": side},
                               {"messages": [f"{'平台方' if side == 'platform' else '船方'}已确认计划，未释放或占用容量"],
                                "released": [], "occupied": []})
            self._audit("plan", plan_id, f"plan.confirmed.{side}", actor_id, {})
        return self.plan(plan_id)

    def _lease_expired(self, plan: sqlite3.Row) -> bool:
        return plan["lease_expires_at"] is not None and plan["lease_expires_at"] < self._now()

    def seal_plan(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._require(actor_id, "plan.seal")
        plan = self._plan_row(plan_id)
        if plan["state"] != "reserved":
            raise InvalidState("只有已预留资源的计划可以封存")
        if plan["platform_confirmed_by"] is None or plan["vessel_confirmed_by"] is None:
            raise InvalidState("平台方与船方均确认后才能封存")
        now = self._now()
        with transaction(self.connection, immediate=True):
            self._expire_leases()
            cursor = self.connection.execute(
                "UPDATE lift_plans SET state='sealed',sealed_at=?,updated_at=? "
                "WHERE plan_id=? AND state='reserved' AND lease_expires_at>=?",
                (now, now, plan_id, now),
            )
            if cursor.rowcount != 1:
                raise InvalidState("计划已封存、租约已失效或状态已变化")
            explanation = {
                "released": [],
                "occupied": self._lease_view(json.loads(plan["evaluation_json"]))["occupied"],
                "messages": [
                    f"计划封存：冻结靠泊窗口 {plan['window_id']}、提油轮 {plan['vessel_id']}、"
                    f"海况版本 {plan['sea_state_version_id']} 与全部罐容小时桶",
                    "并发封存仅一个成功：其他封存请求将看到 sealed 状态并被拒绝",
                ],
                "capacity_view_after": self._capacity_view_sql(plan["window_id"]),
            }
            self._record_event(plan_id, 1, "plan.sealed", self._hour_now(), actor_id,
                               {"platform_confirmed_by": plan["platform_confirmed_by"],
                                "vessel_confirmed_by": plan["vessel_confirmed_by"]}, explanation)
            self._audit("plan", plan_id, "plan.sealed", actor_id, {})
        return self.plan(plan_id)

    # --------------------------------------------------------------- 封存修订

    def _plan_row(self, plan_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM lift_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFound("提油计划不存在")
        return row

    def _current_entries(self, plan_id: str, revision: int) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM plan_entries WHERE plan_id=? AND revision=? ORDER BY entry_id",
            (plan_id, revision),
        ).fetchall()

    def _content_from_rows(self, plan: sqlite3.Row) -> dict[str, Any]:
        entries = self._current_entries(plan["plan_id"], plan["revision"])
        return {
            "plan_id": plan["plan_id"],
            "vessel_id": plan["vessel_id"],
            "window_id": plan["window_id"],
            "load_grade": plan["load_grade"],
            "load_target_m3": plan["load_target_m3"],
            "sea_state_version_id": plan["sea_state_version_id"],
            "production_cut_percent": plan["production_cut_percent"],
            "storage_entries": [
                {
                    "entry_id": row["entry_id"],
                    "tank_id": row["tank_id"],
                    "batch_id": row["batch_id"],
                    "opening_level_m3": row["opening_level_m3"],
                    "rate_m3d": row["rate_m3d"],
                }
                for row in entries if row["kind"] == "storage" and row["state"] == "active"
            ],
            "wait_entries": [
                {
                    "entry_id": row["entry_id"],
                    "vessel_id": row["vessel_id_ref"],
                    "priority": row["priority"],
                    "requested_m3": row["requested_m3"],
                }
                for row in entries if row["kind"] == "waitlist" and row["state"] == "active"
            ],
        }

    def _state_at_hour(self, plan: sqlite3.Row, effective_hour: str) -> tuple[dict[str, str], Decimal]:
        """从上一版评估的小时轨迹取生效小时前的最新液位与累计装船量。

        若生效小时不早于模拟起点（如窗口起点的修订），回退到条目期初液位。
        """
        evaluation = json.loads(plan["evaluation_json"])
        levels: dict[str, str] = {}
        simulated_loaded = ZERO
        for entry_id, rows in evaluation.get("tank_hours", {}).items():
            level = None
            for row in rows:
                if row["hour_start"] < effective_hour:
                    level = row["level_m3"]
                    simulated_loaded = max(simulated_loaded, Decimal(str(row["cumulative_loaded_m3"])))
                else:
                    break
            if level is None:
                entry = self.connection.execute(
                    "SELECT opening_level_m3 FROM plan_entries WHERE plan_id=? AND revision=? AND entry_id=?",
                    (plan["plan_id"], plan["revision"], entry_id),
                ).fetchone()
                level = entry["opening_level_m3"] if entry is not None else "0"
            levels[entry_id] = level
        return levels, simulated_loaded

    def _capacity_view_sql(self, window_id: str) -> list[dict[str, Any]]:
        window = self.connection.execute(
            "SELECT * FROM berth_windows WHERE window_id=?", (window_id,)
        ).fetchone()
        if window is None:
            return []
        hour_now = self._hour_now()
        rows = self.connection.execute(
            "SELECT bucket_kind,resource_id,hour_start,state,COALESCE(SUM(CAST(reserved_m3 AS REAL)),0) reserved_m3,"
            "GROUP_CONCAT(plan_id) plan_ids FROM plan_buckets WHERE state='active' AND hour_start>=? "
            "AND hour_start>=? AND hour_start<? "
            "GROUP BY bucket_kind,resource_id,hour_start,state ORDER BY hour_start,bucket_kind,resource_id",
            (hour_now, window["starts_at"], window["ends_at"]),
        ).fetchall()
        view: list[dict[str, Any]] = []
        for row in rows:
            item = {
                "bucket_kind": row["bucket_kind"],
                "resource_id": row["resource_id"],
                "hour_start": row["hour_start"],
                "reserved_m3": decimal_text(Decimal(str(row["reserved_m3"]))),
                "held_by_plans": sorted(set(row["plan_ids"].split(","))),
            }
            if row["bucket_kind"] == "tank":
                tank = self.tank(row["resource_id"])
                reserved = Decimal(str(row["reserved_m3"]))
                item["capacity_m3"] = tank["capacity_m3"]
                item["free_m3"] = decimal_text(max(ZERO, Decimal(str(tank["capacity_m3"])) - reserved))
            view.append(item)
        return view

    def _record_event(
        self,
        plan_id: str,
        revision: int,
        event_type: str,
        effective_from: str,
        actor_id: str,
        detail: Mapping[str, Any],
        explanation: Mapping[str, Any],
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO plan_events(plan_id,revision,event_type,effective_from,actor_id,detail_json,"
            "explanation_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (plan_id, revision, event_type, effective_from, actor_id,
             canonical_json(detail), canonical_json(explanation), self._now()),
        )
        return int(cursor.lastrowid)

    def _amend(
        self,
        actor_id: str,
        plan_id: str,
        event_type: str,
        content: Mapping[str, Any],
        detail: Mapping[str, Any],
        messages: list[str],
        *,
        complete: bool = False,
        before_commit=None,
    ) -> dict[str, Any]:
        """通用修订：重算生效小时之后的容量桶，只替换未完成部分。"""
        self._require(actor_id, "plan.amend")
        plan = self._plan_row(plan_id)
        if plan["state"] != "sealed":
            raise InvalidState("只有已封存计划允许修订，未封存计划可等待租约过期后重拟")
        window = self.window(plan["window_id"])
        effective_hour = self._hour_now()
        if not (window["starts_at"] <= effective_hour < window["ends_at"]):
            raise InvalidState("当前时间不在靠泊窗口内，无法修订未完成部分")
        remaining_window = dict(window)
        remaining_window["starts_at"] = effective_hour
        evaluation = self._evaluate(content, window_override=remaining_window)
        if not complete and not evaluation["feasible"]:
            raise InvalidState(json.dumps(
                {"message": "修订后计划存在硬性冲突", "violations": evaluation["violations"],
                 "switch_suggestions": evaluation["switch_suggestions"]},
                ensure_ascii=False,
            ))
        new_revision = plan["revision"] + 1
        with transaction(self.connection, immediate=True):
            result = self._amend_locked(
                actor_id, plan, event_type, content, detail, messages, effective_hour,
                evaluation, new_revision, complete=complete,
            )
            if before_commit is not None:
                before_commit(new_revision)
        return result

    def _amend_locked(
        self,
        actor_id: str,
        plan: sqlite3.Row,
        event_type: str,
        content: Mapping[str, Any],
        detail: Mapping[str, Any],
        messages: list[str],
        effective_hour: str,
        evaluation: Mapping[str, Any],
        new_revision: int,
        *,
        complete: bool,
    ) -> dict[str, Any]:
        plan_id = plan["plan_id"]
        if complete:
            # 完成或取消时释放该计划全部修订版的容量桶（含已成为历史的窗口小时）。
            release_select = ""
            release_update = ""
            select_params: tuple[object, ...] = (plan_id,)
            update_params: tuple[object, ...] = (plan_id,)
        else:
            release_select = "AND hour_start>=?"
            release_update = "AND hour_start>=?"
            select_params = (plan_id, plan["revision"], effective_hour)
            update_params = (plan_id, plan["revision"], effective_hour)
        old_rows = self.connection.execute(
            "SELECT * FROM plan_buckets WHERE plan_id=? AND state='active' "
            + ("" if complete else "AND revision=? ")
            + release_select,
            select_params,
        ).fetchall()
        released = [
            {"bucket_kind": row["bucket_kind"], "resource_id": row["resource_id"],
             "hour_start": row["hour_start"], "released_m3": row["reserved_m3"]}
            for row in old_rows
        ]
        self.connection.execute(
            "UPDATE plan_buckets SET state='released' WHERE plan_id=? AND state='active' "
            + ("" if complete else "AND revision=? ")
            + release_update,
            update_params,
        )
        self._snapshot_revision(plan, new_revision, content)
        new_buckets: list[dict[str, Any]] = [] if complete else evaluation["buckets"]
        try:
            for bucket in new_buckets:
                holder = self._bucket_conflict(
                    bucket["bucket_kind"], bucket["resource_id"], bucket["hour_start"],
                    Decimal(str(bucket["reserved_m3"])),
                )
                if holder is not None and holder != plan_id:
                    raise Conflict(
                        f"{bucket['bucket_kind']} 资源 {bucket['resource_id']} 在 "
                        f"{bucket['hour_start']} 已被计划 {holder} 占用，无法换入"
                    )
                self.connection.execute(
                    "INSERT INTO plan_buckets(plan_id,revision,bucket_kind,resource_id,hour_start,"
                    "reserved_m3,is_exclusive,state) VALUES(?,?,?,?,?,?,?, 'active')",
                    (plan_id, new_revision, bucket["bucket_kind"], bucket["resource_id"],
                     bucket["hour_start"], bucket["reserved_m3"], int(bucket["exclusive"])),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("修订后的资源占用与其他有效租约冲突") from exc
        # 计划总装船目标保持不变；loaded_m3 记录实际累计，修订只重排未完成部分的容量桶。
        self.connection.execute(
            "UPDATE lift_plans SET revision=?,vessel_id=?,load_grade=?,load_target_m3=?,"
            "production_cut_percent=?,evaluation_json=?,updated_at=? WHERE plan_id=?",
            (
                new_revision, content["vessel_id"], content["load_grade"],
                plan["load_target_m3"], content["production_cut_percent"],
                canonical_json(evaluation), self._now(), plan_id,
            ),
        )
        net = self._net_change(released, [
            {"bucket_kind": b["bucket_kind"], "resource_id": b["resource_id"],
             "hour_start": b["hour_start"], "occupied_m3": b["reserved_m3"]}
            for b in new_buckets
        ])
        explanation = {
            "effective_from": effective_hour,
            "released": released,
            "occupied": [
                {"bucket_kind": b["bucket_kind"], "resource_id": b["resource_id"],
                 "hour_start": b["hour_start"], "occupied_m3": b["reserved_m3"]}
                for b in new_buckets
            ],
            "net_by_resource": net,
            "messages": messages,
            "capacity_view_after": self._capacity_view_sql(plan["window_id"]),
        }
        event_id = self._record_event(plan_id, new_revision, event_type, effective_hour,
                                      actor_id, detail, explanation)
        self._audit("plan", plan_id, event_type, actor_id,
                    {"event_id": event_id, "revision": new_revision,
                     "released_buckets": len(released), "occupied_buckets": len(new_buckets)})
        return {"plan_id": plan_id, "revision": new_revision, "event_id": event_id,
                "capacity_explanation": explanation, "evaluation": evaluation}

    @staticmethod
    def _net_change(released: Iterable[Mapping[str, Any]], occupied: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        totals: dict[tuple[str, str], dict[str, Decimal]] = {}
        for row in released:
            key = (row["bucket_kind"], row["resource_id"])
            totals.setdefault(key, {"released": ZERO, "occupied": ZERO})
            totals[key]["released"] += Decimal(str(row["released_m3"]))
        for row in occupied:
            key = (row["bucket_kind"], row["resource_id"])
            totals.setdefault(key, {"released": ZERO, "occupied": ZERO})
            totals[key]["occupied"] += Decimal(str(row["occupied_m3"]))
        return [
            {"bucket_kind": kind, "resource_id": resource_id,
             "released_m3": decimal_text(quantize_volume(values["released"])),
             "occupied_m3": decimal_text(quantize_volume(values["occupied"])),
             "net_released_m3": decimal_text(quantize_volume(values["released"] - values["occupied"]))}
            for (kind, resource_id), values in sorted(totals.items())
        ]

    def _snapshot_revision(self, plan: sqlite3.Row, new_revision: int, content: Mapping[str, Any]) -> None:
        """把上一版条目复制到新版本，再应用 content 中的活动条目与候补。"""
        prior = self._current_entries(plan["plan_id"], plan["revision"])
        active_storage = {item["entry_id"]: item for item in content["storage_entries"]}
        active_wait = {item["entry_id"] for item in content.get("wait_entries", [])}
        for row in prior:
            state = row["state"]
            if row["kind"] == "storage":
                state = "active" if row["entry_id"] in active_storage else "released"
            elif row["kind"] == "waitlist":
                state = "active" if row["entry_id"] in active_wait else (
                    "promoted" if row["state"] == "promoted" else "released"
                )
            self.connection.execute(
                "INSERT INTO plan_entries(plan_id,revision,entry_id,kind,tank_id,batch_id,vessel_id_ref,"
                "opening_level_m3,rate_m3d,priority,requested_m3,cut_percent,state) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (plan["plan_id"], new_revision, row["entry_id"], row["kind"], row["tank_id"],
                 row["batch_id"], row["vessel_id_ref"], row["opening_level_m3"], row["rate_m3d"],
                 row["priority"], row["requested_m3"],
                 content["production_cut_percent"] if row["kind"] == "production-cut" else row["cut_percent"],
                 state),
            )
        existing = {row["entry_id"] for row in prior}
        for item in content["storage_entries"]:
            if item["entry_id"] in existing:
                self.connection.execute(
                    "UPDATE plan_entries SET tank_id=?,batch_id=?,opening_level_m3=?,rate_m3d=?,state='active' "
                    "WHERE plan_id=? AND revision=? AND entry_id=?",
                    (item["tank_id"], item["batch_id"], item["opening_level_m3"], item["rate_m3d"],
                     plan["plan_id"], new_revision, item["entry_id"]),
                )
            else:
                self.connection.execute(
                    "INSERT INTO plan_entries(plan_id,revision,entry_id,kind,tank_id,batch_id,"
                    "opening_level_m3,rate_m3d,state) VALUES(?,?,?,?,?,?,?,?, 'active')",
                    (plan["plan_id"], new_revision, item["entry_id"], "storage", item["tank_id"],
                     item["batch_id"], item["opening_level_m3"], item["rate_m3d"]),
                )
        for item in content.get("wait_entries", []):
            if item["entry_id"] not in existing:
                self.connection.execute(
                    "INSERT INTO plan_entries(plan_id,revision,entry_id,kind,vessel_id_ref,priority,"
                    "requested_m3,state) VALUES(?,?,?,?,?,?,?, 'active')",
                    (plan["plan_id"], new_revision, item["entry_id"], "waitlist", item["vessel_id"],
                     item["priority"], item["requested_m3"]),
                )

    def _future_content(
        self,
        plan: sqlite3.Row,
        effective_hour: str,
        *,
        target_remaining: Decimal,
        grade: str | None = None,
        cut_percent: Decimal | None = None,
        drop_entries: set[str] | None = None,
        add_entries: list[dict[str, Any]] | None = None,
        vessel_id: str | None = None,
        batch_override: str | None = None,
    ) -> dict[str, Any]:
        levels, simulated_loaded = self._state_at_hour(plan, effective_hour)
        loaded_before = Decimal(plan["loaded_m3"])
        new_loaded = Decimal(plan["load_target_m3"]) - target_remaining
        # 模拟轨迹中的装船抽取已经体现在液位上，只需再扣除实际登记超出模拟的差额，
        # 避免对同一批装船量重复扣减罐容。
        extra_draw = quantize_volume(max(ZERO, new_loaded - max(loaded_before, simulated_loaded)))
        drop_entries = drop_entries or set()
        entries: list[dict[str, Any]] = []
        order = list(levels.items())
        for entry_id, level_text in order:
            if entry_id in drop_entries:
                continue
            row = self.connection.execute(
                "SELECT * FROM plan_entries WHERE plan_id=? AND revision=? AND entry_id=?",
                (plan["plan_id"], plan["revision"], entry_id),
            ).fetchone()
            level = Decimal(level_text)
            if extra_draw > ZERO:
                tank = self.tank(row["tank_id"])
                available = max(ZERO, level - Decimal(str(tank["heel_m3"])))
                take = min(extra_draw, available)
                level = quantize_volume(level - take)
                extra_draw = quantize_volume(extra_draw - take)
            entries.append({
                "entry_id": entry_id,
                "tank_id": row["tank_id"],
                "batch_id": batch_override or row["batch_id"],
                "opening_level_m3": decimal_text(level),
                "rate_m3d": row["rate_m3d"],
            })
        for added in add_entries or []:
            entries.append(added)
        current = self._content_from_rows(plan)
        return {
            "plan_id": plan["plan_id"],
            "vessel_id": vessel_id or plan["vessel_id"],
            "window_id": plan["window_id"],
            "load_grade": grade or plan["load_grade"],
            "load_target_m3": decimal_text(target_remaining),
            "loaded_baseline_m3": decimal_text(new_loaded),
            "sea_state_version_id": plan["sea_state_version_id"],
            "production_cut_percent": decimal_text(cut_percent if cut_percent is not None
                                                   else Decimal(plan["production_cut_percent"])),
            "storage_entries": entries,
            "wait_entries": current["wait_entries"],
        }

    def _max_loaded_by_hour(self, plan: sqlite3.Row, effective_hour: str) -> Decimal:
        evaluation = json.loads(plan["evaluation_json"])
        # 修订后的评估窗口被截断到生效小时，累计装船量从已登记基线起算。
        maximum = Decimal(str(evaluation.get("loaded_baseline_m3", 0)))
        for rows in evaluation.get("tank_hours", {}).values():
            for row in rows:
                if row["hour_start"] < effective_hour:
                    maximum = max(maximum, Decimal(str(row["cumulative_loaded_m3"])))
        return maximum

    def record_partial_loading(self, actor_id: str, plan_id: str, loaded_m3: object) -> dict[str, Any]:
        plan = self._plan_row(plan_id)
        loaded = quantize_volume(Decimal(str(loaded_m3)))
        if loaded < Decimal(plan["loaded_m3"]):
            raise ValidationFailed("累计装船量不能小于已记录值")
        if loaded > Decimal(plan["load_target_m3"]):
            raise ValidationFailed("累计装船量不能超过计划装船量")
        effective_hour = self._hour_now()
        max_physical = self._max_loaded_by_hour(plan, effective_hour)
        if loaded > max_physical + Decimal("0.001"):
            raise ValidationFailed(
                f"截至 {effective_hour}，按海况与装船速率最多已装 "
                f"{decimal_text(max_physical)} 立方米，不能登记 {decimal_text(loaded)} 立方米"
            )
        remaining = quantize_volume(Decimal(plan["load_target_m3"]) - loaded)
        complete = remaining == ZERO
        content = self._future_content(plan, effective_hour, target_remaining=remaining)
        now = self._now

        def _persist_loaded(new_revision: int) -> None:
            self.connection.execute(
                "UPDATE lift_plans SET loaded_m3=?,updated_at=? WHERE plan_id=?",
                (decimal_text(loaded), now(), plan_id),
            )
            if complete:
                self.connection.execute(
                    "UPDATE lift_plans SET state='completed' WHERE plan_id=?",
                    (plan_id,),
                )

        messages = [
            f"部分装船登记：累计已装 {decimal_text(loaded)} 立方米，剩余 {decimal_text(remaining)} 立方米",
            "已装船部分按各罐抽取顺序释放对应罐容小时桶，仅未完成部分重新占位",
        ]
        if complete:
            messages.append("装船已全部完成，释放窗口、提油轮与全部剩余罐容")
        return self._amend(
            actor_id, plan_id, "loading.partial", content,
            {"loaded_m3": decimal_text(loaded), "remaining_m3": decimal_text(remaining)},
            messages, complete=complete, before_commit=_persist_loaded,
        )

    def record_production_cut(self, actor_id: str, plan_id: str, cut_percent: object) -> dict[str, Any]:
        plan = self._plan_row(plan_id)
        cut = Decimal(str(cut_percent))
        if not ZERO <= cut <= Decimal(100):
            raise ValidationFailed("cut_percent 必须在 0 到 100 之间")
        remaining = Decimal(plan["load_target_m3"]) - Decimal(plan["loaded_m3"])
        effective_hour = self._hour_now()
        content = self._future_content(plan, effective_hour, target_remaining=remaining, cut_percent=cut)
        return self._amend(
            actor_id, plan_id, "production.cut", content,
            {"previous_cut_percent": plan["production_cut_percent"], "cut_percent": decimal_text(cut)},
            [f"降产比例由 {plan['production_cut_percent']}% 调整为 {decimal_text(cut)}%："
             f"加工来油按新比例进入各罐，罐容小时桶同步下调，窗口与提油轮占用不变"],
        )

    def switch_tank(self, actor_id: str, plan_id: str, entry_id: str, to_tank_id: str,
                    opening_level_m3: object = 0) -> dict[str, Any]:
        plan = self._plan_row(plan_id)
        rows = self.connection.execute(
            "SELECT * FROM plan_entries WHERE plan_id=? AND revision=? AND entry_id=? AND kind='storage'",
            (plan_id, plan["revision"], entry_id),
        ).fetchall()
        if not rows:
            raise NotFound("当前修订中没有该储罐条目")
        source = rows[0]
        target_tank = self.tank(to_tank_id)
        if not target_tank.get("active", 1):
            raise ValidationFailed("目标储罐已停用")
        if plan["load_grade"] not in target_tank["compatible_grades"]:
            raise ValidationFailed(f"目标储罐 {to_tank_id} 不兼容品位 {plan['load_grade']}")
        if source["batch_id"]:
            batch = self.batch(source["batch_id"])
            if Decimal(batch["water_cut_percent"]) > Decimal(target_tank["max_water_cut_percent"]):
                raise ValidationFailed(
                    f"批次 {source['batch_id']} 含水率超过目标储罐 {to_tank_id} 上限")
        current_tanks = {
            row["tank_id"] for row in self._current_entries(plan_id, plan["revision"])
            if row["kind"] == "storage" and row["state"] == "active"
        }
        if to_tank_id in current_tanks:
            raise ValidationFailed("目标储罐已在当前计划中使用")
        opening = quantize_volume(Decimal(str(opening_level_m3)))
        if opening > Decimal(target_tank["capacity_m3"]):
            raise ValidationFailed("目标罐初始液位超过罐容")
        effective_hour = self._hour_now()
        remaining = Decimal(plan["load_target_m3"]) - Decimal(plan["loaded_m3"])
        new_entry = {
            "entry_id": f"{entry_id}-sw{plan['revision']}",
            "tank_id": to_tank_id,
            "batch_id": source["batch_id"],
            "opening_level_m3": decimal_text(opening),
            "rate_m3d": source["rate_m3d"],
        }
        content = self._future_content(
            plan, effective_hour, target_remaining=remaining,
            drop_entries={entry_id}, add_entries=[new_entry],
        )
        return self._amend(
            actor_id, plan_id, "tank.switched", content,
            {"from_entry_id": entry_id, "from_tank_id": source["tank_id"],
             "to_tank_id": to_tank_id, "new_entry_id": new_entry["entry_id"]},
            [f"临时换罐自 {effective_hour} 起生效：释放原罐 {source['tank_id']} 未完成部分的罐容小时桶，"
             f"改由 {to_tank_id} 承接来油与装船抽取；已在原罐完成的装船部分不受影响"],
        )

    def downgrade_quality(self, actor_id: str, plan_id: str, new_grade: str,
                          new_batch_id: str | None = None) -> dict[str, Any]:
        plan = self._plan_row(plan_id)
        grade = str(new_grade).strip().upper()
        catalog = self._catalog()
        if grade not in catalog["quality"]:
            raise ValidationFailed(f"缺少 {grade} 的质量界限，不能降级")
        if grade not in self.vessel(plan["vessel_id"])["compatible_grades"]:
            raise ValidationFailed(f"提油轮 {plan['vessel_id']} 不适装降级后的品位 {grade}")
        batch_override = None
        if new_batch_id is not None:
            batch = self.batch(new_batch_id)
            if batch["grade"] != grade:
                raise ValidationFailed(f"替换批次 {new_batch_id} 的品位不是 {grade}")
            batch_override = new_batch_id
        effective_hour = self._hour_now()
        remaining = Decimal(plan["load_target_m3"]) - Decimal(plan["loaded_m3"])
        content = self._future_content(
            plan, effective_hour, target_remaining=remaining, grade=grade,
            batch_override=batch_override,
        )
        if new_batch_id is None:
            for item in content["storage_entries"]:
                item["batch_id"] = None
        return self._amend(
            actor_id, plan_id, "quality.downgraded", content,
            {"previous_grade": plan["load_grade"], "new_grade": grade,
             "new_batch_id": new_batch_id},
            [f"质量降级 {plan['load_grade']}→{grade}：仅对未完成装船部分重新校验储罐与质量界限，"
             "已装船部分保持原品位；不满足新界限的储罐需先临时换罐"],
        )

    def cancel_voyage(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        plan = self._plan_row(plan_id)
        effective_hour = self._hour_now()
        loaded = Decimal(plan["loaded_m3"])
        remaining = quantize_volume(Decimal(plan["load_target_m3"]) - loaded)
        dummy = self._future_content(plan, effective_hour, target_remaining=max(remaining, Decimal("0.001")))

        def _cancel_and_promote(new_revision: int) -> None:
            self.connection.execute(
                "UPDATE lift_plans SET state='cancelled',updated_at=? WHERE plan_id=?",
                (self._now(), plan_id),
            )
            promoted = self._promote_waitlist(plan, effective_hour, actor_id)
            self._audit("plan", plan_id, "voyage.cancelled", actor_id,
                        {"promoted_plan_id": promoted.get("plan_id") if promoted else None})
            result_ref["promoted"] = promoted

        result_ref: dict[str, Any] = {}
        result = self._amend(
            actor_id, plan_id, "voyage.cancelled", dummy,
            {"loaded_m3": decimal_text(loaded), "cancelled_m3": decimal_text(remaining)},
            [f"航次取消：已装船 {decimal_text(loaded)} 立方米维持结算，取消剩余 "
             f"{decimal_text(remaining)} 立方米；窗口、提油轮与未完成罐容全部释放"],
            complete=True, before_commit=_cancel_and_promote,
        )
        result["promoted"] = result_ref.get("promoted")
        return result

    def _promote_waitlist(self, plan: sqlite3.Row, effective_hour: str, actor_id: str) -> dict[str, Any] | None:
        """航次取消后按冻结优先级推进第一个可推进候补，生成新租约计划。"""
        evaluation = json.loads(plan["evaluation_json"])
        candidates = [row for row in evaluation.get("waitlist", []) if row.get("promotable")]
        if not candidates:
            return None
        winner = candidates[0]
        wait_row = next((row for row in self._current_entries(plan["plan_id"], plan["revision"])
                         if row["entry_id"] == winner["entry_id"]), None)
        if wait_row is None or wait_row["state"] != "active":
            return None
        new_plan_id = f"{plan['plan_id']}-w{winner['entry_id']}"
        if self.connection.execute("SELECT 1 FROM lift_plans WHERE plan_id=?", (new_plan_id,)).fetchone():
            return None
        remaining_window = dict(self.window(plan["window_id"]))
        remaining_window["starts_at"] = effective_hour
        levels, _ = self._state_at_hour(plan, effective_hour)
        storage_entries = []
        for entry_id, level in levels.items():
            source = self.connection.execute(
                "SELECT * FROM plan_entries WHERE plan_id=? AND revision=? AND entry_id=?",
                (plan["plan_id"], plan["revision"], entry_id),
            ).fetchone()
            if source is None or source["kind"] != "storage":
                continue
            storage_entries.append({
                "entry_id": entry_id,
                "tank_id": source["tank_id"],
                "batch_id": source["batch_id"],
                "opening_level_m3": level,
                "rate_m3d": source["rate_m3d"],
            })
        tail = [
            {"entry_id": row["entry_id"], "vessel_id": row["vessel_id_ref"],
             "priority": row["priority"], "requested_m3": row["requested_m3"]}
            for row in self._current_entries(plan["plan_id"], plan["revision"])
            if row["kind"] == "waitlist" and row["state"] == "active" and row["entry_id"] != winner["entry_id"]
        ]
        content = {
            "plan_id": new_plan_id,
            "vessel_id": winner["vessel_id"],
            "window_id": plan["window_id"],
            "load_grade": plan["load_grade"],
            "load_target_m3": winner["requested_m3"],
            "sea_state_version_id": plan["sea_state_version_id"],
            "production_cut_percent": plan["production_cut_percent"],
            "storage_entries": storage_entries,
            "wait_entries": tail,
        }
        promoted_eval = self._evaluate(content, window_override=remaining_window)
        if not promoted_eval["feasible"] or not storage_entries:
            return {"entry_id": winner["entry_id"], "plan_id": None,
                    "reason": "候补首位在释放后的容量下仍不可行，保留在等待表"}
        self.connection.execute(
            "UPDATE plan_entries SET state='promoted' WHERE plan_id=? AND revision=? AND entry_id=?",
            (plan["plan_id"], plan["revision"], winner["entry_id"]),
        )
        now = self._now()
        lease_expires_at = self.window(plan["window_id"])["ends_at"]
        self.connection.execute(
            "INSERT INTO lift_plans(plan_id,vessel_id,window_id,load_grade,load_target_m3,loaded_m3,"
            "sea_state_version_id,production_cut_percent,state,revision,lease_minutes,lease_expires_at,"
            "evaluation_json,created_by,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'0',?,?, 'reserved', 1, 0,?,?,?,?,?)",
            (new_plan_id, winner["vessel_id"], plan["window_id"], plan["load_grade"],
             winner["requested_m3"], plan["sea_state_version_id"], plan["production_cut_percent"],
             lease_expires_at,
             canonical_json(promoted_eval), actor_id, now, now),
        )
        self._insert_entries(new_plan_id, 1, content)
        self._insert_buckets(new_plan_id, 1, promoted_eval)
        explanation = {
            "released": [],
            "occupied": self._lease_view(promoted_eval)["occupied"],
            "messages": [f"计划 {plan['plan_id']} 航次取消，候补按冻结优先级推进："
                         f"{winner['entry_id']}（{winner['vessel_id']}，优先级 {winner['priority']}）取得剩余窗口租约"],
            "capacity_view_after": self._capacity_view_sql(plan["window_id"]),
        }
        self._record_event(new_plan_id, 1, "waitlist.promoted", effective_hour, actor_id,
                           {"source_plan_id": plan["plan_id"], "winner_entry_id": winner["entry_id"]},
                           explanation)
        self._audit("plan", new_plan_id, "waitlist.promoted", actor_id,
                    {"source_plan_id": plan["plan_id"]})
        return {"entry_id": winner["entry_id"], "plan_id": new_plan_id,
                "vessel_id": winner["vessel_id"], "frozen_rank": winner["frozen_rank"]}

    # ------------------------------------------------------------------ 查询

    def plan(self, plan_id: str) -> dict[str, Any]:
        plan = self._plan_row(plan_id)
        result = dict(plan)
        result["entries"] = [dict(row) for row in self._current_entries(plan_id, plan["revision"])]
        result["evaluation"] = json.loads(plan["evaluation_json"])
        result["confirmations"] = {
            "platform": None if plan["platform_confirmed_by"] is None else {
                "by": plan["platform_confirmed_by"], "at": plan["platform_confirmed_at"]},
            "vessel": None if plan["vessel_confirmed_by"] is None else {
                "by": plan["vessel_confirmed_by"], "at": plan["vessel_confirmed_at"]},
        }
        return result

    def plan_events(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._require(actor_id, "plan.read")
        self._plan_row(plan_id)
        rows = self.connection.execute(
            "SELECT * FROM plan_events WHERE plan_id=? ORDER BY event_id", (plan_id,)
        ).fetchall()
        return {"plan_id": plan_id, "events": [
            {
                "event_id": row["event_id"],
                "revision": row["revision"],
                "event_type": row["event_type"],
                "effective_from": row["effective_from"],
                "actor_id": row["actor_id"],
                "detail": json.loads(row["detail_json"]),
                "capacity_explanation": json.loads(row["explanation_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]}

    def capacity_view(self, actor_id: str, window_id: str) -> dict[str, Any]:
        self._require(actor_id, "capacity.read")
        self.window(window_id)
        return {"window_id": window_id, "view": self._capacity_view_sql(window_id), "as_of": self._now()}

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM lifting_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
