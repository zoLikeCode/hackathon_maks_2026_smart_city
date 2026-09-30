from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.demo_seed import ASSETS, DemoSetupError, PHONE, prepare_demo_connection
from src.auth_store import SqliteChairmanAuthorizationStore
from src.hoa_store import HoaStore
from src.verification_store import PhoneVerificationStore


class DemoSeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "demo.sqlite3"
        self.auth = SqliteChairmanAuthorizationStore(self.path)
        self.hoa = HoaStore(None, self.path)
        self.phone = PhoneVerificationStore(self.path)
        self.photos = {
            "pipe_photo": (ASSETS / "demo_pipe_leak.png").read_bytes(),
            "light_photo": (ASSETS / "demo_hall_light.png").read_bytes(),
        }

    def prepare(self, *, role: str = "owner", allow_existing_hoa: bool = False) -> dict:
        with sqlite3.connect(self.path) as connection:
            return prepare_demo_connection(
                connection, role=role, **self.photos, allow_existing_hoa=allow_existing_hoa
            )

    def test_three_real_roles_photo_calendar_and_user_request_survive_rerun(self) -> None:
        user_id = 6710500
        self.phone.save(user_id, PHONE)
        result = self.prepare()
        self.assertEqual(result["user_id"], user_id)
        self.assertEqual(
            (self.auth.get_state(user_id).role, self.auth.get_state(user_id).step),
            ("owner", "recording_demo_awaiting_phone"),
        )
        self.assertEqual(self.auth.get_profile_by_user_id(user_id).phone, PHONE)
        membership = next(item for item in self.hoa.memberships(user_id)
                          if item["unit"] == "ДЕМО-0500")
        self.assertEqual(membership["space_id"], result["space_id"])
        specialist = self.hoa.specialist_memberships(user_id, PHONE)
        self.assertEqual(len(specialist), 1)
        self.assertEqual(len(specialist[0]["work_hours"]), 7)

        owner_requests = self.hoa.list_requests(user_id=user_id)
        owner_done = next(item for item in owner_requests
                          if item["request_id"] == result["owner_done"])
        self.assertEqual((owner_done["status"], owner_done["photo_count"]), ("done", 1))
        self.assertEqual(
            self.hoa.get_request_photo(user_id, result["owner_done"], 1)[0], "image/png"
        )
        self.assertIsNotNone(
            self.hoa.get_request_photo(9999, result["owner_done"], 1,
                                       chairman_space_id=result["space_id"])
        )
        specialist_requests = self.hoa.list_specialist_requests(user_id, PHONE)
        open_request = next(item for item in specialist_requests
                            if item["request_id"] == result["specialist_open"])
        self.assertEqual((open_request["status"], open_request["priority"],
                          open_request["photo_count"]), ("review", "urgent", 1))
        self.assertIsNotNone(
            self.hoa.get_request_photo(user_id, result["specialist_open"], 1,
                                       specialist_phone=PHONE)
        )
        calendar_request = next(item for item in specialist_requests
                                if item["request_id"] == result["calendar"])
        self.assertEqual(calendar_request["visit_status"], "accepted")
        self.assertEqual(calendar_request["visit_date"], result["calendar_date"])

        created = self.hoa.create_request(
            user_id, membership["resident_id"], "Новая заявка из бота",
            "Заявка, созданная пользователем во время записи видео.",
        )
        self.hoa.set_specialist_request_status(user_id, PHONE, result["specialist_open"],
                                               "in_progress")
        rerun = self.prepare(role="specialist")
        self.assertEqual(rerun["space_id"], result["space_id"])
        self.assertEqual(
            (self.auth.get_state(user_id).role, self.auth.get_state(user_id).step),
            ("specialist", "recording_demo_awaiting_phone"),
        )
        self.assertEqual(
            next(item for item in self.hoa.list_specialist_requests(user_id, PHONE)
                 if item["request_id"] == result["specialist_open"])["status"], "review"
        )
        self.assertIn(created["request_id"],
                      {item["request_id"] for item in self.hoa.list_requests(user_id=user_id)})
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM service_requests").fetchone()[0], 5)

        self.prepare(role="chairman")
        self.assertEqual(
            (self.auth.get_state(user_id).role, self.auth.get_state(user_id).step),
            ("chairman", "recording_demo_awaiting_phone"),
        )
        self.assertIn(
            created["request_id"],
            {item["request_id"] for item in self.hoa.list_requests(user_id=user_id)},
        )

    def test_requires_unique_signed_contact_before_any_changes(self) -> None:
        with self.assertRaisesRegex(DemoSetupError, "не подтверждён"):
            self.prepare()
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM hoa_spaces").fetchone()[0], 0)
        self.phone.save(6710500, PHONE)
        self.phone.save(6710501, PHONE)
        with self.assertRaisesRegex(DemoSetupError, "нескольким MAX ID"):
            self.prepare()
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM hoa_spaces").fetchone()[0], 0)

    def test_reuses_existing_chairman_and_electrician_without_replacing_them(self) -> None:
        user_id = 6710500
        self.phone.save(user_id, PHONE)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT INTO hoa_spaces(space_id, name, address, created_at) "
                "VALUES ('existing-hoa', 'Действующее ТСЖ', 'Казань, ул. Ленина, 7', '2026-01-01')"
            )
            connection.execute(
                "INSERT INTO chairman_profiles(phone, user_id, full_name, space_id, "
                "protocol_sha256, protocol_filename, verified_at) "
                "VALUES (?, ?, 'Иван Иванов', 'existing-hoa', 'real-review', 'protocol.pdf', '2026-01-01')",
                (PHONE, user_id),
            )
            connection.execute(
                "INSERT INTO specialists(specialist_id, space_id, specialty, full_name, phone, "
                "user_id, created_at, updated_at) "
                "VALUES ('existing-electrician', 'existing-hoa', 'electrician', "
                "'Пётр Петров', ?, ?, '2026-01-01', '2026-01-01')",
                (PHONE, user_id),
            )
            connection.execute(
                "INSERT INTO specialist_hours(specialist_id, weekday, start_hour, end_hour) "
                "VALUES ('existing-electrician', 0, 9, 17)"
            )
        with self.assertRaisesRegex(DemoSetupError, "--use-existing-hoa"):
            self.prepare()
        result = self.prepare(allow_existing_hoa=True)
        self.assertEqual(result["space_id"], "existing-hoa")
        self.assertEqual(result["specialty"], "electrician")
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute("SELECT protocol_filename FROM chairman_profiles WHERE user_id = ?",
                                   (user_id,)).fetchone()[0], "protocol.pdf"
            )
            self.assertEqual(
                connection.execute("SELECT full_name FROM specialists WHERE phone = ?",
                                   (PHONE,)).fetchone()[0], "Пётр Петров"
            )
            self.assertEqual(
                connection.execute("SELECT count(*) FROM specialist_hours WHERE specialist_id = "
                                   "'existing-electrician'").fetchone()[0], 1
            )
            self.assertEqual(
                connection.execute("SELECT title FROM service_requests WHERE request_id = ?",
                                   (result["specialist_open"],)).fetchone()[0],
                "ДЕМО · Не горит свет в прихожей",
            )

    def test_stale_phone_confirmation_cannot_grant_demo_roles(self) -> None:
        self.phone.save(6710500, PHONE)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE phone_verifications SET verified_at = '2025-01-01T00:00:00+00:00' "
                "WHERE user_id = 6710500"
            )
        with self.assertRaisesRegex(DemoSetupError, "подтвердить заново"):
            self.prepare()
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM hoa_spaces").fetchone()[0], 0)
if __name__ == "__main__":
    unittest.main()
