from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any

from src.verification_store import (
    PhoneVerificationStore,
    PostgresPhoneVerificationStore,
    create_phone_verification_store,
)


class FakeCursor:
    def __init__(self, database: dict[int, tuple[int, str, datetime]]) -> None:
        self.database = database
        self.row: tuple[int, str, datetime] | None = None

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, parameters: tuple[Any, ...] | None = None) -> None:
        normalized = " ".join(query.split()).upper()
        if normalized.startswith("CREATE TABLE"):
            return
        if normalized.startswith("INSERT INTO") and parameters is not None:
            user_id, phone, verified_at = parameters
            self.database[int(user_id)] = (int(user_id), str(phone), verified_at)
            return
        if normalized.startswith("SELECT") and parameters is not None:
            self.row = self.database.get(int(parameters[0]))

    def fetchone(self) -> tuple[int, str, datetime] | None:
        return self.row


class FakeConnection:
    def __init__(self, database: dict[int, tuple[int, str, datetime]]) -> None:
        self.database = database

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.database)


class VerificationStoreTests(unittest.TestCase):
    def test_factory_uses_sqlite_without_database_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = create_phone_verification_store(None, Path(directory) / "test.sqlite3")
        self.assertIsInstance(store, PhoneVerificationStore)

    def test_postgres_store_saves_and_reads_verification(self) -> None:
        database: dict[int, tuple[int, str, datetime]] = {}

        def connect(_database_url: str) -> FakeConnection:
            return FakeConnection(database)

        store = PostgresPhoneVerificationStore(
            "postgresql://example",
            startup_timeout=0,
            connect=connect,
        )
        saved = store.save(42, "+79991234567")
        loaded = store.get(42)

        self.assertEqual(saved.phone, "+79991234567")
        self.assertEqual(loaded, saved)


if __name__ == "__main__":
    unittest.main()
