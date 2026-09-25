import unittest
import hashlib
import hmac
import tempfile
from pathlib import Path

from src.bot import HELP_TEXT, WELCOME_TEXT, handle_update, reply_target
from src.phone_verification import extract_phone_from_vcard, verify_contact_signature
from src.verification_store import PhoneVerificationStore


class FakeApi:
    def __init__(self, token: str = "test-token") -> None:
        self.token = token
        self.sent: list[tuple[str, dict]] = []

    def send_message(self, text: str, **options: object) -> dict:
        self.sent.append((text, options))
        return {}

    def verify_contact(self, vcf_info: str, signature: str) -> bool:
        return verify_contact_signature(self.token, vcf_info, signature)


class BotTests(unittest.TestCase):
    def test_started_event_sends_welcome(self) -> None:
        api = FakeApi()
        handle_update(api, {"update_type": "bot_started", "user": {"user_id": 42}})
        self.assertEqual(api.sent[0][0], WELCOME_TEXT)
        self.assertEqual(api.sent[0][1]["user_id"], 42)
        self.assertTrue(api.sent[0][1]["attachments"])

    def test_help_command_in_dialog(self) -> None:
        api = FakeApi()
        handle_update(
            api,
            {
                "update_type": "message_created",
                "message": {
                    "sender": {"user_id": 42},
                    "recipient": {"chat_type": "dialog", "user_id": 42},
                    "body": {"text": "/help"},
                },
            },
        )
        self.assertEqual(api.sent, [(HELP_TEXT, {"user_id": 42})])

    def test_group_message_uses_chat_id(self) -> None:
        update = {
            "message": {
                "sender": {"user_id": 42},
                "recipient": {"chat_type": "chat", "chat_id": 100},
            }
        }
        self.assertEqual(reply_target(update), {"chat_id": 100})

    def test_signed_contact_is_saved(self) -> None:
        token = "test-token"
        vcard = (
            "BEGIN:VCARD\r\nVERSION:3.0\r\n"
            "TEL;TYPE=cell:79991234567\r\nFN:Test User\r\nEND:VCARD\r\n"
        )
        signature = hmac.new(token.encode(), vcard.encode(), hashlib.sha256).hexdigest()
        update = {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": 42},
                "recipient": {"chat_type": "dialog", "user_id": 42},
                "body": {
                    "attachments": [
                        {
                            "type": "contact",
                            "payload": {
                                "vcf_info": vcard,
                                "hash": signature,
                            },
                        }
                    ]
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            store = PhoneVerificationStore(Path(directory) / "test.sqlite3")
            api = FakeApi(token)
            handle_update(api, update, store)

            verification = store.get(42)

        self.assertIsNotNone(verification)
        self.assertEqual(verification.phone, "+79991234567")
        self.assertIn("подтверждён", api.sent[0][0])

    def test_unsigned_contact_is_rejected(self) -> None:
        api = FakeApi()
        handle_update(
            api,
            {
                "update_type": "message_created",
                "message": {
                    "sender": {"user_id": 42},
                    "recipient": {"chat_type": "dialog", "user_id": 42},
                    "body": {
                        "attachments": [
                            {
                                "type": "contact",
                                "payload": {"vcf_info": "BEGIN:VCARD\r\nEND:VCARD\r\n"},
                            }
                        ]
                    },
                },
            },
        )
        self.assertIn("не подтверждён MAX", api.sent[0][0])

    def test_vcard_with_literal_newline_escapes_is_supported(self) -> None:
        token = "test-token"
        literal = "BEGIN:VCARD\\r\\nTEL:+79991234567\\r\\nEND:VCARD\\r\\n"
        normalized = "BEGIN:VCARD\r\nTEL:+79991234567\r\nEND:VCARD\r\n"
        signature = hmac.new(token.encode(), normalized.encode(), hashlib.sha256).hexdigest()

        self.assertTrue(verify_contact_signature(token, literal, signature))
        self.assertEqual(extract_phone_from_vcard(literal), "+79991234567")



if __name__ == "__main__":
    unittest.main()
