import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .domain import (
    ConflictError,
    DomainError,
    InvalidTransition,
    NotFoundError,
    PermissionDenied,
    ValidationError,
    Actor,
)


def _json_bytes(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def create_handler(service, rules, static_dir, quota=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ModularPython/1.0"

        def log_message(self, format, *args):
            return

        def _send(self, status, payload):
            body = _json_bytes(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, status, body):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _actor(self):
            return Actor.from_headers(self.headers)

        def _body(self):
            cached = getattr(self, "_cached_body", None)
            if cached is not None:
                return cached
            length = int(self.headers.get("Content-Length", "0") or 0)
            if not length:
                self._cached_body = {}
                return {}
            raw = self.rfile.read(length)
            try:
                value = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise ValidationError("request body must be valid JSON")
            if not isinstance(value, dict):
                raise ValidationError("request body must be a JSON object")
            self._cached_body = value
            return value

        def _fail(self, exc):
            if isinstance(exc, PermissionDenied):
                status = 403
            elif isinstance(exc, NotFoundError):
                status = 404
            elif isinstance(exc, (ConflictError, InvalidTransition)):
                status = 409
            elif isinstance(exc, ValidationError):
                status = 400
            elif isinstance(exc, DomainError):
                status = 400
            elif isinstance(exc, sqlite3.IntegrityError):
                status = 409
                exc = ConflictError(str(exc))
            else:
                status = 500
            self._send(status, {"error": str(exc), "type": type(exc).__name__})

        def do_GET(self):
            try:
                parsed = urlparse(self.path)
                parts = [part for part in parsed.path.split("/") if part]
                if parsed.path == "/health":
                    return self._send(200, service.health())
                if parsed.path == "/":
                    index = os.path.join(static_dir, "index.html")
                    with open(index, "r", encoding="utf-8") as handle:
                        return self._send_html(200, handle.read())
                if parts == ["api", "audit"]:
                    return self._send(200, {"items": service.audit_log()})
                if quota is not None:
                    response = self._quota_get(parsed, parts)
                    if response is not None:
                        return response
                if len(parts) == 3 and parts[:2] == ["api", "entities"]:
                    return self._send(200, service.get(parts[2]))
                if len(parts) >= 2 and parts[0] == "api":
                    if parts[1] == "entities":
                        raise NotFoundError("not found")
                    if len(parts) == 3:
                        return self._send(200, service.get(parts[2]))
                    query = parse_qs(parsed.query)
                    status = query.get("status", [None])[0]
                    return self._send(
                        200,
                        {"items": service.list(parts[1], status=status)},
                    )
                raise NotFoundError("not found")
            except Exception as exc:
                self._fail(exc)

        def do_POST(self):
            try:
                parsed = urlparse(self.path)
                parts = [part for part in parsed.path.split("/") if part]
                actor = self._actor()
                if quota is not None:
                    response = self._quota_post(parsed, parts, actor)
                    if response is not None:
                        return response
                if len(parts) == 3 and parts[:2] == ["api", "entities"]:
                    body = self._body()
                    action = body.pop("action", None)
                    if not action:
                        raise ValidationError("action is required")
                    data = body.pop("data", body)
                    expected = body.pop("expected_version", None)
                    return self._send(
                        200,
                        service.transition(actor, parts[2], action, data, expected),
                    )
                if len(parts) == 4 and parts[0] == "api" and parts[3] == "actions":
                    body = self._body()
                    action = body.pop("action", None)
                    if not action:
                        raise ValidationError("action is required")
                    return self._send(
                        200,
                        service.transition(
                            actor,
                            parts[2],
                            action,
                            body.pop("data", body),
                            body.pop("expected_version", None),
                        ),
                    )
                if len(parts) == 5 and parts[0] == "api" and parts[4] == "actions":
                    return self._send(
                        200,
                        service.transition(actor, parts[2], parts[3], self._body(), None),
                    )
                if len(parts) == 2 and parts[0] == "api":
                    body = self._body()
                    idem = self.headers.get("Idempotency-Key")
                    return self._send(
                        201,
                        service.create(actor, parts[1], body, idem),
                    )
                raise NotFoundError("not found")
            except Exception as exc:
                self._fail(exc)

        def _quota_get(self, parsed, parts):
            query = parse_qs(parsed.query)
            if len(parts) == 2 and parts[1] == "permits":
                kwargs = {}
                if query.get("status"):
                    kwargs["status"] = query["status"][0]
                if query.get("species"):
                    kwargs["species"] = query["species"][0]
                if query.get("year"):
                    kwargs["year"] = query["year"][0]
                return self._send(200, {"items": quota.list_permits(**kwargs)})
            if len(parts) == 4 and parts[1] == "permits" and parts[3] == "ledger":
                return self._send(200, quota.permit_ledger(parts[2]))
            if len(parts) == 2 and parts[1] == "pairings":
                kwargs = {}
                if query.get("status"):
                    kwargs["status"] = query["status"][0]
                if query.get("permit_no"):
                    kwargs["permit_no"] = query["permit_no"][0]
                return self._send(200, {"items": quota.list_pairings(**kwargs)})
            if len(parts) == 2 and parts[1] == "releases":
                kwargs = {}
                if query.get("pairing_id"):
                    kwargs["pairing_id"] = query["pairing_id"][0]
                return self._send(200, {"items": quota.list_releases(**kwargs)})
            if len(parts) == 2 and parts[1] == "registry-jobs":
                return self._send(200, {"items": quota.list_jobs()})
            if len(parts) == 3 and parts[1] == "registry-jobs":
                return self._send(200, quota.get_job_view(parts[2]))
            if len(parts) == 2 and parts[1] == "pending-receipts":
                return self._send(200, {"items": quota.list_pending()})
            return None

        def _quota_post(self, parsed, parts, actor):
            body = self._body()
            idem = self.headers.get("Idempotency-Key")

            # 许可证
            if len(parts) == 2 and parts[1] == "permits":
                return self._send(201, quota.issue_permit(actor, body))
            if len(parts) == 4 and parts[1] == "permits":
                permit_no, action = parts[2], parts[3]
                if action == "amend":
                    return self._send(200, quota.amend_permit(actor, permit_no, body))
                if action == "adjust-quota":
                    return self._send(
                        200,
                        quota.adjust_quota(actor, permit_no, body.get("quota")),
                    )
                if action == "expire":
                    return self._send(200, quota.expire_permit(actor, permit_no))
                if action == "withdraw":
                    return self._send(200, quota.withdraw_permit(actor, permit_no))
            # 配对
            if len(parts) == 2 and parts[1] == "pairings":
                return self._send(
                    201, quota.propose_pairing(actor, body, idem),
                )
            if len(parts) == 4 and parts[1] == "pairings":
                pairing_id, action = parts[2], parts[3]
                if action == "approve":
                    return self._send(
                        200,
                        quota.approve_pairing(
                            actor, pairing_id,
                            body.get("data", body),
                            body.get("expected_version"),
                        ),
                    )
                if action == "execute":
                    return self._send(
                        200,
                        quota.execute_pairing(
                            actor, pairing_id, body.get("data", body)
                        ),
                    )
                if action == "reject":
                    return self._send(
                        200,
                        quota.reject_pairing(
                            actor, pairing_id, body.get("data", body)
                        ),
                    )
            # 运输放行
            if len(parts) == 2 and parts[1] == "releases":
                return self._send(201, quota.create_release(actor, body, idem))
            # 外部登记回执对账
            if len(parts) == 2 and parts[1] == "registry-jobs":
                return self._send(
                    201, quota.open_registry_job(actor, body.get("job_no")),
                )
            if len(parts) == 4 and parts[1] == "registry-jobs":
                job_no, action = parts[2], parts[3]
                if action == "receipts":
                    receipts = body.get("receipts")
                    if receipts is None:
                        receipts = [body] if body.get("receipt_no") else []
                    return self._send(
                        200, quota.receive_receipts(actor, job_no, receipts),
                    )
                if action == "retry":
                    return self._send(200, quota.retry_pending(actor, job_no))
            if (len(parts) == 4 and parts[1] == "receipts"
                    and parts[3] == "resolve"):
                return self._send(
                    200,
                    quota.resolve_conflict(
                        actor, parts[2], body.get("resolution"),
                        body.get("note"),
                    ),
                )
            return None

    return Handler


def create_server(host, port, service, rules, static_dir, quota=None):
    handler = create_handler(service, rules, static_dir, quota=quota)
    return ThreadingHTTPServer((host, int(port)), handler)
