"""PostgreSQL request-store regression tests for an existing bot database.

These tests use a small SQL recorder because CI does not provide a PostgreSQL
server. The production database already has a different `service_requests`
table, and the mini app must be able to use it without changing its schema.
"""

from __future__ import annotations

import re
import unittest
from datetime import UTC, date, datetime
from typing import Any

from src.request_store import PostgresRequestStore


APP_COLUMNS = (
    ("id", "text", True, False),
    ("space_id", "text", True, False),
    ("created_by_user_id", "bigint", True, False),
    ("title", "text", True, False),
    ("category", "text", True, False),
    ("description", "text", True, False),
    ("address", "text", True, False),
    ("created_at", "timestamp with time zone", True, False),
    ("scheduled_for", "date", False, False),
    ("status", "text", True, True),
    ("priority", "text", True, True),
    ("assignee", "text", False, False),
)

LEGACY_COLUMNS = (
    ("request_id", "text", True, False),
    ("space_id", "text", False, False),
    ("resident_id", "text", False, False),
    ("user_id", "bigint", False, False),
    ("title", "text", False, False),
    ("description", "text", False, False),
    ("status", "text", False, False),
    ("created_at", "text", False, False),
    ("updated_at", "text", False, False),
    ("priority", "text", False, False),
)


class FakeDatabase:
    def __init__(self, *, legacy: bool = False, compatible: bool = False) -> None:
        self.schemas: dict[str, tuple[tuple[Any, ...], ...]] = {}
        if legacy:
            self.schemas["service_requests"] = LEGACY_COLUMNS
        elif compatible:
            self.schemas["service_requests"] = APP_COLUMNS
        self.statements: list[tuple[str, tuple[Any, ...] | None]] = []
        self.rows: dict[str, list[tuple[Any, ...]]] = {
            "service_requests": [],
            "mini_app_service_requests": [],
        }
        self.app_space_by_id: dict[str, str] = {}
        self.legacy_rows: list[dict[str, Any]] = []
        self.addresses = {"hoa-1": "ул. Мира, 1", "hoa-2": "ул. Садовая, 2"}
        self.schedules: dict[str, date | None] = {}
        self.legacy = legacy

    def connect(self, _url: str) -> FakeConnection:
        return FakeConnection(self)


class FakeConnection:
    def __init__(self, database: FakeDatabase) -> None:
        self.database = database

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.database)


