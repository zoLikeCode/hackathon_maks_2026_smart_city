"""Contract tests for the merged HOA request API and the isolated legacy store."""

import hashlib
import hmac
import io
import json
import sqlite3
import tempfile
import time
import unittest
from email.message import Message
from pathlib import Path
from urllib.parse import urlencode

from src.auth_store import SqliteChairmanAuthorizationStore
from src.hoa_store import HoaStore
from src.mini_app import MiniAppServer
from src.request_store import SqliteRequestStore
from src.verification_store import PhoneVerificationStore


def signed_init_data(bot_token: str, user_id: int, *, claimed_role=None, start_param=None):
    user = {"id": user_id, "first_name": f"User {user_id}"}
    if claimed_role:
        user["role"] = claimed_role
    values = {"auth_date": str(int(time.time())), "user": json.dumps(user, separators=(",", ":"))}
    if start_param is not None:
        values["start_param"] = start_param
    launch_params = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, launch_params.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class RequestStoreTests(unittest.TestCase):
    def test_isolated_store_persists_scoped_requests_and_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            store = SqliteRequestStore(path)
            first = store.create(
                space_id="hoa-1", created_by_user_id=42, title="Лифт", category="Лифт",
                description="Не работает", address="ул. Мира, 1", scheduled_for="2026-10-01",
            )
            store.create(
                space_id="hoa-2", created_by_user_id=43, title="Свет", category="Электрика",
                description="Нет света", address="ул. Мира, 2", scheduled_for=None,
            )
            reopened = SqliteRequestStore(path)
            self.assertEqual([item.id for item in reopened.list_for_space("hoa-1")], [first.id])
            self.assertEqual(reopened.set_priority(first.id, "urgent").priority, "urgent")
            self.assertEqual(reopened.set_status(first.id, "in_progress").status, "in_progress")
            self.assertEqual(reopened.set_schedule(first.id, None).scheduled_for, None)
            self.assertIsNone(reopened.set_priority("missing", "low"))
            with self.assertRaises(ValueError):
                reopened.set_priority(first.id, "emergency")


class RequestApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        database_path = root / "hoa.sqlite3"
        self.authorization = SqliteChairmanAuthorizationStore(database_path)
        for user_id, space_id in ((42, "hoa-1"), (43, "hoa-2")):
            self.authorization.approve_chairman(
                user_id=user_id, phone=f"+799900000{user_id}", full_name=f"Председатель {user_id}",
                space_id=space_id, hoa_name=f"Дом {user_id}", address=f"ул. Мира, {user_id}",
                protocol_sha256=f"protocol-{user_id}", protocol_filename=f"protocol-{user_id}.pdf",
            )
        self.hoa = HoaStore(None, database_path)
        self.phone_store = PhoneVerificationStore(database_path)
        self.phone_store.save(500, "+79872660500")
        self.phone_store.save(501, "+79969433497")
        self.phone_store.save(100, "+79990000100")
        self.legacy = SqliteRequestStore(root / "legacy.sqlite3")
        self.server = MiniAppServer(
            self.authorization, "test-token", hoa_store=self.hoa, phone_store=self.phone_store,
            request_repository=self.legacy, host="127.0.0.1", port=0,
        )
        self.handler_type = self.server._handler()
        self.database_path = database_path

    def tearDown(self):
        self.directory.cleanup()

    def call(self, path, *, user_id=None, method="GET", body=None, claimed_role=None,
             start_param=None, init_data=None):
        headers = Message()
        if user_id is not None or init_data is not None:
            headers["Authorization"] = "tma " + (init_data or signed_init_data(
                "test-token", user_id, claimed_role=claimed_role, start_param=start_param,
            ))
        data = b""
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
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
        head, response = handler.wfile.getvalue().split(b"\r\n\r\n", 1)
        status = int(head.split(b"\r\n", 1)[0].split()[1])
        content_type = next((line.split(b":", 1)[1].strip().decode() for line in head.split(b"\r\n")
                             if line.lower().startswith(b"content-type:")), "")
        return status, json.loads(response) if content_type.startswith("application/json") else response, content_type

    def seed_resident(self, *, user_id=200, space_id="hoa-1", resident_id="resident-1"):
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """INSERT INTO residents (resident_id, space_id, unit, full_name, area, share,
                   ownership, code_prefix, code_secret, user_id, phone, active, updated_at)
                   VALUES (?, ?, ?, ?, '40', '1', 'собственность', ?, 'secret', ?, NULL, 1, 'now')""",
                (resident_id, space_id, resident_id, "Иванов Иван", resident_id, user_id),
            )
        return resident_id

    def seed_request(self, *, user_id=200, resident_id="resident-1", specialist_id=None,
                     photo=b"\x89PNG\r\nrequest-photo"):
        created = self.hoa.create_request(
            user_id, resident_id, "Течёт труба", "Нужен ремонт трубы",
            specialty="plumber" if specialist_id else "other", specialist_id=specialist_id,
            source="chat",
        )
        request_id = created["request_id"]
        if photo is not None:
            with sqlite3.connect(self.database_path) as connection:
                connection.execute(
                    """INSERT INTO request_photos (request_id, position, mime_type, content, created_at)
                       VALUES (?, 1, 'image/png', ?, 'now')""", (request_id, photo),
                )
        return request_id

    def test_signed_role_switches_for_both_admin_phones_and_spoof_rejection(self):
        self.assertEqual(self.call("/api/session")[0], 401)
        for user_id in (500, 501):
            self.assertEqual(self.call("/api/session", user_id=user_id)[0], 403)
            for launch, role in (("role_chairman", "chairman"), ("role_specialist", "specialist"),
                                 ("role_owner", "owner")):
                with self.subTest(user_id=user_id, role=role):
                    status, session, _ = self.call("/api/session", user_id=user_id, start_param=launch)
                    self.assertEqual(status, 200)
                    self.assertEqual(session["role"], role)
                    self.assertTrue(session["demo_access"])
                    self.assertEqual(self.call("/api/requests", user_id=user_id, start_param=launch)[0], 403)
            self.assertEqual(self.call("/api/session?start_param=role_specialist", user_id=user_id)[0], 403)
            self.assertEqual(self.call("/api/session", user_id=user_id, claimed_role="chairman")[0], 403)
            signed = signed_init_data("test-token", user_id, start_param="role_chairman")
            self.assertEqual(self.call("/api/session", init_data=signed.replace("role_chairman", "role_owner"))[0], 401)
        self.assertEqual(self.call("/api/session", user_id=999, start_param="role_chairman")[0], 403)
        self.phone_store.save(500, "+79991234567")
        self.assertEqual(self.call("/api/session", user_id=500, start_param="role_specialist")[0], 403)
        self.assertEqual(self.call("/api/session", user_id=501, start_param="role_specialist")[0], 200)

    def test_real_chairman_and_demo_role_are_distinct(self):
        self.authorization.approve_chairman(
            user_id=500, phone="+79872660500", full_name="Тестовый председатель",
            space_id="hoa-500", hoa_name="Тестовый дом", address="Тестовая улица, 1",
            protocol_sha256="protocol-500", protocol_filename="protocol-500.pdf",
        )
        status, chairman, _ = self.call("/api/session", user_id=500, start_param="role_chairman")
        self.assertEqual(status, 200)
        self.assertEqual(chairman["space_id"], "hoa-500")
        self.assertNotIn("demo_access", chairman)
        self.assertEqual(self.call("/api/profile", user_id=500, start_param="role_chairman")[0], 200)
        self.assertEqual(self.call("/api/requests", user_id=500, start_param="role_chairman")[0], 200)
        status, owner, _ = self.call("/api/session", user_id=500, start_param="role_owner")
        self.assertEqual(status, 200)
        self.assertTrue(owner["demo_access"])
        self.assertNotIn("space_id", owner)
        self.assertEqual(self.call("/api/profile", user_id=500, start_param="role_owner")[0], 403)
        self.assertEqual(self.call("/api/specialists", user_id=500, start_param="role_owner")[0], 403)

    def test_chairman_creates_and_lists_specialists_only_in_own_space(self):
        body = {"specialty": "plumber", "full_name": "Петров Пётр", "phone": "+79990000100"}
        status, specialist, _ = self.call("/api/specialists", user_id=42, method="POST", body=body)
        self.assertEqual(status, 201)
        specialist_id = specialist["specialist_id"]
        self.assertEqual(self.call("/api/specialists", user_id=42)[1]["specialists"][0]["specialist_id"], specialist_id)
        self.assertEqual(self.call("/api/specialists", user_id=43)[1]["specialists"], [])
        self.assertEqual(self.call("/api/specialists", user_id=100, method="POST", body=body)[0], 403)
        self.assertEqual(self.call("/api/specialists", user_id=500, start_param="role_chairman",
                                   method="POST", body=body)[0], 403)
        self.assertEqual(self.call(f"/api/specialists/{specialist_id}", user_id=43, method="PATCH",
                                   body={**body, "work_hours": []})[0], 404)
        self.hoa.claim_specialist_phone(100, "+79990000100")
        self.assertEqual(self.call("/api/session", user_id=100)[1]["role"], "specialist")

    def test_requests_are_scoped_to_chairman_owner_and_assigned_specialist(self):
        self.seed_resident()
        self.seed_resident(user_id=201, space_id="hoa-2", resident_id="resident-2")
        specialist = self.hoa.create_specialist("hoa-1", "plumber", "Петров Пётр", "+79990000100")
        self.hoa.claim_specialist_phone(100, "+79990000100")
        own_id = self.seed_request(specialist_id=specialist["specialist_id"])
        unassigned_id = self.seed_request(photo=None)
        other_id = self.seed_request(user_id=201, resident_id="resident-2", photo=None)
        for user_id, expected in ((42, {own_id, unassigned_id}), (43, {other_id}),
                                  (100, {own_id}), (200, {own_id, unassigned_id}), (201, {other_id})):
            with self.subTest(user_id=user_id):
                status, payload, _ = self.call("/api/requests", user_id=user_id)
                self.assertEqual(status, 200)
                self.assertEqual({item["request_id"] for item in payload["requests"]}, expected)
        self.assertEqual(self.call("/api/requests", user_id=100)[1]["requests"][0]["photo_count"], 1)
        self.assertEqual(self.call("/api/requests", user_id=100)[1]["requests"][0]["id"], own_id)
        assigned_url = f"/api/requests/{own_id}"
        self.assertEqual(self.call(assigned_url, user_id=100, method="PATCH",
                                   body={"status": "done"})[0], 400)
        status, changed, _ = self.call(assigned_url, user_id=100, method="PATCH",
                                       body={"status": "in_progress", "priority": "today"})
        self.assertEqual(status, 200)
        self.assertEqual(changed["request"]["status"], "in_progress")
        self.assertEqual(self.call(assigned_url, user_id=100, method="PATCH",
                                   body={"status": "done"})[0], 200)
        self.assertEqual(self.call(f"/api/requests/{unassigned_id}", user_id=100,
                                   method="PATCH", body={"priority": "urgent"})[0], 404)

    def test_photos_return_original_bytes_only_to_authorized_participants(self):
        self.seed_resident()
        specialist = self.hoa.create_specialist("hoa-1", "plumber", "Петров Пётр", "+79990000100")
        self.hoa.claim_specialist_phone(100, "+79990000100")
        photo = b"\x89PNG\r\n\x1a\nunique-test-pixels"
        request_id = self.seed_request(specialist_id=specialist["specialist_id"], photo=photo)
        url = f"/api/requests/{request_id}/photos/1"
        for user_id in (42, 100, 200):
            status, content, content_type = self.call(url, user_id=user_id)
            self.assertEqual((status, content_type, content), (200, "image/png", photo))
        for user_id in (43, 201, 999):
            self.assertNotEqual(self.call(url, user_id=user_id)[0], 200)
        self.assertEqual(self.call(url, user_id=500, start_param="role_owner")[0], 403)
        self.assertEqual(self.call(url)[0], 401)
        self.assertEqual(self.call(f"/api/requests/{request_id}/photos/4", user_id=42)[0], 404)

    def test_historical_isolated_rows_are_chairman_scoped_readonly_and_have_no_post(self):
        first = self.legacy.create(
            space_id="hoa-1", created_by_user_id=42, title="Старая заявка", category="Лифт",
            description="Лифт остановился", address="ул. Мира, 42", scheduled_for=None,
        )
        self.legacy.create(
            space_id="hoa-2", created_by_user_id=43, title="Другая заявка", category="Лифт",
            description="Лифт остановился", address="ул. Мира, 43", scheduled_for=None,
        )
        status, chairman, _ = self.call("/api/requests", user_id=42)
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in chairman["requests"]], [first.id])
        self.assertEqual(chairman["requests"][0]["source"], "mini_app_readonly")
        self.assertTrue(chairman["requests"][0]["read_only"])
        self.assertEqual(self.call("/api/requests", user_id=100)[0], 403)
        self.assertEqual(self.call("/api/requests", user_id=43)[1]["requests"][0]["title"], "Другая заявка")
        self.assertEqual(self.call("/api/requests", user_id=42, method="POST",
                                   body={"title": "Новая", "description": "Новая заявка"})[0], 404)
        self.assertEqual(self.call(f"/api/requests/{first.id}", user_id=42, method="PATCH",
                                   body={"priority": "urgent"})[0], 404)
        self.assertEqual(self.call(f"/api/requests/{first.id}/photos/1", user_id=42)[0], 404)

    def test_main_request_controls_use_single_scoped_patch_route(self):
        self.seed_resident()
        request_id = self.seed_request(photo=None)
        url = f"/api/requests/{request_id}"
        self.assertEqual(self.call(url, user_id=43, method="PATCH", body={"priority": "urgent"})[0], 404)
        self.assertEqual(self.call(url, user_id=200, method="PATCH", body={"priority": "urgent"})[0], 403)
        self.assertEqual(self.call(url, user_id=42, method="PATCH", body={"priority": "invalid"})[0], 400)
        status, result, _ = self.call(url, user_id=42, method="PATCH", body={"priority": "urgent"})
        self.assertEqual(status, 200)
        self.assertEqual(result["request"]["priority"], "urgent")
        self.assertEqual(self.call(f"{url}/priority", user_id=42, method="PATCH",
                                   body={"priority": "today"})[0], 404)


if __name__ == "__main__":
    unittest.main()
