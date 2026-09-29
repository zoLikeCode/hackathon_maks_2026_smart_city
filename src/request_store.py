from __future__ import annotations

import logging
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Callable, Protocol
from uuid import uuid4


logger = logging.getLogger(__name__)


PRIORITIES = frozenset({"urgent", "high", "normal", "low"})
STATUSES = frozenset({"new", "in_progress", "waiting", "done"})


def validate_scheduled_for(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Дата должна быть в формате ГГГГ-ММ-ДД")
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Укажите существующую дату") from error
    return value


@dataclass(frozen=True)
class ServiceRequest:
    id: str
    title: str
    category: str
    description: str
    address: str
    created_at: str
    scheduled_for: str | None
    status: str
    priority: str
    assignee: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class RequestRepository(Protocol):
    def create(
        self,
        *,
        space_id: str,
        created_by_user_id: int,
        title: str,
        category: str,
        description: str,
        address: str,
        scheduled_for: str | None,
    ) -> ServiceRequest: ...

    def list_all(self) -> list[ServiceRequest]: ...

    def list_for_space(self, space_id: str) -> list[ServiceRequest]: ...

    def set_priority(self, request_id: str, priority: str) -> ServiceRequest | None: ...

    def set_status(self, request_id: str, status: str) -> ServiceRequest | None: ...

    def set_schedule(self, request_id: str, scheduled_for: str | None) -> ServiceRequest | None: ...


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS service_requests (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    created_by_user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    description TEXT NOT NULL,
    address TEXT NOT NULL,
    created_at TEXT NOT NULL,
    scheduled_for TEXT,
    status TEXT NOT NULL DEFAULT 'new'
        CHECK (status IN ('new', 'in_progress', 'waiting', 'done')),
    priority TEXT NOT NULL DEFAULT 'normal'
        CHECK (priority IN ('urgent', 'high', 'normal', 'low')),
    assignee TEXT
);
CREATE INDEX IF NOT EXISTS idx_service_requests_space_created
    ON service_requests (space_id, created_at DESC);
"""

_POSTGRES_APP_COLUMNS = {
    "id": "text",
    "space_id": "text",
    "created_by_user_id": "bigint",
    "title": "text",
    "category": "text",
    "description": "text",
    "address": "text",
    "created_at": "timestamp with time zone",
    "scheduled_for": "date",
    "status": "text",
    "priority": "text",
    "assignee": "text",
}
_POSTGRES_LEGACY_COLUMNS = {
    "request_id": "text",
    "space_id": "text",
    "resident_id": "text",
    "user_id": "bigint",
    "title": "text",
    "description": "text",
    "status": "text",
    "created_at": "text",
    "updated_at": "text",
    "priority": "text",
}
_POSTGRES_ISOLATED_TABLE = "mini_app_service_requests"
_POSTGRES_LEGACY_META_TABLE = "mini_app_legacy_request_meta"


def _postgres_schema(table_name: str) -> tuple[str, str]:
    if table_name not in {"service_requests", _POSTGRES_ISOLATED_TABLE}:
        raise ValueError("Неизвестная таблица заявок")
    index_name = f"idx_{table_name}_mini_app_space_created"
    return (
        f"""
    CREATE TABLE IF NOT EXISTS {table_name} (
        id TEXT PRIMARY KEY,
        space_id TEXT NOT NULL,
        created_by_user_id BIGINT NOT NULL,
        title TEXT NOT NULL,
        category TEXT NOT NULL,
        description TEXT NOT NULL,
        address TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        scheduled_for DATE,
        status TEXT NOT NULL DEFAULT 'new'
            CHECK (status IN ('new', 'in_progress', 'waiting', 'done')),
        priority TEXT NOT NULL DEFAULT 'normal'
            CHECK (priority IN ('urgent', 'high', 'normal', 'low')),
        assignee TEXT
    )
    """,
        f"""
    CREATE INDEX IF NOT EXISTS {index_name}
        ON {table_name} (space_id, created_at DESC)
    """,
    )


_POSTGRES_LEGACY_META_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {_POSTGRES_LEGACY_META_TABLE} (
    request_id TEXT PRIMARY KEY,
    scheduled_for DATE
)
"""

_COLUMNS = "id, title, category, description, address, created_at, scheduled_for, status, priority, assignee"


def _request_from_row(row: tuple[Any, ...] | None) -> ServiceRequest | None:
    if row is None:
        return None
    created_at = row[5].isoformat() if isinstance(row[5], datetime) else str(row[5])
    scheduled_for = row[6].isoformat() if isinstance(row[6], date) else row[6]
    return ServiceRequest(
        id=str(row[0]),
        title=str(row[1]),
        category=str(row[2]),
        description=str(row[3]),
        address=str(row[4]),
        created_at=created_at,
        scheduled_for=scheduled_for,
        status=str(row[7]),
        priority=str(row[8]),
        assignee=None if row[9] is None else str(row[9]),
    )


def _postgres_columns_match(
    columns: dict[str, tuple[str, bool, bool]], expected: dict[str, str]
) -> bool:
    if not columns or any(
        columns.get(name, (None, False, False))[0] != data_type
        for name, data_type in expected.items()
    ):
        return False
    return not any(
        name not in expected and required and not has_default
        for name, (_data_type, required, has_default) in columns.items()
    )


class SqliteRequestStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(SQLITE_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=10)
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def create(
        self,
        *,
        space_id: str,
        created_by_user_id: int,
        title: str,
        category: str,
        description: str,
        address: str,
        scheduled_for: str | None,
    ) -> ServiceRequest:
        scheduled_for = validate_scheduled_for(scheduled_for)
        request_id = str(uuid4())
        created_at = datetime.now(UTC).isoformat()
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO service_requests
                        (id, space_id, created_by_user_id, title, category, description,
                         address, created_at, scheduled_for)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id, space_id, created_by_user_id, title, category,
                        description, address, created_at, scheduled_for,
                    ),
                )
                row = connection.execute(
                    f"SELECT {_COLUMNS} FROM service_requests WHERE id = ?", (request_id,)
                ).fetchone()
        result = _request_from_row(row)
        assert result is not None
        return result

    def list_all(self) -> list[ServiceRequest]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT {_COLUMNS} FROM service_requests ORDER BY created_at DESC, id DESC"
            ).fetchall()
        return [_request_from_row(row) for row in rows if row is not None]

    def list_for_space(self, space_id: str) -> list[ServiceRequest]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT {_COLUMNS} FROM service_requests "
                "WHERE space_id = ? ORDER BY created_at DESC, id DESC",
                (space_id,),
            ).fetchall()
        return [_request_from_row(row) for row in rows if row is not None]

    def set_priority(self, request_id: str, priority: str) -> ServiceRequest | None:
        if not isinstance(priority, str) or priority not in PRIORITIES:
            raise ValueError("Неизвестный приоритет")
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    "UPDATE service_requests SET priority = ? WHERE id = ?",
                    (priority, request_id),
                )
                row = connection.execute(
                    f"SELECT {_COLUMNS} FROM service_requests WHERE id = ?", (request_id,)
                ).fetchone()
        return _request_from_row(row)

    def set_status(self, request_id: str, status: str) -> ServiceRequest | None:
        if not isinstance(status, str) or status not in STATUSES:
            raise ValueError("Неизвестный статус")
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    "UPDATE service_requests SET status = ? WHERE id = ?",
                    (status, request_id),
                )
                row = connection.execute(
                    f"SELECT {_COLUMNS} FROM service_requests WHERE id = ?", (request_id,)
                ).fetchone()
        return _request_from_row(row)

    def set_schedule(self, request_id: str, scheduled_for: str | None) -> ServiceRequest | None:
        scheduled_for = validate_scheduled_for(scheduled_for)
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    "UPDATE service_requests SET scheduled_for = ? WHERE id = ?",
                    (scheduled_for, request_id),
                )
                row = connection.execute(
                    f"SELECT {_COLUMNS} FROM service_requests WHERE id = ?", (request_id,)
                ).fetchone()
        return _request_from_row(row)


