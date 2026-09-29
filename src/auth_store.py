from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Protocol


class ProtocolAlreadyClaimedError(RuntimeError):
    """Протокол уже закреплён за другим подтверждённым номером."""


@dataclass(frozen=True)
class AuthorizationState:
    user_id: int
    role: str
    step: str
    updated_at: str


@dataclass(frozen=True)
class ChairmanProfile:
    user_id: int
    phone: str
    full_name: str
    space_id: str
    hoa_name: str
    address: str
    protocol_filename: str
    verified_at: str


class ChairmanAuthorizationRepository(Protocol):
    def get_state(self, user_id: int) -> AuthorizationState | None: ...

    def set_state(self, user_id: int, role: str, step: str) -> AuthorizationState: ...

    def clear_state(self, user_id: int) -> None: ...

    def get_profile_by_user_id(self, user_id: int) -> ChairmanProfile | None: ...

    def get_profile_by_space_id(self, space_id: str) -> ChairmanProfile | None: ...

    def get_profile_by_phone(self, phone: str) -> ChairmanProfile | None: ...

    def get_profile_by_protocol(self, document_sha256: str) -> ChairmanProfile | None: ...

    def link_user_to_phone(self, user_id: int, phone: str) -> ChairmanProfile | None: ...

    def record_review(
        self,
        *,
        document_sha256: str,
        user_id: int,
        phone: str,
        filename: str,
        status: str,
        analysis: dict[str, Any],
    ) -> None: ...

    def approve_chairman(
        self,
        *,
        user_id: int,
        phone: str,
        full_name: str,
        space_id: str,
        hoa_name: str,
        address: str,
        protocol_sha256: str,
        protocol_filename: str,
    ) -> ChairmanProfile: ...


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS authorization_states (
    user_id INTEGER PRIMARY KEY,
    role TEXT NOT NULL,
    step TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hoa_spaces (
    space_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    address TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chairman_profiles (
    phone TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL UNIQUE,
    full_name TEXT NOT NULL,
    space_id TEXT NOT NULL UNIQUE,
    protocol_sha256 TEXT NOT NULL UNIQUE,
    protocol_filename TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    FOREIGN KEY (space_id) REFERENCES hoa_spaces(space_id)
);

CREATE TABLE IF NOT EXISTS protocol_reviews (
    document_sha256 TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    phone TEXT NOT NULL,
    filename TEXT NOT NULL,
    status TEXT NOT NULL,
    analysis_json TEXT NOT NULL,
    reviewed_at TEXT NOT NULL
);
"""


POSTGRES_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS authorization_states (
        user_id BIGINT PRIMARY KEY,
        role TEXT NOT NULL,
        step TEXT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hoa_spaces (
        space_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        address TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chairman_profiles (
        phone TEXT PRIMARY KEY,
        user_id BIGINT NOT NULL UNIQUE,
        full_name TEXT NOT NULL,
        space_id TEXT NOT NULL UNIQUE REFERENCES hoa_spaces(space_id),
        protocol_sha256 TEXT NOT NULL UNIQUE,
        protocol_filename TEXT NOT NULL,
        verified_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS protocol_reviews (
        document_sha256 TEXT PRIMARY KEY,
        user_id BIGINT NOT NULL,
        phone TEXT NOT NULL,
        filename TEXT NOT NULL,
        status TEXT NOT NULL,
        analysis_json JSONB NOT NULL,
        reviewed_at TIMESTAMPTZ NOT NULL
    )
    """,
)


def _as_iso(value: Any) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _profile_from_row(row: tuple[Any, ...] | None) -> ChairmanProfile | None:
    if row is None:
        return None
    return ChairmanProfile(
        user_id=int(row[0]),
        phone=str(row[1]),
        full_name=str(row[2]),
        space_id=str(row[3]),
        hoa_name=str(row[4]),
        address=str(row[5]),
        protocol_filename=str(row[6]),
        verified_at=_as_iso(row[7]),
    )


class SqliteChairmanAuthorizationStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            with connection:
                connection.executescript(SQLITE_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def get_state(self, user_id: int) -> AuthorizationState | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT user_id, role, step, updated_at
                FROM authorization_states
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return AuthorizationState(int(row[0]), str(row[1]), str(row[2]), str(row[3]))

    def set_state(self, user_id: int, role: str, step: str) -> AuthorizationState:
        updated_at = datetime.now(UTC).isoformat()
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO authorization_states (user_id, role, step, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        role = excluded.role,
                        step = excluded.step,
                        updated_at = excluded.updated_at
                    """,
                    (user_id, role, step, updated_at),
                )
        return AuthorizationState(user_id, role, step, updated_at)

    def clear_state(self, user_id: int) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("DELETE FROM authorization_states WHERE user_id = ?", (user_id,))

    def _get_profile(self, where: str, value: Any) -> ChairmanProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                f"""
                SELECT p.user_id, p.phone, p.full_name, p.space_id, s.name, s.address,
                       p.protocol_filename, p.verified_at
                FROM chairman_profiles p
                JOIN hoa_spaces s ON s.space_id = p.space_id
                WHERE {where} = ?
                """,
                (value,),
            ).fetchone()
        return _profile_from_row(row)

    def get_profile_by_user_id(self, user_id: int) -> ChairmanProfile | None:
        return self._get_profile("p.user_id", user_id)

    def get_profile_by_space_id(self, space_id: str) -> ChairmanProfile | None:
        return self._get_profile("p.space_id", space_id)

    def get_profile_by_phone(self, phone: str) -> ChairmanProfile | None:
        return self._get_profile("p.phone", phone)

    def get_profile_by_protocol(self, document_sha256: str) -> ChairmanProfile | None:
        return self._get_profile("p.protocol_sha256", document_sha256)

    def link_user_to_phone(self, user_id: int, phone: str) -> ChairmanProfile | None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    "DELETE FROM chairman_profiles WHERE user_id = ? AND phone <> ?",
                    (user_id, phone),
                )
                connection.execute(
                    "UPDATE chairman_profiles SET user_id = ? WHERE phone = ?",
                    (user_id, phone),
                )
        return self.get_profile_by_phone(phone)

    def record_review(
        self,
        *,
        document_sha256: str,
        user_id: int,
        phone: str,
        filename: str,
        status: str,
        analysis: dict[str, Any],
    ) -> None:
        reviewed_at = datetime.now(UTC).isoformat()
        payload = json.dumps(analysis, ensure_ascii=False, sort_keys=True)
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO protocol_reviews
                        (document_sha256, user_id, phone, filename, status,
                         analysis_json, reviewed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(document_sha256) DO UPDATE SET
                        user_id = excluded.user_id,
                        phone = excluded.phone,
                        filename = excluded.filename,
                        status = excluded.status,
                        analysis_json = excluded.analysis_json,
                        reviewed_at = excluded.reviewed_at
                    """,
                    (document_sha256, user_id, phone, filename, status, payload, reviewed_at),
                )

    def approve_chairman(
        self,
        *,
        user_id: int,
        phone: str,
        full_name: str,
        space_id: str,
        hoa_name: str,
        address: str,
        protocol_sha256: str,
        protocol_filename: str,
    ) -> ChairmanProfile:
        verified_at = datetime.now(UTC).isoformat()
        with closing(self._connect()) as connection:
            with connection:
                claimed = connection.execute(
                    "SELECT phone FROM chairman_profiles WHERE protocol_sha256 = ?",
                    (protocol_sha256,),
                ).fetchone()
                if claimed is not None and str(claimed[0]) != phone:
                    raise ProtocolAlreadyClaimedError
                connection.execute(
                    """
                    INSERT INTO hoa_spaces (space_id, name, address, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(space_id) DO UPDATE SET
                        name = excluded.name,
                        address = excluded.address
                    """,
                    (space_id, hoa_name, address, verified_at),
                )
                connection.execute(
                    "DELETE FROM chairman_profiles WHERE space_id = ? AND phone <> ?",
                    (space_id, phone),
                )
                connection.execute(
                    "DELETE FROM chairman_profiles WHERE user_id = ? AND phone <> ?",
                    (user_id, phone),
                )
                connection.execute(
                    """
                    INSERT INTO chairman_profiles
                        (phone, user_id, full_name, space_id, protocol_sha256,
                         protocol_filename, verified_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(phone) DO UPDATE SET
                        user_id = excluded.user_id,
                        full_name = excluded.full_name,
                        space_id = excluded.space_id,
                        protocol_sha256 = excluded.protocol_sha256,
                        protocol_filename = excluded.protocol_filename,
                        verified_at = excluded.verified_at
                    """,
                    (
                        phone,
                        user_id,
                        full_name,
                        space_id,
                        protocol_sha256,
                        protocol_filename,
                        verified_at,
                    ),
                )
        profile = self.get_profile_by_phone(phone)
        if profile is None:
            raise RuntimeError("Не удалось создать профиль председателя")
        return profile


class PostgresChairmanAuthorizationStore:
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

    def get_state(self, user_id: int) -> AuthorizationState | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT user_id, role, step, updated_at
                    FROM authorization_states
                    WHERE user_id = %s
                    """,
                    (user_id,),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return AuthorizationState(int(row[0]), str(row[1]), str(row[2]), _as_iso(row[3]))

    def set_state(self, user_id: int, role: str, step: str) -> AuthorizationState:
        updated_at = datetime.now(UTC)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO authorization_states (user_id, role, step, updated_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT(user_id) DO UPDATE SET
                        role = excluded.role,
                        step = excluded.step,
                        updated_at = excluded.updated_at
                    """,
                    (user_id, role, step, updated_at),
                )
        return AuthorizationState(user_id, role, step, updated_at.isoformat())

    def clear_state(self, user_id: int) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM authorization_states WHERE user_id = %s", (user_id,))

    def _get_profile(self, where: str, value: Any) -> ChairmanProfile | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT p.user_id, p.phone, p.full_name, p.space_id, s.name, s.address,
                           p.protocol_filename, p.verified_at
                    FROM chairman_profiles p
                    JOIN hoa_spaces s ON s.space_id = p.space_id
                    WHERE {where} = %s
                    """,
                    (value,),
                )
                row = cursor.fetchone()
        return _profile_from_row(row)

    def get_profile_by_user_id(self, user_id: int) -> ChairmanProfile | None:
        return self._get_profile("p.user_id", user_id)

    def get_profile_by_space_id(self, space_id: str) -> ChairmanProfile | None:
        return self._get_profile("p.space_id", space_id)

    def get_profile_by_phone(self, phone: str) -> ChairmanProfile | None:
        return self._get_profile("p.phone", phone)

    def get_profile_by_protocol(self, document_sha256: str) -> ChairmanProfile | None:
        return self._get_profile("p.protocol_sha256", document_sha256)

    def link_user_to_phone(self, user_id: int, phone: str) -> ChairmanProfile | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM chairman_profiles WHERE user_id = %s AND phone <> %s",
                    (user_id, phone),
                )
                cursor.execute(
                    "UPDATE chairman_profiles SET user_id = %s WHERE phone = %s",
                    (user_id, phone),
                )
        return self.get_profile_by_phone(phone)

    def record_review(
        self,
        *,
        document_sha256: str,
        user_id: int,
        phone: str,
        filename: str,
        status: str,
        analysis: dict[str, Any],
    ) -> None:
        reviewed_at = datetime.now(UTC)
        payload = json.dumps(analysis, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO protocol_reviews
                        (document_sha256, user_id, phone, filename, status,
                         analysis_json, reviewed_at)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
                    ON CONFLICT(document_sha256) DO UPDATE SET
                        user_id = excluded.user_id,
                        phone = excluded.phone,
                        filename = excluded.filename,
                        status = excluded.status,
                        analysis_json = excluded.analysis_json,
                        reviewed_at = excluded.reviewed_at
                    """,
                    (document_sha256, user_id, phone, filename, status, payload, reviewed_at),
                )

    def approve_chairman(
        self,
        *,
        user_id: int,
        phone: str,
        full_name: str,
        space_id: str,
        hoa_name: str,
        address: str,
        protocol_sha256: str,
        protocol_filename: str,
    ) -> ChairmanProfile:
        verified_at = datetime.now(UTC)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT phone FROM chairman_profiles WHERE protocol_sha256 = %s FOR UPDATE",
                    (protocol_sha256,),
                )
                claimed = cursor.fetchone()
                if claimed is not None and str(claimed[0]) != phone:
                    raise ProtocolAlreadyClaimedError
                cursor.execute(
                    """
                    INSERT INTO hoa_spaces (space_id, name, address, created_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT(space_id) DO UPDATE SET
                        name = excluded.name,
                        address = excluded.address
                    """,
                    (space_id, hoa_name, address, verified_at),
                )
                cursor.execute(
                    "DELETE FROM chairman_profiles WHERE space_id = %s AND phone <> %s",
                    (space_id, phone),
                )
                cursor.execute(
                    "DELETE FROM chairman_profiles WHERE user_id = %s AND phone <> %s",
                    (user_id, phone),
                )
                cursor.execute(
                    """
                    INSERT INTO chairman_profiles
                        (phone, user_id, full_name, space_id, protocol_sha256,
                         protocol_filename, verified_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(phone) DO UPDATE SET
                        user_id = excluded.user_id,
                        full_name = excluded.full_name,
                        space_id = excluded.space_id,
                        protocol_sha256 = excluded.protocol_sha256,
                        protocol_filename = excluded.protocol_filename,
                        verified_at = excluded.verified_at
                    """,
                    (
                        phone,
                        user_id,
                        full_name,
                        space_id,
                        protocol_sha256,
                        protocol_filename,
                        verified_at,
                    ),
                )
        profile = self.get_profile_by_phone(phone)
        if profile is None:
            raise RuntimeError("Не удалось создать профиль председателя")
        return profile


def create_chairman_authorization_store(
    database_url: str | None,
    sqlite_path: Path,
) -> ChairmanAuthorizationRepository:
    if database_url:
        return PostgresChairmanAuthorizationStore(database_url)
    return SqliteChairmanAuthorizationStore(sqlite_path)