class FakeCursor:
    def __init__(self, database: FakeDatabase) -> None:
        self.database = database
        self.result: list[tuple[Any, ...]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params: tuple[Any, ...] | None = None) -> None:
        normalized = " ".join(query.split())
        upper = normalized.upper()
        self.database.statements.append((normalized, params))
        self.result = []
        if "PG_ATTRIBUTE" in upper:
            assert params is not None
            self.result = list(self.database.schemas.get(str(params[0]), ()))
            return
        match = re.match(r'CREATE TABLE IF NOT EXISTS ["]?(\w+)', normalized, re.I)
        if match:
            name = match.group(1)
            if name in {"service_requests", "mini_app_service_requests"}:
                self.database.schemas.setdefault(name, APP_COLUMNS)
            else:
                self.database.schemas.setdefault(name, ())
            return
        if upper.startswith("CREATE INDEX"):
            if self.database.legacy and " ON SERVICE_REQUESTS " in f" {upper} ":
                raise AssertionError("must not add an index to the legacy table")
            return
        if upper.startswith("SELECT 1 FROM SERVICE_REQUESTS WHERE REQUEST_ID"):
            assert params is not None
            self.result = [(1,)] if any(
                row["request_id"] == params[0] for row in self.database.legacy_rows
            ) else []
            return
        if upper.startswith("SELECT") and "FROM SERVICE_REQUESTS AS R" in upper:
            assert params is not None
            rows = self.database.legacy_rows
            if "R.REQUEST_ID = %S" in upper:
                rows = [row for row in rows if row["request_id"] == params[0]]
            if "R.SPACE_ID = %S" in upper:
                space_id = params[-1]
                rows = [row for row in rows if row["space_id"] == space_id]
            self.result = [
                (
                    row["request_id"], row["title"], "Без категории",
                    row["description"],
                    self.database.addresses.get(row["space_id"], "Адрес не указан"),
                    row["created_at"], self.database.schedules.get(row["request_id"]),
                    row["status"], row["priority"] or "normal", None,
                )
                for row in rows
            ]
            return
        if upper.startswith("SELECT") and "FROM MINI_APP_SERVICE_REQUESTS" in upper:
            self.result = self._app_rows("mini_app_service_requests", upper, params)
            return
        if upper.startswith("SELECT") and "FROM SERVICE_REQUESTS" in upper:
            self.result = self._app_rows("service_requests", upper, params)
            return
        if upper.startswith("INSERT INTO MINI_APP_LEGACY_REQUEST_META"):
            assert params is not None
            self.database.schedules[str(params[0])] = params[1]
            return
        if upper.startswith("INSERT INTO"):
            match = re.match(r'INSERT INTO ["]?(\w+)', normalized, re.I)
            assert match is not None
            table = match.group(1)
            if table == "mini_app_service_requests" or (
                table == "service_requests" and not self.database.legacy
            ):
                assert params is not None
                row = (
                    params[0], params[3], params[4], params[5], params[6],
                    params[7], params[8], "new", "normal", None,
                )
                self.database.rows[table].append(row)
                self.database.app_space_by_id[str(params[0])] = str(params[1])
                self.result = [row]
                return
            raise AssertionError(f"unexpected insert into {table}")
        if upper.startswith("UPDATE"):
            assert params is not None
            if upper.startswith("UPDATE SERVICE_REQUESTS SET") and "WHERE REQUEST_ID" in upper:
                request_id = str(params[-1])
                for row in self.database.legacy_rows:
                    if row["request_id"] == request_id:
                        field = "priority" if "SET PRIORITY" in upper else "status"
                        row[field] = params[0]
                        row["updated_at"] = params[1]
                        self.result = [(request_id,)]
                        break
                return
            match = re.match(r"UPDATE (\w+) SET (\w+) = %S WHERE ID = %S", upper)
            if match:
                table = match.group(1).lower()
                field = match.group(2).lower()
                index = {"scheduled_for": 6, "status": 7, "priority": 8}[field]
                for position, row in enumerate(self.database.rows[table]):
                    if row[0] == params[1]:
                        updated = list(row)
                        updated[index] = params[0]
                        self.database.rows[table][position] = tuple(updated)
                        self.result = [tuple(updated)]
                        break
                return
            return
        raise AssertionError(f"unexpected SQL: {normalized}")

    def _app_rows(
        self, table: str, query: str, params: tuple[Any, ...] | None
    ) -> list[tuple[Any, ...]]:
        rows = list(self.database.rows[table])
        if "WHERE SPACE_ID = %S" in query:
            assert params is not None
            rows = [
                row for row in rows
                if self.database.app_space_by_id.get(str(row[0])) == params[0]
            ]
        return rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.result[0] if self.result else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self.result)


