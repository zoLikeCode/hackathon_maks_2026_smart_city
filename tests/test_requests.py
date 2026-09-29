import hashlib
import hmac
import io
import json
import tempfile
import time
import unittest
from email.message import Message
from pathlib import Path
from urllib.parse import urlencode

from src.auth_store import SqliteChairmanAuthorizationStore
from src.bot import handle_update, parse_specialist_user_ids
from src.mini_app import MiniAppServer
from src.request_store import ServiceRequest, SqliteRequestStore
from src.verification_store import PhoneVerificationStore


def signed_init_data(
    bot_token: str,
    user_id: int,
    *,
    claimed_role: str | None = None,
    start_param: str | None = None,
) -> str:
    user = {"id": user_id, "first_name": f"User {user_id}"}
    if claimed_role:
        user["role"] = claimed_role
    values = {
        "auth_date": str(int(time.time())),
        "user": json.dumps(user, separators=(",", ":")),
    }
    if start_param is not None:
        values["start_param"] = start_param
    launch_params = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret_key, launch_params.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class RequestStoreTests(unittest.TestCase):
    def test_persists_requests_and_updates_priority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.sqlite3"
            store = SqliteRequestStore(path)
            first = store.create(
                space_id="hoa-1",
                created_by_user_id=42,
                title="Не работает лифт",
                category="Лифт",
                description="Застрял на пятом этаже",
                address="ул. Мира, 1",
                scheduled_for="2026-10-01",
            )
            store.create(
                space_id="hoa-2",
                created_by_user_id=43,
                title="Освещение",
                category="Электрика",
                description="Не горит свет",
                address="ул. Мира, 2",
                scheduled_for=None,
            )

            reopened = SqliteRequestStore(path)
            self.assertEqual(len(reopened.list_all()), 2)
            self.assertEqual([item.id for item in reopened.list_for_space("hoa-1")], [first.id])
            changed = reopened.set_priority(first.id, "urgent")
            self.assertIsNotNone(changed)
            self.assertEqual(changed.priority, "urgent")
            self.assertEqual(changed.scheduled_for, "2026-10-01")
            self.assertEqual(reopened.list_for_space("hoa-1")[0].priority, "urgent")
            changed = reopened.set_status(first.id, "in_progress")
            self.assertEqual(changed.status, "in_progress")
            changed = reopened.set_schedule(first.id, "2026-10-03")
            self.assertEqual(changed.scheduled_for, "2026-10-03")
            self.assertIsNone(reopened.set_schedule(first.id, None).scheduled_for)
            self.assertEqual(SqliteRequestStore(path).list_for_space("hoa-1")[0].status, "in_progress")
            self.assertIsNone(SqliteRequestStore(path).list_for_space("hoa-1")[0].scheduled_for)
            self.assertIsNone(reopened.set_priority("missing", "low"))
            self.assertIsNone(reopened.set_status("missing", "done"))
            self.assertIsNone(reopened.set_schedule("missing", None))
            with self.assertRaises(ValueError):
                reopened.set_priority(first.id, "critical")
            with self.assertRaises(ValueError):
                reopened.set_status(first.id, "closed")
            with self.assertRaises(ValueError):
                reopened.set_schedule(first.id, "2026-02-30")

    def test_specialist_ids_are_explicit_positive_max_ids(self) -> None:
        self.assertEqual(parse_specialist_user_ids("42, 100,42"), frozenset({42, 100}))
        self.assertEqual(parse_specialist_user_ids(""), frozenset())
        with self.assertRaises(ValueError):
            parse_specialist_user_ids("42, nope")
        with self.assertRaises(ValueError):
            parse_specialist_user_ids("0")

    def test_allowlisted_specialist_gets_app_button_from_bot(self) -> None:
        class FakeApi:
            def __init__(self) -> None:
                self.sent = []

            def send_message(self, text: str, **options: object) -> None:
                self.sent.append((text, options))

        api = FakeApi()
        handle_update(
            api,
            {"update_type": "bot_started", "user": {"user_id": 100}},
            bot_username="smart_city_bot",
            specialist_user_ids=frozenset({100}),
        )
        self.assertIn("специалиста", api.sent[0][0])
        button = api.sent[0][1]["attachments"][0]["payload"]["buttons"][0][0]
        self.assertEqual(button["text"], "Открыть заявки")


class RequestApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        database_path = Path(self.directory.name) / "app.sqlite3"
        authorization = SqliteChairmanAuthorizationStore(database_path)
        authorization.approve_chairman(
            user_id=42,
            phone="+79990000042",
            full_name="Анна Иванова",
            space_id="hoa-1",
            hoa_name="Дом 1",
            address="ул. Мира, 1",
            protocol_sha256="protocol-1",
            protocol_filename="protocol-1.pdf",
        )
        authorization.approve_chairman(
            user_id=43,
            phone="+79990000043",
            full_name="Борис Петров",
            space_id="hoa-2",
            hoa_name="Дом 2",
            address="ул. Мира, 2",
            protocol_sha256="protocol-2",
            protocol_filename="protocol-2.pdf",
        )
        self.authorization = authorization
        self.phone_store = PhoneVerificationStore(database_path)
        self.phone_store.save(500, "+79872660500")
        self.phone_store.save(501, "+79969433497")
        self.server = MiniAppServer(
            authorization,
            "test-token",
            request_repository=SqliteRequestStore(database_path),
            specialist_user_ids={100},
            phone_repository=self.phone_store,
            host="127.0.0.1",
            port=0,
        )
        self.handler_type = self.server._handler()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def call(
        self,
        path: str,
        *,
        user_id: int | None = None,
        method: str = "GET",
        body: dict | None = None,
        claimed_role: str | None = None,
        start_param: str | None = None,
        init_data: str | None = None,
    ) -> tuple[int, dict]:
        headers = Message()
        if user_id is not None or init_data is not None:
            headers["Authorization"] = (
                "tma " + (init_data or signed_init_data(
                    "test-token", user_id, claimed_role=claimed_role, start_param=start_param
                ))
            )
        data = b""
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
            headers["Content-Length"] = str(len(data))
        handler = self.handler_type.__new__(self.handler_type)
        handler.path = path
        handler.headers = headers
        handler.rfile = io.BytesIO(data)
        handler.wfile = io.BytesIO()
        handler.command = method
        handler.requestline = f"{method} {path} HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        getattr(handler, f"do_{method}")()
        response = handler.wfile.getvalue()
        head, response_body = response.split(b"\r\n\r\n", 1)
        status = int(head.split(b"\r\n", 1)[0].split()[1])
        return status, json.loads(response_body)

    def test_session_requires_signed_user_and_server_role(self) -> None:
        self.assertEqual(self.call("/api/session")[0], 401)
        status, specialist = self.call("/api/session", user_id=100)
        self.assertEqual(status, 200)
        self.assertEqual(specialist["role"], "specialist")
        status, chairman = self.call("/api/session", user_id=42, claimed_role="specialist")
        self.assertEqual(status, 200)
        self.assertEqual(chairman["role"], "chairman")
        self.assertEqual(chairman["address"], "ул. Мира, 1")
        self.assertEqual(self.call("/api/session", user_id=999)[0], 403)
        self.assertEqual(self.call("/api/profile", user_id=42)[1]["role"], "Председатель ТСЖ")

    def test_verified_phone_can_choose_signed_specialist_or_chairman_launch(self) -> None:
        status, specialist = self.call(
            "/api/session", user_id=500, start_param="role_specialist"
        )
        self.assertEqual(status, 200)
        self.assertEqual(specialist["role"], "specialist")
        self.assertEqual(self.call("/api/requests", user_id=500, start_param="role_specialist")[0], 200)

        status, chairman = self.call(
            "/api/session", user_id=500, start_param="role_chairman"
        )
        self.assertEqual(status, 200)
        self.assertEqual(chairman["role"], "chairman")
        self.assertTrue(chairman["demo_access"])
        self.assertEqual(self.call("/api/requests", user_id=500, start_param="role_chairman")[0], 403)
        self.assertEqual(
            self.call(
                "/api/requests",
                user_id=500,
                start_param="role_chairman",
                method="POST",
                body={"title": "Test", "category": "Test", "description": "Test"},
            )[0],
            403,
        )
        status, owner = self.call("/api/session", user_id=500, start_param="role_owner")
        self.assertEqual(status, 200)
        self.assertEqual(owner["role"], "owner")
        self.assertTrue(owner["demo_access"])
        self.assertEqual(self.call("/api/requests", user_id=500, start_param="role_owner")[0], 403)
        self.assertEqual(self.call("/api/profile", user_id=500, start_param="role_owner")[0], 403)

    def test_specialist_request_store_failure_returns_json(self) -> None:
        class FailingRequestStore:
            def list_all(self) -> list:
                raise RuntimeError("database unavailable")

        self.server._request_repository = FailingRequestStore()
        self.handler_type = self.server._handler()
        with self.assertLogs("src.mini_app", level="ERROR") as logs:
            status, payload = self.call(
                "/api/requests", user_id=500, start_param="role_specialist"
            )
        self.assertEqual(status, 503)
        self.assertEqual(payload, {"error": "Не удалось загрузить заявки. Попробуйте ещё раз."})
        self.assertIn("database unavailable", "\n".join(logs.output))

    def test_legacy_32_character_request_id_reaches_specialist_patch_actions(self) -> None:
        request_id = "a" * 32

        class RecordingRequestStore:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, str | None]] = []

            def _updated(self, action: str, item_id: str, value: str | None) -> ServiceRequest:
                self.calls.append((action, item_id, value))
                return ServiceRequest(
                    id=item_id,
                    title="Лифт",
                    category="Без категории",
                    description="Не работает",
                    address="ул. Мира, 1",
                    created_at="2026-09-29T10:00:00+00:00",
                    scheduled_for=value if action == "schedule" else None,
                    status=value if action == "status" else "new",
                    priority=value if action == "priority" else "normal",
                    assignee=None,
                )

            def set_priority(self, item_id: str, value: str) -> ServiceRequest:
                return self._updated("priority", item_id, value)

            def set_status(self, item_id: str, value: str) -> ServiceRequest:
                return self._updated("status", item_id, value)

            def set_schedule(self, item_id: str, value: str | None) -> ServiceRequest:
                return self._updated("schedule", item_id, value)

        repository = RecordingRequestStore()
        self.server._request_repository = repository
        self.handler_type = self.server._handler()
        actions = (
            ("priority", {"priority": "urgent"}, "urgent"),
            ("status", {"status": "done"}, "done"),
            ("schedule", {"scheduled_for": "2026-10-05"}, "2026-10-05"),
        )
        for action, body, value in actions:
            status, payload = self.call(
                f"/api/requests/{request_id}/{action}",
                user_id=100,
                method="PATCH",
                body=body,
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["request"]["id"], request_id)
            self.assertEqual(repository.calls[-1], (action, request_id, value))

        other_id = "A_2-" * 8
        status, _payload = self.call(
            f"/api/requests/{other_id}/priority",
            user_id=100,
            method="PATCH",
            body={"priority": "high"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(repository.calls[-1], ("priority", other_id, "high"))

        calls_before_invalid = len(repository.calls)
        for invalid_id in ("a" * 31, "!" * 32):
            self.assertEqual(
                self.call(
                    f"/api/requests/{invalid_id}/priority",
                    user_id=100,
                    method="PATCH",
                    body={"priority": "high"},
                )[0],
                404,
            )
        self.assertEqual(len(repository.calls), calls_before_invalid)

    def test_role_launch_cannot_be_spoofed_or_changed_without_signature(self) -> None:
        self.assertEqual(self.call("/api/session", user_id=500)[0], 403)
        self.assertEqual(self.call("/api/session?start_param=role_specialist", user_id=500)[0], 403)
        self.assertEqual(
            self.call("/api/session", user_id=500, claimed_role="specialist")[0], 403
        )
        self.assertEqual(self.call("/api/session", user_id=500, start_param="role_unknown")[0], 403)
        self.assertEqual(self.call("/api/session", user_id=999, start_param="role_specialist")[0], 403)
        status, ordinary_chairman = self.call(
            "/api/session", user_id=42, start_param="role_specialist", claimed_role="specialist"
        )
        self.assertEqual(status, 200)
        self.assertEqual(ordinary_chairman["role"], "chairman")

        signed = signed_init_data("test-token", 500, start_param="role_chairman")
        tampered = signed.replace("role_chairman", "role_specialist")
        self.assertEqual(self.call("/api/session", init_data=tampered)[0], 401)

        self.phone_store.save(500, "+79991234567")
        self.assertEqual(
            self.call("/api/session", user_id=500, start_param="role_specialist")[0], 403
        )

    def test_second_verified_admin_can_open_each_role(self) -> None:
        self.assertEqual(self.call("/api/session", user_id=501)[0], 403)
        for start_param, role in (
            ("role_specialist", "specialist"),
            ("role_chairman", "chairman"),
            ("role_owner", "owner"),
        ):
            with self.subTest(role=role):
                status, session = self.call(
                    "/api/session", user_id=501, start_param=start_param
                )
                self.assertEqual(status, 200)
                self.assertEqual(session["role"], role)
                if role == "specialist":
                    self.assertNotIn("demo_access", session)
                    self.assertEqual(
                        self.call("/api/requests", user_id=501, start_param=start_param)[0], 200
                    )
                else:
                    self.assertTrue(session["demo_access"])
                    self.assertEqual(session["phone"], "+7•••••3497")
                    self.assertEqual(
                        self.call("/api/requests", user_id=501, start_param=start_param)[0], 403
                    )

        self.phone_store.save(501, "+79991234567")
        self.assertEqual(
            self.call("/api/session", user_id=501, start_param="role_specialist")[0], 403
        )
        self.assertEqual(
            self.call("/api/session", user_id=500, start_param="role_specialist")[0], 200
        )

    def test_verified_chairman_profile_is_real_only_in_chairman_mode(self) -> None:
        self.authorization.approve_chairman(
            user_id=500,
            phone="+79872660500",
            full_name="Тестовый председатель",
            space_id="hoa-test-500",
            hoa_name="Тестовый дом",
            address="Тестовая улица, 1",
            protocol_sha256="protocol-500",
            protocol_filename="protocol-500.pdf",
        )
        status, chairman = self.call("/api/session", user_id=500, start_param="role_chairman")
        self.assertEqual(status, 200)
        self.assertEqual(chairman["role"], "chairman")
        self.assertNotIn("demo_access", chairman)
        self.assertEqual(self.call("/api/profile", user_id=500, start_param="role_chairman")[0], 200)
        self.assertEqual(self.call("/api/requests", user_id=500, start_param="role_chairman")[0], 200)

        status, owner = self.call("/api/session", user_id=500, start_param="role_owner")
        self.assertEqual(status, 200)
        self.assertEqual(owner["role"], "owner")
        self.assertTrue(owner["demo_access"])
        self.assertNotIn("space_id", owner)
        self.assertEqual(self.call("/api/profile", user_id=500, start_param="role_owner")[0], 403)

    def test_chairman_creates_own_request_and_specialist_prioritizes(self) -> None:
        payload = {
            "title": "  Не работает лифт  ",
            "category": "Лифт",
            "description": "Застрял на пятом этаже",
            "scheduled_for": "2026-10-01",
        }
        status, created = self.call("/api/requests", user_id=42, method="POST", body=payload)
        self.assertEqual(status, 201)
        item = created["request"]
        self.assertEqual(item["title"], "Не работает лифт")
        self.assertEqual(item["address"], "ул. Мира, 1")
        self.assertEqual(item["status"], "new")
        self.assertEqual(item["priority"], "normal")
        self.assertEqual(item["scheduled_for"], "2026-10-01")
        self.assertIsNone(item["assignee"])
        self.assertEqual(len(self.call("/api/requests", user_id=42)[1]["requests"]), 1)
        self.assertEqual(self.call("/api/requests", user_id=43)[1]["requests"], [])
        self.assertEqual(len(self.call("/api/requests", user_id=100)[1]["requests"]), 1)

        url = f"/api/requests/{item['id']}/priority"
        self.assertEqual(self.call(url, user_id=42, method="PATCH", body={"priority": "high"})[0], 403)
        status, changed = self.call(
            url, user_id=100, method="PATCH", body={"priority": "urgent"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(changed["request"]["priority"], "urgent")
        self.assertEqual(self.call("/api/requests", user_id=100)[1]["requests"][0]["priority"], "urgent")

        status_url = f"/api/requests/{item['id']}/status"
        schedule_url = f"/api/requests/{item['id']}/schedule"
        self.assertEqual(
            self.call(status_url, user_id=42, method="PATCH", body={"status": "done"})[0],
            403,
        )
        self.assertEqual(
            self.call(schedule_url, user_id=42, method="PATCH", body={"scheduled_for": None})[0],
            403,
        )
        status, changed = self.call(
            status_url, user_id=100, method="PATCH", body={"status": "in_progress"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(changed["request"]["status"], "in_progress")
        status, changed = self.call(
            schedule_url,
            user_id=100,
            method="PATCH",
            body={"scheduled_for": "2026-10-04"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(changed["request"]["scheduled_for"], "2026-10-04")
        self.assertEqual(
            self.call("/api/requests", user_id=100)[1]["requests"][0]["scheduled_for"],
            "2026-10-04",
        )
        status, changed = self.call(
            schedule_url, user_id=100, method="PATCH", body={"scheduled_for": None}
        )
        self.assertEqual(status, 200)
        self.assertIsNone(changed["request"]["scheduled_for"])

    def test_rejects_invalid_requests_and_role_spoofing(self) -> None:
        good = {"title": "Лифт", "category": "Лифт", "description": "Не работает"}
        self.assertEqual(self.call("/api/requests", user_id=100, method="POST", body=good)[0], 403)
        self.assertEqual(self.call("/api/requests", user_id=42, method="POST", body={**good, "space_id": "hoa-2"})[0], 400)
        self.assertEqual(self.call("/api/requests", user_id=42, method="POST", body={**good, "scheduled_for": "2026-02-30"})[0], 400)
        self.assertEqual(self.call("/api/requests", user_id=42, method="POST", body={**good, "title": " "})[0], 400)
        missing_url = "/api/requests/00000000-0000-0000-0000-000000000000/priority"
        self.assertEqual(self.call(missing_url, user_id=100, method="PATCH", body={"priority": "high"})[0], 404)
        self.assertEqual(self.call(missing_url, user_id=100, method="PATCH", body={"priority": []})[0], 400)
        status_url = "/api/requests/00000000-0000-0000-0000-000000000000/status"
        schedule_url = "/api/requests/00000000-0000-0000-0000-000000000000/schedule"
        self.assertEqual(self.call(status_url, user_id=100, method="PATCH", body={"status": "done"})[0], 404)
        self.assertEqual(self.call(status_url, user_id=100, method="PATCH", body={"status": "closed"})[0], 400)
        self.assertEqual(self.call(schedule_url, user_id=100, method="PATCH", body={"scheduled_for": "2026-02-30"})[0], 400)
        self.assertEqual(self.call(schedule_url, user_id=100, method="PATCH", body={"scheduled_for": ""})[0], 400)
        self.assertEqual(self.call(schedule_url, user_id=100, method="PATCH", body={"scheduled_for": None, "status": "done"})[0], 400)


if __name__ == "__main__":
    unittest.main()
