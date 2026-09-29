from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src.hoa_store import HoaStore
from src.request_notifications import notify_request_changes


class SpecialistRequestTests(unittest.TestCase):
    def test_priority_and_status_transitions_for_assigned_specialist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hoa.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    """CREATE TABLE service_requests (
                         request_id TEXT PRIMARY KEY, space_id TEXT NOT NULL,
                         resident_id TEXT NOT NULL, user_id INTEGER NOT NULL,
                         title TEXT NOT NULL, description TEXT NOT NULL,
                         status TEXT NOT NULL, created_at TEXT NOT NULL,
                         updated_at TEXT NOT NULL)"""
                )
                connection.execute(
                    """INSERT INTO service_requests VALUES
                       ('old', 'hoa', 'resident', 1, 'Старая заявка', 'Описание',
                        'new', 'now', 'now')"""
                )
                connection.commit()
            store = HoaStore(None, path)
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(
                    connection.execute("SELECT status FROM service_requests WHERE request_id = 'old'").fetchone()[0],
                    "review",
                )
                connection.execute("DELETE FROM service_requests WHERE request_id = 'old'")
                connection.execute(
                    "CREATE TABLE hoa_spaces (space_id TEXT PRIMARY KEY, name TEXT, address TEXT)"
                )
                connection.execute(
                    "INSERT INTO hoa_spaces VALUES ('hoa', 'ТСЖ', 'Улица, 1')"
                )
                connection.execute(
                    """INSERT INTO residents
                       (resident_id, space_id, unit, full_name, area, share, ownership,
                        code_prefix, code_secret, user_id, phone, active, updated_at)
                       VALUES ('resident', 'hoa', '1', 'Иванов Иван', '40', '1', 'собственность',
                               'prefix', 'secret', 1, NULL, 1, 'now')"""
                )
                connection.execute(
                    """INSERT INTO specialists
                       (specialist_id, space_id, specialty, full_name, phone, user_id,
                        created_at, updated_at)
                       VALUES ('plumber', 'hoa', 'plumber', 'Петров Пётр', '+79990000000', 2,
                               'now', 'now')"""
                )
                connection.commit()

            created = store.create_request(
                1, "resident", "Заявка собственника", "Кран течёт, почините!",
                specialist_id="plumber", specialty="plumber", source="chat",
            )
            request_id = created["request_id"]
            self.assertEqual(created["status"], "review")
            assigned = store.list_specialist_requests(2, "+79990000000")
            self.assertEqual(assigned[0]["status"], "review")
            self.assertIsNone(assigned[0]["priority"])
            self.assertEqual(assigned[0]["address"], "Улица, 1")
            self.assertEqual(assigned[0]["specialist_id"], "plumber")
            self.assertFalse(store.set_specialist_request_priority(3, "+79990000000", request_id, "urgent"))
            with self.assertRaises(ValueError):
                store.set_specialist_request_controls(
                    2, "+79990000000", request_id, status="done", priority="urgent",
                )
            self.assertIsNone(store.list_specialist_requests(2, "+79990000000")[0]["priority"])
            self.assertTrue(store.set_specialist_request_controls(
                2, "+79990000000", request_id, status="in_progress", priority="urgent",
            ))
            self.assertTrue(store.set_specialist_request_status(2, "+79990000000", request_id, "done"))
            self.assertTrue(store.set_specialist_request_priority(2, "+79990000000", request_id, "planned"))
            self.assertEqual(store.list_requests(user_id=1)[0]["priority"], "planned")
            self.assertFalse(store.set_request_priority("other-hoa", request_id, "urgent"))
            self.assertTrue(store.set_request_controls(
                "hoa", request_id, status="done", priority="urgent",
            ))
            self.assertEqual(store.list_requests(user_id=1)[0]["priority"], "urgent")

            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    """INSERT INTO specialists
                       (specialist_id, space_id, specialty, full_name, phone, user_id,
                        created_at, updated_at)
                       VALUES ('electrician', 'hoa', 'electrician', 'Сидоров Сидор',
                               '+79990000001', 3, 'now', 'now')"""
                )
                connection.execute(
                    """INSERT INTO specialists
                       (specialist_id, space_id, specialty, full_name, phone, user_id,
                        created_at, updated_at)
                       VALUES ('pending', 'hoa', 'plumber', 'Не вошёл',
                               '+79990000002', NULL, 'now', 'now')"""
                )
                connection.commit()
            with self.assertRaises(ValueError):
                store.set_request_controls("hoa", request_id, specialist_id="unknown")
            with self.assertRaises(ValueError):
                store.set_request_controls("hoa", request_id, specialist_id="pending")
            before = store.get_request_notification_context("hoa", request_id)
            self.assertTrue(store.set_request_controls(
                "hoa", request_id, status="in_progress", specialist_id="electrician",
            ))
            after = store.get_request_notification_context("hoa", request_id)
            self.assertEqual(after["specialist_user_id"], 3)
            self.assertFalse(store.list_specialist_requests(2, "+79990000000"))
            self.assertEqual(store.list_specialist_requests(3, "+79990000001")[0]["request_id"], request_id)

            class Messages:
                def __init__(self) -> None:
                    self.sent: list[tuple[int, str]] = []

                def send_message(self, text: str, *, user_id: int) -> None:
                    self.sent.append((user_id, text))

            messages = Messages()
            self.assertEqual(notify_request_changes(messages, request_id, before, after), [])
            self.assertEqual({user_id for user_id, _ in messages.sent}, {1, 2, 3})
            self.assertIn("В процессе выполнения", messages.sent[0][1])
            self.assertIn("назначил вас исполнителем", messages.sent[2][1])

            chairman_request = store.create_request(
                1, "resident", "Заявка председателю", "Проблема для председателя",
                specialty="other", source="chat",
            )
            self.assertEqual(chairman_request["status"], "review")


if __name__ == "__main__":
    unittest.main()