class PostgresRequestStore:
    def __init__(
        self,
        database_url: str,
        *,
        startup_timeout: float = 30.0,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        self._database_url = database_url
        self._connect_override = connect
        self._initialize(startup_timeout)

    def _connect(self) -> Any:
        if self._connect_override is not None:
            return self._connect_override(self._database_url)
        try:
            import psycopg
        except ImportError as error:
            raise RuntimeError("PostgreSQL requires psycopg") from error
        return psycopg.connect(self._database_url, connect_timeout=5)

    @staticmethod
    def _table_columns(cursor: Any, table_name: str) -> dict[str, tuple[str, bool, bool]]:
        cursor.execute(
            """
            SELECT a.attname, format_type(a.atttypid, a.atttypmod),
                   a.attnotnull, a.atthasdef
            FROM pg_attribute AS a
            WHERE a.attrelid = to_regclass(%s)
              AND a.attnum > 0 AND NOT a.attisdropped
            """,
            (table_name,),
        )
        return {
            str(name): (str(data_type), bool(required), bool(has_default))
            for name, data_type, required, has_default in cursor.fetchall()
        }

    def _initialize(self, startup_timeout: float) -> None:
        deadline = time.monotonic() + max(0.0, startup_timeout)
        while True:
            try:
                with self._connect() as connection:
                    with connection.cursor() as cursor:
                        main_columns = self._table_columns(cursor, "service_requests")
                        isolated_columns = self._table_columns(
                            cursor, _POSTGRES_ISOLATED_TABLE
                        )
                        if isolated_columns and not _postgres_columns_match(
                            isolated_columns, _POSTGRES_APP_COLUMNS
                        ):
                            raise RuntimeError(
                                "Схема mini_app_service_requests несовместима"
                            )

                        legacy_mode = _postgres_columns_match(
                            main_columns, _POSTGRES_LEGACY_COLUMNS
                        )
                        main_compatible = _postgres_columns_match(
                            main_columns, _POSTGRES_APP_COLUMNS
                        )
                        table_name = (
                            _POSTGRES_ISOLATED_TABLE
                            if isolated_columns or (main_columns and not main_compatible)
                            else "service_requests"
                        )
                        if main_columns and not main_compatible:
                            logger.info(
                                "Existing service_requests schema differs; using %s",
                                table_name,
                            )
                        for statement in _postgres_schema(table_name):
                            cursor.execute(statement)
                        if legacy_mode:
                            cursor.execute(_POSTGRES_LEGACY_META_SCHEMA)
                self._table_name = table_name
                self._legacy_mode = legacy_mode
                return
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)

    def create(
        self,
        *,
        space_id: str,
        created_by_user_id: int,
        title: str,
        category: str,
        description: str,
        address: str,
        scheduled_for: str | None,
    ) -> ServiceRequest:
        scheduled_for = validate_scheduled_for(scheduled_for)
        request_id = str(uuid4())
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    INSERT INTO {self._table_name}
                        (id, space_id, created_by_user_id, title, category, description,
                         address, created_at, scheduled_for)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING {_COLUMNS}
                    """,
                    (
                        request_id, space_id, created_by_user_id, title, category,
                        description, address, datetime.now(UTC),
                        date.fromisoformat(scheduled_for) if scheduled_for is not None else None,
                    ),
                )
                row = cursor.fetchone()
        result = _request_from_row(row)
        assert result is not None
        return result

    def _legacy_requests(
        self, cursor: Any, *, request_id: str | None = None, space_id: str | None = None
    ) -> list[ServiceRequest]:
        if not self._legacy_mode:
            return []
        clauses: list[str] = []
        parameters: list[str] = []
        if request_id is not None:
            clauses.append("r.request_id = %s")
            parameters.append(request_id)
        if space_id is not None:
            clauses.append("r.space_id = %s")
            parameters.append(space_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        cursor.execute(
            f"""
            SELECT r.request_id, r.title, 'Без категории', r.description,
                   COALESCE(NULLIF(s.address, ''), 'Адрес не указан'),
                   r.created_at, m.scheduled_for, r.status,
                   COALESCE(r.priority, 'normal'), NULL::TEXT
            FROM service_requests AS r
            LEFT JOIN hoa_spaces AS s ON s.space_id = r.space_id
            LEFT JOIN {_POSTGRES_LEGACY_META_TABLE} AS m
              ON m.request_id = r.request_id
            {where}
            ORDER BY r.created_at DESC, r.request_id DESC
            """,
            tuple(parameters),
        )
        return [
            item for row in cursor.fetchall()
            if (item := _request_from_row(row)) is not None
        ]

    @staticmethod
    def _sorted_requests(items: list[ServiceRequest]) -> list[ServiceRequest]:
        return sorted(items, key=lambda item: (item.created_at, item.id), reverse=True)

    def list_all(self) -> list[ServiceRequest]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT {_COLUMNS} FROM {self._table_name} "
                    "ORDER BY created_at DESC, id DESC"
                )
                rows = cursor.fetchall()
                legacy_items = self._legacy_requests(cursor)
        app_items = [item for row in rows if (item := _request_from_row(row)) is not None]
        return self._sorted_requests(app_items + legacy_items)

    def list_for_space(self, space_id: str) -> list[ServiceRequest]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT {_COLUMNS} FROM {self._table_name} "
                    "WHERE space_id = %s ORDER BY created_at DESC, id DESC",
                    (space_id,),
                )
                rows = cursor.fetchall()
                legacy_items = self._legacy_requests(cursor, space_id=space_id)
        app_items = [item for row in rows if (item := _request_from_row(row)) is not None]
        return self._sorted_requests(app_items + legacy_items)

    def set_priority(self, request_id: str, priority: str) -> ServiceRequest | None:
        if not isinstance(priority, str) or priority not in PRIORITIES:
            raise ValueError("Неизвестный приоритет")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE {self._table_name} SET priority = %s WHERE id = %s "
                    f"RETURNING {_COLUMNS}",
                    (priority, request_id),
                )
                row = cursor.fetchone()
                if row is None and self._legacy_mode:
                    cursor.execute(
                        "UPDATE service_requests SET priority = %s, updated_at = %s "
                        "WHERE request_id = %s RETURNING request_id",
                        (priority, datetime.now(UTC).isoformat(), request_id),
                    )
                    if cursor.fetchone() is not None:
                        return self._legacy_requests(cursor, request_id=request_id)[0]
        return _request_from_row(row)

    def set_status(self, request_id: str, status: str) -> ServiceRequest | None:
        if not isinstance(status, str) or status not in STATUSES:
            raise ValueError("Неизвестный статус")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE {self._table_name} SET status = %s WHERE id = %s "
                    f"RETURNING {_COLUMNS}",
                    (status, request_id),
                )
                row = cursor.fetchone()
                if row is None and self._legacy_mode:
                    cursor.execute(
                        "UPDATE service_requests SET status = %s, updated_at = %s "
                        "WHERE request_id = %s RETURNING request_id",
                        (status, datetime.now(UTC).isoformat(), request_id),
                    )
                    if cursor.fetchone() is not None:
                        return self._legacy_requests(cursor, request_id=request_id)[0]
        return _request_from_row(row)

    def set_schedule(self, request_id: str, scheduled_for: str | None) -> ServiceRequest | None:
        scheduled_for = validate_scheduled_for(scheduled_for)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE {self._table_name} SET scheduled_for = %s WHERE id = %s "
                    f"RETURNING {_COLUMNS}",
                    (date.fromisoformat(scheduled_for) if scheduled_for is not None else None,
                     request_id),
                )
                row = cursor.fetchone()
                if row is None and self._legacy_mode:
                    cursor.execute(
                        "SELECT 1 FROM service_requests WHERE request_id = %s",
                        (request_id,),
                    )
                    if cursor.fetchone() is not None:
                        cursor.execute(
                            f"""
                            INSERT INTO {_POSTGRES_LEGACY_META_TABLE}
                                (request_id, scheduled_for)
                            VALUES (%s, %s)
                            ON CONFLICT (request_id) DO UPDATE
                            SET scheduled_for = excluded.scheduled_for
                            """,
                            (request_id, date.fromisoformat(scheduled_for)
                             if scheduled_for is not None else None),
                        )
                        return self._legacy_requests(cursor, request_id=request_id)[0]
        return _request_from_row(row)


def create_request_store(database_url: str | None, sqlite_path: Path) -> RequestRepository:
    if database_url:
        return PostgresRequestStore(database_url)
    return SqliteRequestStore(sqlite_path)
