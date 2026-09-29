from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.bot import send_pending_visit_approvals
from src.hoa_store import HoaStore
from src.max_api import MaxApiError
from src.request_notifications import notify_request_changes


class SpecialistScheduleTests(unittest.TestCase):
    def test_emergency_blocks_new_slots_and_notifies_chairman_until_done(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hoa.sqlite3"
            store = HoaStore(None, path)
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE hoa_spaces (space_id TEXT PRIMARY KEY, name TEXT, address TEXT)")
                connection.execute("INSERT INTO hoa_spaces VALUES ('hoa', 'ТСЖ', 'г. Самара, ул. Ленина, 1')")
                connection.execute("""INSERT INTO residents
                    (resident_id, space_id, unit, full_name, area, share, ownership,
                     code_prefix, code_secret, user_id, active, updated_at)
                    VALUES ('resident', 'hoa', '1', 'Иванов Иван', '40', '1', 'собственность',
                            'prefix', 'secret', 1, 1, 'now')""")
                connection.commit()
            specialist = store.create_specialist("hoa", "plumber", "Петров Пётр", "+79990000000")
            specialist_id = specialist["specialist_id"]
            store.claim_specialist_phone(2, "+79990000000")
            store.update_specialist(
                "hoa", specialist_id, specialty="plumber", full_name="Петров Пётр",
                phone="+79990000000", hours=[
                    {"weekday": day, "start_hour": 9, "end_hour": 12} for day in range(7)
                ],
            )
            emergency_id = store.create_request(
                1, "resident", "Прорвало трубу", "Вода в подъезде",
                specialist_id=specialist_id, specialty="plumber", source="chat",
            )["request_id"]
            other_id = store.create_request(
                1, "resident", "Починить кран", "Течёт кран",
                specialist_id=specialist_id, specialty="plumber", source="chat",
            )["request_id"]
            pending = store.request_visit_access(2, "+79990000000", other_id)
            before = store.get_request_notification_context("hoa", emergency_id)
            self.assertTrue(store.set_specialist_request_controls(
                2, "+79990000000", emergency_id,
                status="in_progress", priority="emergency",
            ))
            after = store.get_request_notification_context("hoa", emergency_id)

            class Messages:
                def __init__(self) -> None:
                    self.sent: list[tuple[int, str]] = []

                def send_message(self, text: str, *, user_id: int) -> None:
                    self.sent.append((user_id, text))

            messages = Messages()
            self.assertEqual(notify_request_changes(
                messages, emergency_id, before, after, chairman_user_id=3,
            ), ["Чат ТСЖ ещё не подключён; уведомление об аварии не отправлено."])
            chairman_messages = [text for user, text in messages.sent if user == 3]
            self.assertEqual(len(chairman_messages), 1)
            self.assertIn("Авария", chairman_messages[0])
            self.assertTrue(store.list_specialist_requests(2, "+79990000000")[0]["emergency_busy"])
            day = (datetime.now(ZoneInfo("Europe/Samara")).date() + timedelta(days=1)).isoformat()
            with self.assertRaisesRegex(ValueError, "устраняет аварию"):
                store.request_visit_access(2, "+79990000000", other_id)
            with self.assertRaisesRegex(ValueError, "устраняет аварию"):
                store.owner_visit_days(1, other_id, pending["token"], 0)
            with self.assertRaisesRegex(ValueError, "устраняет аварию"):
                store.confirm_owner_visit(1, other_id, pending["token"], day, 10)
            with self.assertRaisesRegex(ValueError, "до исполнения"):
                store.set_specialist_request_priority(2, "+79990000000", emergency_id, "planned")
            self.assertTrue(store.set_specialist_request_status(
                2, "+79990000000", emergency_id, "done",
            ))
            self.assertFalse(store.list_specialist_requests(2, "+79990000000")[0]["emergency_busy"])
            self.assertIn(day, store.owner_visit_days(1, other_id, pending["token"], 0)["dates"])

    def test_chairman_edit_hours_and_owner_visit_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hoa.sqlite3"
            store = HoaStore(None, path)
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE hoa_spaces (space_id TEXT PRIMARY KEY, name TEXT, address TEXT)")
                connection.execute("INSERT INTO hoa_spaces VALUES ('hoa', 'ТСЖ', 'г. Самара, ул. Ленина, 1')")
                connection.execute("""INSERT INTO residents
                    (resident_id, space_id, unit, full_name, area, share, ownership,
                     code_prefix, code_secret, user_id, active, updated_at)
                    VALUES ('resident', 'hoa', '1', 'Иванов Иван', '40', '1', 'собственность',
                            'prefix', 'secret', 1, 1, 'now')""")
                connection.commit()
            created = store.create_specialist("hoa", "plumber", "Петров Пётр", "+79990000000")
            specialist_id = created["specialist_id"]
            store.claim_specialist_phone(2, "+79990000000")
            day = datetime.now(ZoneInfo("Europe/Samara")).date() + timedelta(days=1)
            hours = [{"weekday": weekday, "start_hour": 9, "end_hour": 12}
                     for weekday in range(7)]
            updated = store.update_specialist("hoa", specialist_id, specialty="plumber",
                                              full_name="Петров Пётр Иванович", phone="+79990000000",
                                              hours=hours)
            self.assertTrue(updated["linked"])
            self.assertEqual(updated["work_hours"], hours)
            request_id = store.create_request(1, "resident", "Течёт кран", "Описание",
                                              specialist_id=specialist_id, specialty="plumber",
                                              source="chat")["request_id"]
            access = store.request_visit_access(2, "+79990000000", request_id)
            self.assertEqual(access["timezone"], "Europe/Samara")
            self.assertEqual(store.list_requests(user_id=1)[0]["visit_status"], "awaiting_owner")
            self.assertIsNone(store.owner_visit_days(3, request_id, access["token"], 0))
            renewed = store.request_visit_access(2, "+79990000000", request_id)
            self.assertIsNone(store.owner_visit_days(1, request_id, access["token"], 0))
            access = renewed
            self.assertIn(day.isoformat(), store.owner_visit_days(1, request_id, access["token"], 0)["dates"])
            later = store.owner_visit_days(1, request_id, access["token"], 1)
            self.assertEqual(len(later["dates"]), 3)
            self.assertFalse(later["has_next"])
            with self.assertRaises(ValueError):
                store.owner_visit_days(1, request_id, access["token"], 2)
            outside = (datetime.now(ZoneInfo("Europe/Samara")).date() + timedelta(days=10)).isoformat()
            self.assertEqual(store.owner_visit_hours(1, request_id, access["token"], outside)["hours"], [])
            with self.assertRaises(ValueError):
                store.confirm_owner_visit(1, request_id, access["token"], outside, 10)
            options = store.owner_visit_hours(1, request_id, access["token"], day.isoformat())
            self.assertEqual(options["hours"], [9, 10, 11])
            with self.assertRaises(ValueError):
                store.confirm_owner_visit(1, request_id, access["token"], day.isoformat(), 12)
            self.assertTrue(store.confirm_owner_visit(1, request_id, access["token"], day.isoformat(), 10)["accepted"])
            self.assertIsNone(store.confirm_owner_visit(1, request_id, access["token"], day.isoformat(), 11))
            self.assertEqual(store.list_requests(user_id=1)[0]["visit_status"], "accepted")
            self.assertEqual(store.pending_visit_approval_notices()[0]["specialist_user_id"], 2)

            class Messages:
                failed = True

                def __init__(self) -> None:
                    self.sent: list[tuple[int, str]] = []

                def send_message(self, text: str, *, user_id: int) -> None:
                    if self.failed:
                        raise MaxApiError("temporary failure")
                    self.sent.append((user_id, text))

            messages = Messages()
            with self.assertLogs("src.bot", level="ERROR"):
                send_pending_visit_approvals(messages, store)
            self.assertEqual(len(store.pending_visit_approval_notices()), 1)
            messages.failed = False
            send_pending_visit_approvals(messages, store)
            self.assertEqual(store.pending_visit_approval_notices(), [])
            self.assertEqual(messages.sent[0][0], 2)
            self.assertIn("разрешил вам войти в квартиру", messages.sent[0][1])
            second_id = store.create_request(1, "resident", "Вторая заявка", "Описание",
                                             specialist_id=specialist_id, specialty="plumber",
                                             source="chat")["request_id"]
            second = store.request_visit_access(2, "+79990000000", second_id)
            self.assertEqual(store.owner_visit_hours(1, second_id, second["token"], day.isoformat())["hours"], [9, 11])
            self.assertFalse(store.decline_owner_access(1, second_id, second["token"])["accepted"])
            changed = store.update_specialist("hoa", specialist_id, specialty="plumber",
                                              full_name="Петров Пётр Иванович", phone="+79990000001",
                                              hours=hours)
            self.assertFalse(changed["linked"])
            self.assertFalse(store.specialist_memberships(2, "+79990000000"))


if __name__ == "__main__":
    unittest.main()
