from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.bot import check_pending_group_chats, handle_update
from src.hoa_store import HoaStore


class GroupChatTests(unittest.TestCase):
    def test_only_registered_max_chat_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = HoaStore(None, Path(directory) / "hoa.sqlite3")
            saved = store.set_group_chat_link("hoa", "https://www.max.ru/join/official/")
            self.assertEqual(saved["link"], "https://max.ru/join/official")
            with self.assertRaises(ValueError):
                store.set_group_chat_link("other", "https://max.ru/join/official")
            with self.assertRaises(ValueError):
                store.set_group_chat_link("hoa", "https://example.com/join/official")

            class Api:
                def __init__(self) -> None:
                    self.chats = {
                        10: {"type": "chat", "status": "active", "link": "https://max.ru/join/official", "title": "Дом"},
                        11: {"type": "chat", "status": "active", "link": "https://max.ru/join/other", "title": "Чужой"},
                        12: {"type": "channel", "status": "active", "link": "https://max.ru/join/official", "title": "Канал"},
                        13: {"type": "chat", "status": "active", "link": "https://max.ru/join/unknown", "title": "Новый"},
                    }
                    self.left: list[int] = []

                def get_chat(self, chat_id: int) -> dict:
                    return self.chats[chat_id]

                def leave_chat(self, chat_id: int) -> None:
                    self.left.append(chat_id)

            api = Api()
            for chat_id in (10, 11, 12):
                handle_update(api, {"update_type": "bot_added", "chat_id": chat_id}, hoa_store=store)
            check_pending_group_chats(api, store)
            self.assertTrue(store.is_group_chat_authorized(10))
            self.assertEqual(store.get_group_chat("hoa")["title"], "Дом")
            self.assertEqual(api.left, [11, 12])
            self.assertEqual(store.pending_group_chat_checks(), [])

            handle_update(api, {"update_type": "message_created", "message": {
                "recipient": {"chat_type": "chat", "chat_id": 13},
                "sender": {"user_id": 2}, "body": {"text": "/ping"},
            }}, hoa_store=store)
            check_pending_group_chats(api, store)
            self.assertEqual(api.left, [11, 12, 13])

            store.set_group_chat_link("hoa", "https://max.ru/join/replacement")
            self.assertFalse(store.is_group_chat_authorized(10))
            check_pending_group_chats(api, store)
            self.assertEqual(api.left, [11, 12, 13, 10])


if __name__ == "__main__":
    unittest.main()
