import unittest
import hashlib
import hmac
import tempfile
from pathlib import Path

from src.auth_store import SqliteChairmanAuthorizationStore
from src.bot import HELP_TEXT, WELCOME_TEXT, handle_update, reply_target
from src.hoa_store import HoaStore
from src.owner_registry import ResidentEntry
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

    def test_signed_special_contact_unlocks_role_buttons_for_same_max_user(self) -> None:
        token = "test-token"
        vcard = (
            "BEGIN:VCARD\r\nVERSION:3.0\r\n"
            "TEL;TYPE=cell:79872660500\r\nFN:Test User\r\nEND:VCARD\r\n"
        )
        signature = hmac.new(token.encode(), vcard.encode(), hashlib.sha256).hexdigest()
        update = {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": 42},
                "recipient": {"chat_type": "dialog", "user_id": 42},
                "body": {
                    "attachments": [{
                        "type": "contact",
                        "payload": {"vcf_info": vcard, "hash": signature},
                    }],
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            store = PhoneVerificationStore(Path(directory) / "test.sqlite3")
            api = FakeApi(token)
            handle_update(api, update, store, bot_username="smart_city_bot")
            self.assertEqual(store.get(42).phone, "+79872660500")
            buttons = api.sent[0][1]["attachments"][0]["payload"]["buttons"]
            self.assertEqual(
                [(row[0]["text"], row[0]["payload"]) for row in buttons],
                [
                    ("Специалист", "role_specialist"),
                    ("Председатель", "role_chairman"),
                    ("Собственник", "role_owner"),
                    ("Привязать помещение", "auth:owner:link"),
                ],
            )
            self.assertTrue(all(row[0]["type"] == "open_app" for row in buttons[:3]))
            self.assertEqual(buttons[3][0]["type"], "callback")

            restarted = FakeApi(token)
            handle_update(
                restarted,
                {"update_type": "bot_started", "user": {"user_id": 42}},
                store,
                bot_username="smart_city_bot",
            )
            self.assertEqual(restarted.sent[0][1]["attachments"], api.sent[0][1]["attachments"])

            stranger = FakeApi(token)
            handle_update(
                stranger,
                {"update_type": "bot_started", "user": {"user_id": 43}},
                store,
                bot_username="smart_city_bot",
            )
            stranger_buttons = stranger.sent[0][1]["attachments"][0]["payload"]["buttons"]
            self.assertTrue(all(row[0]["type"] == "callback" for row in stranger_buttons))

    def test_second_signed_admin_contact_gets_all_roles_before_specialist_access(self) -> None:
        token = "test-token"
        vcard = (
            "BEGIN:VCARD\r\nVERSION:3.0\r\n"
            "TEL;TYPE=cell:79969433497\r\nFN:Second Admin\r\nEND:VCARD\r\n"
        )
        signature = hmac.new(token.encode(), vcard.encode(), hashlib.sha256).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            store = PhoneVerificationStore(Path(directory) / "test.sqlite3")
            api = FakeApi(token)
            handle_update(
                api,
                {
                    "update_type": "message_created",
                    "message": {
                        "sender": {"user_id": 44},
                        "recipient": {"chat_type": "dialog", "user_id": 44},
                        "body": {"attachments": [{
                            "type": "contact",
                            "payload": {"vcf_info": vcard, "hash": signature},
                        }]},
                    },
                },
                store,
                bot_username="smart_city_bot",
            )
            self.assertEqual(store.get(44).phone, "+79969433497")
            expected = api.sent[0][1]["attachments"]
            self.assertEqual(
                [row[0]["payload"] for row in expected[0]["payload"]["buttons"]],
                ["role_specialist", "role_chairman", "role_owner", "auth:owner:link"],
            )

            for command in ("/start", "/auth", "/profile", "/status"):
                with self.subTest(command=command):
                    restarted = FakeApi(token)
                    handle_update(
                        restarted,
                        {
                            "update_type": "message_created",
                            "message": {
                                "sender": {"user_id": 44},
                                "recipient": {"chat_type": "dialog", "user_id": 44},
                                "body": {"text": command},
                            },
                        },
                        store,
                        bot_username="smart_city_bot",
                    )
                    self.assertEqual(restarted.sent[0][1]["attachments"], expected)

            started = FakeApi(token)
            handle_update(
                started,
                {"update_type": "bot_started", "user": {"user_id": 44}},
                store,
                bot_username="smart_city_bot",
            )
            self.assertEqual(started.sent[0][1]["attachments"], expected)

    def test_signed_admin_0500_auth_request_and_owner_link(self) -> None:
        token = "test-token"
        vcard = (
            "BEGIN:VCARD\r\nVERSION:3.0\r\n"
            "TEL;TYPE=cell:79872660500\r\nFN:Admin\r\nEND:VCARD\r\n"
        )
        signature = hmac.new(token.encode(), vcard.encode(), hashlib.sha256).hexdigest()
        user_id = 42
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.sqlite3"
            phone_store = PhoneVerificationStore(database)
            auth_store = SqliteChairmanAuthorizationStore(database)
            hoa_store = HoaStore(None, database)
            api = FakeApi(token)
            handle_update(
                api,
                {
                    "update_type": "message_created",
                    "message": {
                        "sender": {"user_id": user_id},
                        "recipient": {"chat_type": "dialog", "user_id": user_id},
                        "body": {"attachments": [{
                            "type": "contact", "payload": {"vcf_info": vcard, "hash": signature},
                        }]},
                    },
                },
                phone_store, auth_store, bot_username="smart_city_bot", hoa_store=hoa_store,
            )
            self.assertEqual(phone_store.get(user_id).phone, "+79872660500")

            api.sent.clear()
            handle_update(
                api,
                {
                    "update_type": "message_created",
                    "message": {
                        "sender": {"user_id": user_id},
                        "recipient": {"chat_type": "dialog", "user_id": user_id},
                        "body": {"text": "/auth"},
                    },
                },
                phone_store, auth_store, bot_username="smart_city_bot", hoa_store=hoa_store,
            )
            self.assertEqual(len(api.sent), 1)
            auth_buttons = api.sent[0][1]["attachments"][0]["payload"]["buttons"]
            self.assertEqual(
                (auth_buttons[3][0]["text"], auth_buttons[3][0]["payload"]),
                ("Привязать помещение", "auth:owner:link"),
            )

            api.sent.clear()
            handle_update(
                api,
                {
                    "update_type": "message_created",
                    "message": {
                        "sender": {"user_id": user_id},
                        "recipient": {"chat_type": "dialog", "user_id": user_id},
                        "body": {"text": "/auth"},
                    },
                },
                phone_store, auth_store, bot_username=None, hoa_store=hoa_store,
            )
            self.assertIn("публичное имя", api.sent[0][0])
            self.assertEqual(api.sent[0][1]["attachments"][0]["payload"]["buttons"], [[{
                "type": "callback", "text": "Привязать помещение", "payload": "auth:owner:link",
            }]])

            api.sent.clear()
            handle_update(
                api,
                {
                    "update_type": "message_created",
                    "message": {
                        "sender": {"user_id": user_id},
                        "recipient": {"chat_type": "dialog", "user_id": user_id},
                        "body": {"text": "/request"},
                    },
                },
                phone_store, auth_store, bot_username="smart_city_bot", hoa_store=hoa_store,
            )
            self.assertEqual(len(api.sent), 1)
            self.assertIn("демонстрацион", api.sent[0][0].lower())
            self.assertIn("/code КОД", api.sent[0][0])

            api.sent.clear()
            handle_update(
                api,
                {
                    "update_type": "message_callback",
                    "callback": {"user": {"user_id": user_id}, "payload": "auth:owner:link"},
                },
                phone_store, auth_store, bot_username="smart_city_bot", hoa_store=hoa_store,
            )
            self.assertEqual(len(api.sent), 1)
            self.assertIn("/code КОД", api.sent[0][0])
            state = auth_store.get_state(user_id)
            self.assertIsNotNone(state)
            self.assertEqual((state.role, state.step), ("owner", "awaiting_code"))

            auth_store.approve_chairman(
                user_id=99, phone="+79990000099", full_name="Председатель",
                space_id="hoa-1", hoa_name="ТСЖ Тест", address="ул. Тестовая, 1",
                protocol_sha256="test-protocol", protocol_filename="protocol.pdf",
            )
            hoa_store.import_registry(
                "hoa-1", "registry.pdf", b"test-registry",
                [ResidentEntry("1", "Иванов Иван", "40", "1", "собственность")], [],
            )
            code = hoa_store.list_residents("hoa-1")[0]["code"]
            api.sent.clear()
            handle_update(
                api,
                {
                    "update_type": "message_created",
                    "message": {
                        "sender": {"user_id": user_id},
                        "recipient": {"chat_type": "dialog", "user_id": user_id},
                        "body": {"text": f"/code {code}"},
                    },
                },
                phone_store, auth_store, bot_username="smart_city_bot", hoa_store=hoa_store,
            )
            self.assertIn("Вход собственника подтверждён", api.sent[0][0])
            memberships = hoa_store.memberships(user_id)
            self.assertEqual(len(memberships), 1)
            self.assertIsNone(auth_store.get_state(user_id))

            api.sent.clear()
            handle_update(
                api,
                {
                    "update_type": "message_created",
                    "message": {
                        "sender": {"user_id": user_id},
                        "recipient": {"chat_type": "dialog", "user_id": user_id},
                        "body": {"text": "/request"},
                    },
                },
                phone_store, auth_store, bot_username="smart_city_bot", hoa_store=hoa_store,
            )
            self.assertIn("Отправьте описание проблемы", api.sent[0][0])
            draft = hoa_store.get_quick_draft(user_id)
            self.assertEqual(draft["stage"], "awaiting_description")
            self.assertEqual(draft["resident_id"], memberships[0]["resident_id"])

    def test_unsigned_special_contact_does_not_unlock_role_buttons(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PhoneVerificationStore(Path(directory) / "test.sqlite3")
            api = FakeApi()
            handle_update(
                api,
                {
                    "update_type": "message_created",
                    "message": {
                        "sender": {"user_id": 42},
                        "recipient": {"chat_type": "dialog", "user_id": 42},
                        "body": {"attachments": [{
                            "type": "contact",
                            "payload": {
                                "vcf_info": "BEGIN:VCARD\r\nTEL:79872660500\r\nEND:VCARD\r\n",
                                "hash": "invalid",
                            },
                        }]},
                    },
                },
                store,
                bot_username="smart_city_bot",
            )
            self.assertIsNone(store.get(42))
            self.assertFalse(api.sent[0][1].get("attachments"))

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

    def test_recording_role_replaces_admin_menu_until_roles_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.sqlite3"
            phones = PhoneVerificationStore(path)
            auth = SqliteChairmanAuthorizationStore(path)
            phones.save(42, "+79872660500")
            auth.set_state(42, "owner", "recording_demo_awaiting_phone")

            def send(command: str) -> FakeApi:
                api = FakeApi()
                handle_update(
                    api,
                    {"update_type": "message_created", "message": {
                        "sender": {"user_id": 42},
                        "recipient": {"chat_type": "dialog", "user_id": 42},
                        "body": {"text": command},
                    }},
                    phones,
                    auth,
                    bot_username="smart_city_bot",
                )
                return api

            def signed_contact(phone: str) -> FakeApi:
                token = "test-token"
                vcard = (
                    "BEGIN:VCARD\r\nVERSION:3.0\r\n"
                    f"TEL;TYPE=cell:{phone.lstrip('+')}\r\nFN:Test User\r\nEND:VCARD\r\n"
                )
                signature = hmac.new(token.encode(), vcard.encode(), hashlib.sha256).hexdigest()
                api = FakeApi(token)
                handle_update(
                    api,
                    {"update_type": "message_created", "message": {
                        "sender": {"user_id": 42},
                        "recipient": {"chat_type": "dialog", "user_id": 42},
                        "body": {"attachments": [{
                            "type": "contact", "payload": {"vcf_info": vcard, "hash": signature},
                        }]},
                    }},
                    phones,
                    auth,
                    bot_username="smart_city_bot",
                )
                return api

            def buttons(api: FakeApi) -> list[list[dict]]:
                return api.sent[0][1]["attachments"][0]["payload"]["buttons"]

            self.assertEqual(buttons(send("/auth"))[0][0]["type"], "request_contact")
            self.assertEqual(buttons(send("/request"))[0][0]["type"], "request_contact")
            old_request = FakeApi()
            handle_update(
                old_request,
                {"update_type": "message_callback", "callback": {
                    "user": {"user_id": 42}, "payload": "request:new",
                }},
                phones,
                auth,
                bot_username="smart_city_bot",
            )
            self.assertEqual(buttons(old_request)[0][0]["type"], "request_contact")
            wrong = signed_contact("+79991234567")
            self.assertIn("0500", wrong.sent[0][0])
            self.assertEqual(phones.get(42).phone, "+79872660500")
            self.assertEqual(auth.get_state(42).step, "recording_demo_awaiting_phone")

            owner_buttons = buttons(signed_contact("+79872660500"))
            self.assertEqual([row[0]["payload"] for row in owner_buttons],
                             ["role_owner", "request:new"])
            self.assertEqual(auth.get_state(42).step, "recording_demo")
            self.assertEqual([row[0]["payload"] for row in buttons(send("/auth"))],
                             ["role_owner", "request:new"])
            self.assertEqual([row[0]["payload"] for row in buttons(signed_contact("+79872660500"))],
                             ["role_owner", "request:new"])

            for role, payload in (("specialist", "role_specialist"),
                                  ("chairman", "role_chairman")):
                auth.set_state(42, role, "recording_demo_awaiting_phone")
                self.assertEqual(buttons(send("/auth"))[0][0]["type"], "request_contact")
                self.assertEqual([row[0]["payload"] for row in buttons(signed_contact("+79872660500"))],
                                 [payload])

            all_roles = buttons(send("/roles"))
            self.assertEqual([row[0]["payload"] for row in all_roles[:3]],
                             ["role_specialist", "role_chairman", "role_owner"])
            self.assertIsNone(auth.get_state(42))



if __name__ == "__main__":
    unittest.main()
