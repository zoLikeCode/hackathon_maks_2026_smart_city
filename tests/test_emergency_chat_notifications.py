from __future__ import annotations

import unittest

from src.max_api import MaxApiError
from src.request_notifications import notify_request_changes


class Messages:
    def __init__(self, fail_chat: bool = False) -> None:
        self.sent: list[tuple[int | None, int | None, str]] = []
        self.fail_chat = fail_chat

    def send_message(
        self, text: str, *, user_id: int | None = None, chat_id: int | None = None,
    ) -> None:
        if chat_id is not None and self.fail_chat:
            raise MaxApiError("Чат недоступен")
        self.sent.append((user_id, chat_id, text))


def request_context(status: str, priority: str) -> dict:
    return {
        "status": status,
        "priority": priority,
        "owner_user_id": 101,
        "hoa_name": "ТСЖ Тест",
        "unit": "42",
        "title": "Авария в квартире",
        "specialist_id": "specialist",
        "specialist_user_id": 202,
    }


class EmergencyChatNotificationTests(unittest.TestCase):
    def test_announces_emergency_and_each_status_change_without_private_details(self) -> None:
        api = Messages()
        before = request_context("review", "planned")
        active = request_context("in_progress", "emergency")
        done = request_context("done", "emergency")

        self.assertEqual(notify_request_changes(
            api, "12345678-full", before, active,
            chairman_user_id=303, group_chat_id=404,
        ), [])
        self.assertEqual(notify_request_changes(
            api, "12345678-full", active, done,
            chairman_user_id=303, group_chat_id=404,
        ), [])
        chat_messages = [text for _, chat_id, text in api.sent if chat_id is not None]
        self.assertEqual(len(chat_messages), 2)
        self.assertIn("Статус: В процессе выполнения", chat_messages[0])
        self.assertIn("устранена", chat_messages[1])
        for text in chat_messages:
            self.assertIn("12345678", text)
            self.assertNotIn("42", text)
            self.assertNotIn("Авария в квартире", text)
        self.assertEqual({chat_id for _, chat_id, _ in api.sent if chat_id is not None}, {404})

    def test_chat_delivery_failure_is_reported_without_losing_other_notifications(self) -> None:
        api = Messages(fail_chat=True)
        warnings = notify_request_changes(
            api, "12345678-full", request_context("review", "emergency"),
            request_context("done", "emergency"), group_chat_id=404,
        )
        self.assertEqual(warnings, ["Не удалось отправить уведомление об аварии в чат ТСЖ."])
        self.assertEqual(len([1 for user_id, _, _ in api.sent if user_id == 101]), 1)


if __name__ == "__main__":
    unittest.main()
