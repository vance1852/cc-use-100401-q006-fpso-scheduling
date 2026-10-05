"""无第三方依赖的储运与提油编排 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import LiftingError, ValidationFailed
from .service import LiftingService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    def __init__(self, service: LiftingService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        query = parse_qs(parsed.query)
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = self._actor(normalized)
            if method == "POST" and path == "/users":
                return Response(201, self.service.create_user(
                    payload["user_id"], payload["display_name"], payload["role"], payload["party"]))
            if method == "POST" and path == "/quality-limits":
                return Response(201, self.service.register_quality_limit(actor, payload))
            if method == "POST" and path == "/tanks":
                return Response(201, self.service.register_tank(actor, payload))
            if method == "POST" and path == "/vessels":
                return Response(201, self.service.register_vessel(actor, payload))
            if method == "POST" and path == "/windows":
                return Response(201, self.service.register_window(actor, payload))
            if method == "POST" and path == "/batches":
                return Response(201, self.service.register_batch(actor, payload))
            if method == "POST" and path == "/sea-states":
                return Response(201, self.service.register_sea_state_version(actor, payload))
            if method == "POST" and path == "/plans":
                return Response(201, self.service.draft_plan(actor, payload))
            if method == "GET" and path == "/plans/compare":
                return Response(200, self.service.compare_plans(actor, query.get("window_id", [None])[0]))
            if method == "GET" and len(parts) == 3 and parts[0] == "plans" and parts[2] == "events":
                return Response(200, self.service.plan_events(actor, parts[1]))
            if method == "GET" and len(parts) == 2 and parts[0] == "plans":
                return Response(200, self.service.plan(parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "plans" and parts[2] == "reserve":
                return Response(200, self.service.reserve_plan(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "plans" and parts[2] == "confirm":
                return Response(200, self.service.confirm_plan(actor, parts[1], payload["side"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "plans" and parts[2] == "seal":
                return Response(200, self.service.seal_plan(actor, parts[1]))
            if method == "POST" and len(parts) == 4 and parts[0] == "plans" and parts[2] == "amendments":
                plan_id, kind = parts[1], parts[3]
                if kind == "partial-loading":
                    return Response(200, self.service.record_partial_loading(actor, plan_id, payload["loaded_m3"]))
                if kind == "production-cut":
                    return Response(200, self.service.record_production_cut(actor, plan_id, payload["cut_percent"]))
                if kind == "tank-switch":
                    return Response(200, self.service.switch_tank(
                        actor, plan_id, payload["entry_id"], payload["to_tank_id"],
                        payload.get("opening_level_m3", 0)))
                if kind == "quality-downgrade":
                    return Response(200, self.service.downgrade_quality(
                        actor, plan_id, payload["new_grade"], payload.get("new_batch_id")))
            if method == "POST" and len(parts) == 3 and parts[0] == "plans" and parts[2] == "cancel":
                return Response(200, self.service.cancel_voyage(actor, parts[1]))
            if method == "GET" and path == "/capacity":
                return Response(200, self.service.capacity_view(actor, query.get("window_id", [""])[0]))
            if method == "GET" and path == "/audit/chain":
                return Response(200, self.service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except LiftingError as exc:
            body = {"error": {"code": exc.code, "message": str(exc)}}
            detail = getattr(exc, "detail", None)
            if detail is not None:
                body["error"]["detail"] = detail
            return Response(exc.status, body)
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    # 同一进程内串行化请求：多个工作线程共享一个 SQLite 连接，
    # 不能在同一连接上并发开启事务。跨进程的并发安全由数据库的
    # BEGIN IMMEDIATE 与独占桶唯一索引保证。
    dispatch_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "LiftingOrchestration/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            with dispatch_lock:
                response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动储运与提油编排服务")
    parser.add_argument("--database", type=Path, default=Path("lifting-orchestration.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(LiftingService(connection))))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
