from __future__ import annotations

import sqlite3
import re
import time
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Callable, Protocol
from uuid import uuid4


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

POSTGRES_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS service_requests (
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
    """
    CREATE INDEX IF NOT EXISTS idx_service_requests_space_created
        ON service_requests (space_id, created_at DESC)
    """,
)

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

    def _initialize(self, startup_timeout: float) -> None:
        deadline = time.monotonic() + max(0.0, startup_timeout)
        while True:
            try:
                with self._connect() as connection:
                    with connection.cursor() as cursor:
                        for statement in POSTGRES_SCHEMA:
                            cursor.execute(statement)
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
                    INSERT INTO service_requests
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

    def list_all(self) -> list[ServiceRequest]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT {_COLUMNS} FROM service_requests ORDER BY created_at DESC, id DESC"
                )
                rows = cursor.fetchall()
        return [_request_from_row(row) for row in rows if row is not None]

    def list_for_space(self, space_id: str) -> list[ServiceRequest]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT {_COLUMNS} FROM service_requests "
                    "WHERE space_id = %s ORDER BY created_at DESC, id DESC",
                    (space_id,),
                )
                rows = cursor.fetchall()
        return [_request_from_row(row) for row in rows if row is not None]

    def set_priority(self, request_id: str, priority: str) -> ServiceRequest | None:
        if not isinstance(priority, str) or priority not in PRIORITIES:
            raise ValueError("Неизвестный приоритет")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE service_requests SET priority = %s WHERE id = %s "
                    f"RETURNING {_COLUMNS}",
                    (priority, request_id),
                )
                row = cursor.fetchone()
        return _request_from_row(row)

    def set_status(self, request_id: str, status: str) -> ServiceRequest | None:
        if not isinstance(status, str) or status not in STATUSES:
            raise ValueError("Неизвестный статус")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE service_requests SET status = %s WHERE id = %s "
                    f"RETURNING {_COLUMNS}",
                    (status, request_id),
                )
                row = cursor.fetchone()
        return _request_from_row(row)

    def set_schedule(self, request_id: str, scheduled_for: str | None) -> ServiceRequest | None:
        scheduled_for = validate_scheduled_for(scheduled_for)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE service_requests SET scheduled_for = %s WHERE id = %s "
                    f"RETURNING {_COLUMNS}",
                    (date.fromisoformat(scheduled_for) if scheduled_for is not None else None,
                     request_id),
                )
                row = cursor.fetchone()
        return _request_from_row(row)


def create_request_store(database_url: str | None, sqlite_path: Path) -> RequestRepository:
    if database_url:
        return PostgresRequestStore(database_url)
    return SqliteRequestStore(sqlite_path)