class PostgresLegacySchemaTests(unittest.TestCase):
    @staticmethod
    def legacy_database() -> FakeDatabase:
        database = FakeDatabase(legacy=True)
        database.legacy_rows = [
            {
                "request_id": "a" * 32,
                "space_id": "hoa-1",
                "title": "Сломан лифт",
                "description": "Лифт стоит",
                "created_at": "2026-09-29T10:00:00+00:00",
                "updated_at": "2026-09-29T10:00:00+00:00",
                "status": "in_progress",
                "priority": "planned",
            },
            {
                "request_id": "b" * 32,
                "space_id": "hoa-1",
                "title": "Нет света",
                "description": "Темно в подъезде",
                "created_at": "2026-09-29T11:00:00+00:00",
                "updated_at": "2026-09-29T11:00:00+00:00",
                "status": "rejected",
                "priority": None,
            },
            {
                "request_id": "c" * 32,
                "space_id": "hoa-2",
                "title": "Уборка",
                "description": "Нужна уборка",
                "created_at": "2026-09-29T12:00:00+00:00",
                "updated_at": "2026-09-29T12:00:00+00:00",
                "status": "in_progress",
                "priority": "normal",
            },
        ]
        return database

    def test_legacy_table_is_preserved_and_new_requests_use_dedicated_table(self) -> None:
        database = self.legacy_database()
        store = PostgresRequestStore(
            "postgresql://example", startup_timeout=0, connect=database.connect
        )

        self.assertIn("mini_app_service_requests", database.schemas)
        self.assertIn("mini_app_legacy_request_meta", database.schemas)
        self.assertEqual(database.schemas["service_requests"], LEGACY_COLUMNS)
        created = store.create(
            space_id="hoa-1",
            created_by_user_id=42,
            title="Новая заявка",
            category="Лифт",
            description="Не работает",
            address="ул. Мира, 1",
            scheduled_for=None,
        )
        self.assertEqual(created.title, "Новая заявка")
        self.assertEqual(len(store.list_all()), 4)
        self.assertEqual(len(store.list_for_space("hoa-1")), 3)
        self.assertTrue(any(
            sql.upper().startswith("INSERT INTO MINI_APP_SERVICE_REQUESTS")
            for sql, _ in database.statements
        ))
        self.assertFalse(any(
            sql.upper().startswith("ALTER TABLE SERVICE_REQUESTS")
            or sql.upper().startswith("INSERT INTO SERVICE_REQUESTS")
            for sql, _ in database.statements
        ))

    def test_legacy_requests_are_visible_with_address_and_safe_defaults(self) -> None:
        database = self.legacy_database()
        store = PostgresRequestStore(
            "postgresql://example", startup_timeout=0, connect=database.connect
        )

        items = {item.id: item for item in store.list_all()}
        self.assertEqual(len(items), 3)
        self.assertEqual(items["a" * 32].category, "Без категории")
        self.assertEqual(items["a" * 32].address, "ул. Мира, 1")
        self.assertEqual(items["a" * 32].status, "in_progress")
        self.assertEqual(items["a" * 32].priority, "planned")
        self.assertEqual(items["b" * 32].status, "rejected")
        self.assertEqual(items["b" * 32].priority, "normal")
        self.assertEqual(items["c" * 32].address, "ул. Садовая, 2")
        self.assertEqual(
            {item.id for item in store.list_for_space("hoa-1")},
            {"a" * 32, "b" * 32},
        )

    def test_legacy_priority_status_and_schedule_updates_are_persisted(self) -> None:
        database = self.legacy_database()
        store = PostgresRequestStore(
            "postgresql://example", startup_timeout=0, connect=database.connect
        )
        request_id = "a" * 32

        changed = store.set_priority(request_id, "urgent")
        self.assertIsNotNone(changed)
        self.assertEqual(changed.priority, "urgent")
        self.assertEqual(database.legacy_rows[0]["priority"], "urgent")
        changed = store.set_status(request_id, "done")
        self.assertIsNotNone(changed)
        self.assertEqual(changed.status, "done")
        self.assertEqual(database.legacy_rows[0]["status"], "done")
        changed = store.set_schedule(request_id, "2026-10-05")
        self.assertIsNotNone(changed)
        self.assertEqual(changed.scheduled_for, "2026-10-05")
        self.assertEqual(database.schedules[request_id], date(2026, 10, 5))
        self.assertEqual(store.list_for_space("hoa-1")[1].scheduled_for, "2026-10-05")
        self.assertIsNone(store.set_schedule(request_id, None).scheduled_for)
        self.assertIsNone(database.schedules[request_id])
        self.assertEqual(database.legacy_rows[0]["title"], "Сломан лифт")

    def test_compatible_table_reuses_existing_requests(self) -> None:
        database = FakeDatabase(compatible=True)
        existing = (
            "existing-id", "Текущая заявка", "Лифт", "Описание",
            "ул. Мира, 1",
            datetime(2026, 9, 29, tzinfo=UTC), None, "new", "normal", None,
        )
        database.rows["service_requests"].append(existing)
        database.app_space_by_id["existing-id"] = "hoa-1"
        store = PostgresRequestStore(
            "postgresql://example", startup_timeout=0, connect=database.connect
        )

        self.assertNotIn("mini_app_service_requests", database.schemas)
        self.assertEqual([item.id for item in store.list_all()], ["existing-id"])
        self.assertEqual([item.id for item in store.list_for_space("hoa-1")], ["existing-id"])
        self.assertFalse(any(
            "FROM MINI_APP_SERVICE_REQUESTS" in sql.upper()
            for sql, _ in database.statements
        ))

    def test_fresh_database_creates_main_request_table(self) -> None:
        database = FakeDatabase()
        store = PostgresRequestStore(
            "postgresql://example", startup_timeout=0, connect=database.connect
        )

        self.assertIn("service_requests", database.schemas)
        self.assertNotIn("mini_app_service_requests", database.schemas)
        self.assertEqual(store.list_all(), [])


if __name__ == "__main__":
    unittest.main()
