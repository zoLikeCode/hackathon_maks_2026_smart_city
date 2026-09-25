import tempfile
import unittest
from pathlib import Path

from src.auth_store import ProtocolAlreadyClaimedError, SqliteChairmanAuthorizationStore


class ChairmanAuthorizationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = SqliteChairmanAuthorizationStore(
            Path(self.directory.name) / "authorization.sqlite3"
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_confirmed_phone_can_link_a_new_max_user(self) -> None:
        self.store.approve_chairman(
            user_id=42,
            phone="+79991234567",
            full_name="Мещерякова Е.А.",
            space_id="hoa_test",
            hoa_name="ТСЖ «Некрасовское»",
            address="г. Сызрань, д. 38",
            protocol_sha256="abc123",
            protocol_filename="protocol.pdf",
        )

        profile = self.store.link_user_to_phone(84, "+79991234567")

        self.assertEqual(profile.user_id, 84)
        self.assertIsNone(self.store.get_profile_by_user_id(42))

    def test_protocol_cannot_be_claimed_by_another_phone(self) -> None:
        common = {
            "full_name": "Мещерякова Е.А.",
            "space_id": "hoa_test",
            "hoa_name": "ТСЖ «Некрасовское»",
            "address": "г. Сызрань, д. 38",
            "protocol_sha256": "abc123",
            "protocol_filename": "protocol.pdf",
        }
        self.store.approve_chairman(user_id=42, phone="+79991234567", **common)

        with self.assertRaises(ProtocolAlreadyClaimedError):
            self.store.approve_chairman(user_id=84, phone="+79997654321", **common)


if __name__ == "__main__":
    unittest.main()
