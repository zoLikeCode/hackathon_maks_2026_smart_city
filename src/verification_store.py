from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Protocol


@dataclass(frozen=True)
class PhoneVerification:
    user_id: int
    phone: str
    verified_at: str


class PhoneVerificationRepository(Protocol):
    def save(self, user_id: int, phone: str) -> PhoneVerification: ...

    def get(self, user_id: int) -> PhoneVerification | None: ...


class PhoneVerificationStore:
    """SQLite-хранилище для локального запуска без Docker."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS phone_verifications (
                        user_id INTEGER PRIMARY KEY,
                        phone TEXT NOT NULL,
                        verified_at TEXT NOT NULL
                    )
                    """
                )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def save(self, user_id: int, phone: str) -> PhoneVerification:
        verified_at = datetime.now(UTC).isoformat()
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO phone_verifications (user_id, phone, verified_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        phone = excluded.phone,
                        verified_at = excluded.verified_at
                    """,
                    (user_id, phone, verified_at),
                )
        return PhoneVerification(user_id=user_id, phone=phone, verified_at=verified_at)

    def get(self, user_id: int) -> PhoneVerification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT user_id, phone, verified_at
                FROM phone_verifications
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return PhoneVerification(user_id=int(row[0]), phone=str(row[1]), verified_at=str(row[2]))


class PostgresPhoneVerificationStore:
    """Хранилище подтверждений в PostgreSQL с автоматическим созданием схемы."""

    def __init__(
        self,
        database_url: str,
        *,
        startup_timeout: float = 30.0,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgreSQL storage")
        self._database_url = database_url
        self._connect_override = connect
        self._initialize(startup_timeout)

    def _connect(self) -> Any:
        if self._connect_override is not None:
            return self._connect_override(self._database_url)
        try:
            import psycopg
        except ImportError as error:
            raise RuntimeError(
                "PostgreSQL requires psycopg. Install project dependencies with 'pip install .'."
            ) from error
        return psycopg.connect(self._database_url, connect_timeout=5)

    def _initialize(self, startup_timeout: float) -> None:
        deadline = time.monotonic() + max(0.0, startup_timeout)
        while True:
            try:
                with self._connect() as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """
                            CREATE TABLE IF NOT EXISTS phone_verifications (
                                user_id BIGINT PRIMARY KEY,
                                phone TEXT NOT NULL,
                                verified_at TIMESTAMPTZ NOT NULL
                            )
                            """
                        )
                return
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)

    def save(self, user_id: int, phone: str) -> PhoneVerification:
        verified_at = datetime.now(UTC)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO phone_verifications (user_id, phone, verified_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT(user_id) DO UPDATE SET
                        phone = excluded.phone,
                        verified_at = excluded.verified_at
                    """,
                    (user_id, phone, verified_at),
                )
        return PhoneVerification(
            user_id=user_id,
            phone=phone,
            verified_at=verified_at.isoformat(),
        )

    def get(self, user_id: int) -> PhoneVerification | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT user_id, phone, verified_at
                    FROM phone_verifications
                    WHERE user_id = %s
                    """,
                    (user_id,),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        verified_at = row[2]
        if isinstance(verified_at, datetime):
            verified_at = verified_at.isoformat()
        return PhoneVerification(
            user_id=int(row[0]),
            phone=str(row[1]),
            verified_at=str(verified_at),
        )


def create_phone_verification_store(
    database_url: str | None,
    sqlite_path: Path,
) -> PhoneVerificationRepository:
    if database_url:
        return PostgresPhoneVerificationStore(database_url)
    return PhoneVerificationStore(sqlite_path)
