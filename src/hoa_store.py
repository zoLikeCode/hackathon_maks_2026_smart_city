from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from src.owner_registry import ResidentEntry
from src.phone_verification import normalize_phone_input


class CodeError(ValueError):
    pass


TIMEZONE_LABELS = {
    "Europe/Kaliningrad": "Калининград, UTC+2", "Europe/Moscow": "Москва, UTC+3",
    "Europe/Samara": "Самара, UTC+4", "Asia/Yekaterinburg": "Екатеринбург, UTC+5",
    "Asia/Omsk": "Омск, UTC+6", "Asia/Krasnoyarsk": "Красноярск, UTC+7",
    "Asia/Irkutsk": "Иркутск, UTC+8", "Asia/Yakutsk": "Якутск, UTC+9",
    "Asia/Vladivostok": "Владивосток, UTC+10", "Asia/Magadan": "Магадан, UTC+11",
    "Asia/Kamchatka": "Камчатка, UTC+12",
}
VISIT_WINDOW_DAYS = 10


def normalize_group_chat_link(value: str) -> str:
    link = value.strip()
    if not link or len(link) > 1024 or any(char.isspace() for char in link):
        raise ValueError("Укажите ссылку на чат MAX")
    try:
        parsed = urlsplit(link)
        valid_host = parsed.hostname in {"max.ru", "www.max.ru"}
        valid_port = parsed.port is None
    except ValueError as error:
        raise ValueError("Укажите корректную ссылку на чат MAX") from error
    if (parsed.scheme != "https" or not valid_host or not valid_port
            or parsed.username or parsed.password or parsed.fragment
            or not parsed.path.strip("/")):
        raise ValueError("Нужна HTTPS-ссылка на групповой чат max.ru")
    return urlunsplit(("https", "max.ru", parsed.path.rstrip("/"), parsed.query, ""))


def timezone_label(timezone: str) -> str:
    return TIMEZONE_LABELS.get(timezone, timezone)


SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS residents (
    resident_id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    unit TEXT NOT NULL,
    full_name TEXT NOT NULL,
    area TEXT NOT NULL,
    share TEXT NOT NULL,
    ownership TEXT NOT NULL,
    code_prefix TEXT NOT NULL UNIQUE,
    code_secret TEXT NOT NULL,
    user_id INTEGER,
    phone TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    UNIQUE(space_id, unit, full_name)
);
CREATE INDEX IF NOT EXISTS residents_by_user ON residents(user_id, active);
CREATE TABLE IF NOT EXISTS specialists (
    specialist_id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    specialty TEXT NOT NULL,
    full_name TEXT NOT NULL,
    phone TEXT NOT NULL,
    user_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(space_id, phone)
);
CREATE INDEX IF NOT EXISTS specialists_by_phone ON specialists(phone);
CREATE TABLE IF NOT EXISTS specialist_hours (
    specialist_id TEXT NOT NULL,
    weekday INTEGER NOT NULL,
    start_hour INTEGER NOT NULL,
    end_hour INTEGER NOT NULL,
    PRIMARY KEY (specialist_id, weekday)
);
CREATE TABLE IF NOT EXISTS hoa_timezones (
    space_id TEXT PRIMARY KEY,
    timezone TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hoa_group_chats (
    space_id TEXT PRIMARY KEY,
    link TEXT NOT NULL UNIQUE,
    chat_id BIGINT UNIQUE,
    title TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_group_chat_checks (
    chat_id BIGINT PRIMARY KEY,
    observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS visit_proposals (
    request_id TEXT PRIMARY KEY,
    specialist_id TEXT NOT NULL,
    visit_date TEXT NOT NULL,
    visit_hour INTEGER NOT NULL,
    timezone TEXT NOT NULL,
    token TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    responded_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS active_visit_slot
ON visit_proposals(specialist_id, visit_date, visit_hour)
WHERE status IN ('pending', 'accepted');
CREATE TABLE IF NOT EXISTS visit_approval_notices (
    request_id TEXT PRIMARY KEY,
    specialist_user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    notified_at TEXT
);
CREATE TABLE IF NOT EXISTS registry_imports (
    import_id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    document_sha256 TEXT NOT NULL,
    resident_count INTEGER NOT NULL,
    skipped_units TEXT NOT NULL,
    uploaded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS code_attempts (
    user_id INTEGER PRIMARY KEY,
    window_number INTEGER NOT NULL,
    attempts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS service_requests (
    request_id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    resident_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL,
    priority TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS requests_by_user ON service_requests(user_id, created_at);
CREATE INDEX IF NOT EXISTS requests_by_space ON service_requests(space_id, created_at);
CREATE TABLE IF NOT EXISTS request_assignments (
    request_id TEXT PRIMARY KEY,
    target TEXT NOT NULL,
    specialty TEXT NOT NULL,
    specialist_id TEXT,
    source TEXT NOT NULL,
    assigned_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS assignments_by_specialist ON request_assignments(specialist_id);
CREATE TABLE IF NOT EXISTS quick_request_drafts (
    user_id INTEGER PRIMARY KEY,
    resident_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    specialty TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS draft_request_photos (
    user_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    mime_type TEXT NOT NULL,
    content BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, position)
);
CREATE TABLE IF NOT EXISTS request_photos (
    request_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    mime_type TEXT NOT NULL,
    content BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (request_id, position)
);
"""

SCHEMA_POSTGRES = tuple(
    part.strip()
    for part in SCHEMA_SQLITE.replace("user_id INTEGER", "user_id BIGINT")
    .replace("window_number INTEGER", "window_number BIGINT")
    .replace("updated_at REAL", "updated_at DOUBLE PRECISION")
    .replace("content BLOB", "content BYTEA")
    .split(";")
    if part.strip()
)


class HoaStore:
    def __init__(self, database_url: str | None, sqlite_path: Path) -> None:
        self._database_url = database_url
        self._sqlite_path = sqlite_path
        if database_url:
            import psycopg

            self._connect = lambda: psycopg.connect(database_url, connect_timeout=5)
            with self._session() as connection:
                with connection.cursor() as cursor:
                    for statement in SCHEMA_POSTGRES:
                        cursor.execute(statement)
                    cursor.execute("ALTER TABLE service_requests ADD COLUMN IF NOT EXISTS priority TEXT")
                    cursor.execute(
                        """UPDATE service_requests SET status = 'review'
                           WHERE status = 'new'"""
                    )
        else:
            sqlite_path.parent.mkdir(parents=True, exist_ok=True)

            def connect() -> sqlite3.Connection:
                connection = sqlite3.connect(sqlite_path, timeout=10)
                connection.execute("PRAGMA busy_timeout = 10000")
                return connection

            self._connect = connect
            with self._session() as connection:
                connection.executescript(SCHEMA_SQLITE)
                columns = {row[1] for row in connection.execute("PRAGMA table_info(service_requests)")}
                if "priority" not in columns:
                    connection.execute("ALTER TABLE service_requests ADD COLUMN priority TEXT")
                connection.execute(
                    """UPDATE service_requests SET status = 'review'
                       WHERE status = 'new'"""
                )

    @contextmanager
    def _session(self) -> Iterator[Any]:
        with closing(self._connect()) as connection:
            with connection:
                yield connection

    def _sql(self, text: str) -> str:
        return text.replace("?", "%s") if self._database_url else text

    def _execute(self, connection: Any, query: str, params: tuple[Any, ...] = ()) -> Any:
        cursor = connection.cursor()
        cursor.execute(self._sql(query), params)
        return cursor

    def get_group_chat(self, space_id: str) -> dict[str, Any] | None:
        with self._session() as connection:
            row = self._execute(connection, """SELECT link, chat_id, title FROM hoa_group_chats
                WHERE space_id = ?""", (space_id,)).fetchone()
        return dict(zip(("link", "chat_id", "title"), row)) if row else None

    def set_group_chat_link(self, space_id: str, link: str) -> dict[str, Any] | None:
        normalized = normalize_group_chat_link(link) if link.strip() else ""
        with self._session() as connection:
            old = self._execute(connection, """SELECT link, chat_id, title FROM hoa_group_chats
                WHERE space_id = ?""", (space_id,)).fetchone()
            if old and old[0] == normalized:
                return dict(zip(("link", "chat_id", "title"), old))
            if normalized:
                duplicate = self._execute(connection, """SELECT space_id FROM hoa_group_chats
                    WHERE link = ? AND space_id <> ?""", (normalized, space_id)).fetchone()
                if duplicate:
                    raise ValueError("Этот чат уже закреплён за другим ТСЖ")
            if old and old[1] is not None:
                self._execute(connection, """INSERT INTO pending_group_chat_checks (chat_id, observed_at)
                    VALUES (?, ?) ON CONFLICT(chat_id) DO NOTHING""",
                    (old[1], datetime.now(UTC).isoformat()))
            if normalized:
                self._execute(connection, """INSERT INTO hoa_group_chats
                    (space_id, link, chat_id, title, updated_at) VALUES (?, ?, NULL, NULL, ?)
                    ON CONFLICT(space_id) DO UPDATE SET link = excluded.link,
                    chat_id = NULL, title = NULL, updated_at = excluded.updated_at""",
                    (space_id, normalized, datetime.now(UTC).isoformat()))
            else:
                self._execute(connection, "DELETE FROM hoa_group_chats WHERE space_id = ?", (space_id,))
        return self.get_group_chat(space_id)

    def queue_group_chat_check(self, chat_id: int) -> None:
        with self._session() as connection:
            self._execute(connection, """INSERT INTO pending_group_chat_checks (chat_id, observed_at)
                VALUES (?, ?) ON CONFLICT(chat_id) DO NOTHING""",
                (chat_id, datetime.now(UTC).isoformat()))

    def pending_group_chat_checks(self) -> list[int]:
        with self._session() as connection:
            rows = self._execute(connection, """SELECT chat_id FROM pending_group_chat_checks
                ORDER BY observed_at LIMIT 5""").fetchall()
        return [int(row[0]) for row in rows]

    def is_group_chat_authorized(self, chat_id: int) -> bool:
        with self._session() as connection:
            return self._execute(connection, "SELECT 1 FROM hoa_group_chats WHERE chat_id = ?",
                                 (chat_id,)).fetchone() is not None

    def bind_group_chat(self, chat_id: int, link: str, title: str | None) -> bool:
        try:
            normalized = normalize_group_chat_link(link)
        except ValueError:
            return False
        with self._session() as connection:
            row = self._execute(connection, """SELECT chat_id FROM hoa_group_chats
                WHERE link = ?""", (normalized,)).fetchone()
            if row is None or row[0] not in {None, chat_id}:
                return False
            assigned = self._execute(connection, """SELECT 1 FROM hoa_group_chats
                WHERE chat_id = ? AND link <> ?""", (chat_id, normalized)).fetchone()
            if assigned:
                return False
            self._execute(connection, """UPDATE hoa_group_chats SET chat_id = ?, title = ?,
                updated_at = ? WHERE link = ?""",
                (chat_id, title, datetime.now(UTC).isoformat(), normalized))
        return True

    def complete_group_chat_check(self, chat_id: int) -> None:
        with self._session() as connection:
            self._execute(connection, "DELETE FROM pending_group_chat_checks WHERE chat_id = ?", (chat_id,))

    def defer_group_chat_check(self, chat_id: int) -> None:
        with self._session() as connection:
            self._execute(connection, """UPDATE pending_group_chat_checks SET observed_at = ?
                WHERE chat_id = ?""", (datetime.now(UTC).isoformat(), chat_id))

    def forget_group_chat(self, chat_id: int) -> None:
        with self._session() as connection:
            self._execute(connection, """UPDATE hoa_group_chats SET chat_id = NULL, title = NULL,
                updated_at = ? WHERE chat_id = ?""", (datetime.now(UTC).isoformat(), chat_id))
            self._execute(connection, "DELETE FROM pending_group_chat_checks WHERE chat_id = ?", (chat_id,))

    def create_specialist(
        self, space_id: str, specialty: str, full_name: str, phone: str,
    ) -> dict[str, Any]:
        full_name = " ".join(full_name.split())
        if specialty not in {"plumber", "electrician"}:
            raise ValueError("Выберите сантехника или электрика")
        if not re.fullmatch(r"[A-Za-zА-Яа-яЁё-]+(?:\s+[A-Za-zА-Яа-яЁё-]+){1,3}", full_name) or len(full_name) > 180:
            raise ValueError("Укажите ФИО специалиста")
        phone = normalize_phone_input(phone)
        now = datetime.now(UTC).isoformat()
        with self._session() as connection:
            self._execute(
                connection,
                """INSERT INTO specialists
                   (specialist_id, space_id, specialty, full_name, phone, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(space_id, phone) DO UPDATE SET
                   specialty = excluded.specialty, full_name = excluded.full_name,
                   updated_at = excluded.updated_at""",
                (uuid.uuid4().hex, space_id, specialty, full_name, phone, now, now),
            )
            row = self._execute(
                connection,
                """SELECT specialist_id, specialty, full_name, phone, user_id
                   FROM specialists WHERE space_id = ? AND phone = ?""",
                (space_id, phone),
            ).fetchone()
        return {
            "specialist_id": row[0], "specialty": row[1], "full_name": row[2],
            "phone": row[3], "linked": row[4] is not None,
        }

    @staticmethod
    def _validate_hours(hours: Any) -> list[dict[str, int]]:
        if not isinstance(hours, list) or len(hours) > 7:
            raise ValueError("Укажите часы работы по дням недели")
        result: list[dict[str, int]] = []
        seen: set[int] = set()
        for item in hours:
            if not isinstance(item, dict) or set(item) != {"weekday", "start_hour", "end_hour"}:
                raise ValueError("Некорректный интервал работы")
            day, start, end = (item[key] for key in ("weekday", "start_hour", "end_hour"))
            if any(type(value) is not int for value in (day, start, end)) or not (0 <= day <= 6 and 0 <= start < end <= 24) or day in seen:
                raise ValueError("Рабочие часы должны быть целыми часами без пересечений")
            seen.add(day)
            result.append({"weekday": day, "start_hour": start, "end_hour": end})
        return sorted(result, key=lambda item: item["weekday"])

    def _hours(self, connection: Any, specialist_id: str) -> list[dict[str, int]]:
        rows = self._execute(connection, "SELECT weekday, start_hour, end_hour FROM specialist_hours WHERE specialist_id = ? ORDER BY weekday", (specialist_id,)).fetchall()
        return [dict(zip(("weekday", "start_hour", "end_hour"), row)) for row in rows]

    def _save_hours(self, connection: Any, specialist_id: str, hours: list[dict[str, int]]) -> None:
        self._execute(connection, "DELETE FROM specialist_hours WHERE specialist_id = ?", (specialist_id,))
        for item in hours:
            self._execute(connection, "INSERT INTO specialist_hours (specialist_id, weekday, start_hour, end_hour) VALUES (?, ?, ?, ?)",
                          (specialist_id, item["weekday"], item["start_hour"], item["end_hour"]))

    def update_specialist(self, space_id: str, specialist_id: str, *, specialty: str, full_name: str,
                          phone: str, hours: Any) -> dict[str, Any] | None:
        full_name = " ".join(full_name.split())
        if specialty not in {"plumber", "electrician"}:
            raise ValueError("Выберите сантехника или электрика")
        if not re.fullmatch(r"[A-Za-zА-Яа-яЁё-]+(?:\s+[A-Za-zА-Яа-яЁё-]+){1,3}", full_name) or len(full_name) > 180:
            raise ValueError("Укажите ФИО специалиста")
        phone = normalize_phone_input(phone)
        clean_hours = self._validate_hours(hours)
        with self._session() as connection:
            old = self._execute(connection, "SELECT phone FROM specialists WHERE space_id = ? AND specialist_id = ?", (space_id, specialist_id)).fetchone()
            if old is None:
                return None
            duplicate = self._execute(connection, "SELECT specialist_id FROM specialists WHERE space_id = ? AND phone = ? AND specialist_id <> ?", (space_id, phone, specialist_id)).fetchone()
            if duplicate:
                raise ValueError("Этот номер уже закреплён за другим специалистом ТСЖ")
            self._execute(connection, """UPDATE specialists SET specialty = ?, full_name = ?, phone = ?,
                           user_id = CASE WHEN phone = ? THEN user_id ELSE NULL END, updated_at = ?
                           WHERE space_id = ? AND specialist_id = ?""",
                          (specialty, full_name, phone, phone, datetime.now(UTC).isoformat(), space_id, specialist_id))
            if old[0] != phone:
                self._execute(connection, """UPDATE visit_proposals SET status = 'cancelled'
                    WHERE specialist_id = ? AND status IN ('pending', 'awaiting_owner')""", (specialist_id,))
            self._save_hours(connection, specialist_id, clean_hours)
        return next((item for item in self.list_specialists(space_id) if item["specialist_id"] == specialist_id), None)

    def list_specialists(self, space_id: str) -> list[dict[str, Any]]:
        with self._session() as connection:
            rows = self._execute(
                connection,
                """SELECT specialist_id, specialty, full_name, phone, user_id
                   FROM specialists WHERE space_id = ? ORDER BY full_name""",
                (space_id,),
            ).fetchall()
            return [
                {"specialist_id": row[0], "specialty": row[1], "full_name": row[2],
                 "phone": row[3], "linked": row[4] is not None,
                 "work_hours": self._hours(connection, row[0])}
                for row in rows
            ]

    def specialist_memberships(self, user_id: int, phone: str) -> list[dict[str, Any]]:
        with self._session() as connection:
            rows = self._execute(
                connection,
                """SELECT p.specialist_id, p.space_id, p.specialty, p.full_name,
                          p.phone, s.name, s.address
                   FROM specialists p JOIN hoa_spaces s ON s.space_id = p.space_id
                   WHERE p.user_id = ? AND p.phone = ? ORDER BY s.name""",
                (user_id, phone),
            ).fetchall()
            return [dict(zip(
                ("specialist_id", "space_id", "specialty", "full_name", "phone",
                 "hoa_name", "address"), row,
            )) | {"work_hours": self._hours(connection, row[0])} for row in rows]

    def claim_specialist_phone(self, user_id: int, phone: str) -> list[dict[str, Any]]:
        with self._session() as connection:
            self._execute(
                connection,
                """UPDATE specialists SET user_id = ?, updated_at = ?
                   WHERE phone = ? AND (user_id IS NULL OR user_id = ?)""",
                (user_id, datetime.now(UTC).isoformat(), phone, user_id),
            )
        return self.specialist_memberships(user_id, phone)

    def find_specialist(self, space_id: str, specialty: str) -> dict[str, Any] | None:
        if specialty not in {"plumber", "electrician"}:
            return None
        with self._session() as connection:
            row = self._execute(
                connection,
                """SELECT p.specialist_id, p.full_name, p.user_id
                   FROM specialists p WHERE p.space_id = ? AND p.specialty = ?
                   ORDER BY CASE WHEN p.user_id IS NULL THEN 1 ELSE 0 END,
                     (SELECT count(*) FROM request_assignments a
                      JOIN service_requests q ON q.request_id = a.request_id
                      WHERE a.specialist_id = p.specialist_id
                        AND q.status IN ('review', 'in_progress')),
                     p.full_name LIMIT 1""",
                (space_id, specialty),
            ).fetchone()
        return {"specialist_id": row[0], "full_name": row[1], "user_id": row[2]} if row else None

    def get_space_timezone(self, space_id: str) -> str:
        with self._session() as connection:
            row = self._execute(connection, """SELECT z.timezone, s.address FROM hoa_spaces s
                LEFT JOIN hoa_timezones z ON z.space_id = s.space_id WHERE s.space_id = ?""", (space_id,)).fetchone()
        if row is None:
            raise ValueError("ТСЖ не найдено")
        if row[0]:
            return str(row[0])
        address = str(row[1]).lower()
        if any(name in address for name in ("самар", "тольятти", "сызран", "новокуйбышев")):
            return "Europe/Samara"
        return "Europe/Moscow"

    def set_space_timezone(self, space_id: str, timezone: str) -> None:
        if timezone not in TIMEZONE_LABELS:
            raise ValueError("Выберите часовой пояс расположения ТСЖ")
        with self._session() as connection:
            self._execute(connection, """INSERT INTO hoa_timezones (space_id, timezone) VALUES (?, ?)
                ON CONFLICT(space_id) DO UPDATE SET timezone = excluded.timezone""", (space_id, timezone))

    def _assigned_specialist(self, connection: Any, user_id: int, phone: str, request_id: str) -> tuple[Any, ...] | None:
        return self._execute(connection, """SELECT p.specialist_id, q.space_id, q.user_id, q.title, r.unit, q.status
            FROM service_requests q
            JOIN request_assignments a ON a.request_id = q.request_id
            JOIN specialists p ON p.specialist_id = a.specialist_id AND p.space_id = q.space_id
            JOIN residents r ON r.resident_id = q.resident_id
            WHERE q.request_id = ? AND p.user_id = ? AND p.phone = ?""",
            (request_id, user_id, phone)).fetchone()

    def _available_visit_hours(self, connection: Any, specialist_id: str, request_id: str,
                               timezone: str, day_text: str) -> list[int]:
        if self._specialist_has_emergency(connection, specialist_id):
            raise ValueError("Специалист устраняет аварию. Выбор времени возобновится после её исполнения")
        try:
            day = date.fromisoformat(day_text)
        except ValueError as error:
            raise ValueError("Укажите дату посещения") from error
        now = datetime.now(ZoneInfo(timezone))
        if day < now.date() or day >= now.date() + timedelta(days=VISIT_WINDOW_DAYS):
            return []
        work = next((item for item in self._hours(connection, specialist_id)
                     if item["weekday"] == day.weekday()), None)
        if work is None:
            return []
        occupied = self._execute(connection, """SELECT visit_hour FROM visit_proposals
            WHERE specialist_id = ? AND visit_date = ? AND request_id <> ?
            AND status IN ('pending', 'accepted')""", (specialist_id, day_text, request_id)).fetchall()
        taken = {int(row[0]) for row in occupied}
        return [hour for hour in range(work["start_hour"], work["end_hour"])
                if hour not in taken and (day > now.date() or hour > now.hour)]

    def _specialist_has_emergency(self, connection: Any, specialist_id: str) -> bool:
        return self._execute(connection, """SELECT 1 FROM service_requests q
            JOIN request_assignments a ON a.request_id = q.request_id
            WHERE a.specialist_id = ? AND q.priority = 'emergency'
              AND q.status <> 'done' LIMIT 1""", (specialist_id,)).fetchone() is not None

    def request_visit_access(self, user_id: int, phone: str, request_id: str) -> dict[str, Any]:
        token = secrets.token_urlsafe(12)
        now = datetime.now(UTC).isoformat()
        with self._session() as connection:
            request = self._assigned_specialist(connection, user_id, phone, request_id)
            if request is None or request[5] not in {"review", "in_progress"}:
                raise ValueError("Заявка больше не назначена вам")
            timezone = self.get_space_timezone(request[1])
            today = datetime.now(ZoneInfo(timezone)).date()
            if not any(self._available_visit_hours(connection, request[0], request_id, timezone,
                                                   (today + timedelta(days=offset)).isoformat())
                       for offset in range(VISIT_WINDOW_DAYS)):
                raise ValueError("У специалиста нет свободных часов на ближайшие 10 дней. Попросите председателя проверить расписание")
            previous = self._execute(connection, "SELECT status FROM visit_proposals WHERE request_id = ?", (request_id,)).fetchone()
            if previous and previous[0] == "accepted":
                raise ValueError("Время посещения уже согласовано с собственником")
            self._execute(connection, """INSERT INTO visit_proposals
                (request_id, specialist_id, visit_date, visit_hour, timezone, token, status, created_at)
                VALUES (?, ?, '', -1, ?, ?, 'awaiting_owner', ?)
                ON CONFLICT(request_id) DO UPDATE SET specialist_id = excluded.specialist_id,
                visit_date = excluded.visit_date, visit_hour = excluded.visit_hour,
                timezone = excluded.timezone, token = excluded.token, status = 'awaiting_owner',
                created_at = excluded.created_at, responded_at = NULL""",
                (request_id, request[0], timezone, token, now))
        return {"owner_user_id": request[2], "title": request[3], "unit": request[4],
                "timezone": timezone, "token": token}

    def _owner_visit_request(self, connection: Any, owner_user_id: int,
                             request_id: str, token: str) -> tuple[Any, ...] | None:
        return self._execute(connection, """SELECT v.specialist_id, q.space_id, p.user_id, q.title
            FROM visit_proposals v JOIN service_requests q ON q.request_id = v.request_id
            JOIN residents r ON r.resident_id = q.resident_id
            JOIN request_assignments a ON a.request_id = q.request_id
            JOIN specialists p ON p.specialist_id = v.specialist_id AND p.space_id = q.space_id
            WHERE v.request_id = ? AND v.token = ? AND v.status = 'awaiting_owner'
              AND q.user_id = ? AND r.user_id = ? AND r.active = 1
              AND q.status IN ('review', 'in_progress') AND a.specialist_id = v.specialist_id""",
            (request_id, token, owner_user_id, owner_user_id)).fetchone()

    def owner_visit_days(self, owner_user_id: int, request_id: str,
                         token: str, page: int) -> dict[str, Any] | None:
        if not 0 <= page <= (VISIT_WINDOW_DAYS - 1) // 7:
            raise ValueError("Недоступная страница расписания")
        with self._session() as connection:
            request = self._owner_visit_request(connection, owner_user_id, request_id, token)
            if request is None:
                return None
            timezone = self.get_space_timezone(request[1])
            today = datetime.now(ZoneInfo(timezone)).date()
            dates = [(today + timedelta(days=offset)).isoformat()
                     for offset in range(page * 7, min(page * 7 + 7, VISIT_WINDOW_DAYS))]
            available = [day for day in dates if self._available_visit_hours(
                connection, request[0], request_id, timezone, day)]
            return {"dates": available, "page": page, "has_next": page * 7 + 7 < VISIT_WINDOW_DAYS,
                    "timezone": timezone}

    def owner_visit_hours(self, owner_user_id: int, request_id: str,
                          token: str, day_text: str) -> dict[str, Any] | None:
        with self._session() as connection:
            request = self._owner_visit_request(connection, owner_user_id, request_id, token)
            if request is None:
                return None
            timezone = self.get_space_timezone(request[1])
            return {"hours": self._available_visit_hours(connection, request[0], request_id,
                                                          timezone, day_text), "timezone": timezone}

    def confirm_owner_visit(self, owner_user_id: int, request_id: str, token: str,
                            day_text: str, hour: int) -> dict[str, Any] | None:
        if type(hour) is not int:
            raise ValueError("Выберите час посещения")
        try:
            with self._session() as connection:
                request = self._owner_visit_request(connection, owner_user_id, request_id, token)
                if request is None:
                    return None
                timezone = self.get_space_timezone(request[1])
                if hour not in self._available_visit_hours(connection, request[0], request_id,
                                                           timezone, day_text):
                    raise ValueError("Этот час уже недоступен. Выберите другой")
                changed = self._execute(connection, """UPDATE visit_proposals SET visit_date = ?,
                    visit_hour = ?, timezone = ?, status = 'accepted', responded_at = ?
                    WHERE request_id = ? AND token = ? AND status = 'awaiting_owner'""",
                    (day_text, hour, timezone, datetime.now(UTC).isoformat(), request_id, token))
                if changed.rowcount != 1:
                    return None
                if request[2] is not None:
                    self._execute(connection, """INSERT INTO visit_approval_notices
                        (request_id, specialist_user_id, created_at, notified_at)
                        VALUES (?, ?, ?, NULL)
                        ON CONFLICT(request_id) DO UPDATE SET
                        specialist_user_id = excluded.specialist_user_id,
                        created_at = excluded.created_at, notified_at = NULL""",
                        (request_id, int(request[2]), datetime.now(UTC).isoformat()))
            return {"date": day_text, "hour": hour, "timezone": timezone,
                    "specialist_user_id": request[2], "title": request[3], "accepted": True}
        except Exception as error:
            if isinstance(error, sqlite3.IntegrityError) or getattr(error, "sqlstate", None) == "23505":
                raise ValueError("Этот час уже занят. Выберите другой") from error
            raise

    def decline_owner_access(self, owner_user_id: int, request_id: str,
                             token: str) -> dict[str, Any] | None:
        with self._session() as connection:
            request = self._owner_visit_request(connection, owner_user_id, request_id, token)
            if request is None:
                return None
            changed = self._execute(connection, """UPDATE visit_proposals SET status = 'declined',
                responded_at = ? WHERE request_id = ? AND token = ? AND status = 'awaiting_owner'""",
                (datetime.now(UTC).isoformat(), request_id, token))
            if changed.rowcount != 1:
                return None
        return {"specialist_user_id": request[2], "title": request[3], "accepted": False}

    def respond_visit(self, owner_user_id: int, request_id: str, token: str, accept: bool) -> dict[str, Any] | None:
        with self._session() as connection:
            row = self._execute(connection, """SELECT v.visit_date, v.visit_hour, v.timezone, p.user_id, q.title,
                v.token, v.status, q.status, a.specialist_id, v.specialist_id
                FROM visit_proposals v JOIN service_requests q ON q.request_id = v.request_id
                JOIN request_assignments a ON a.request_id = q.request_id
                JOIN specialists p ON p.specialist_id = v.specialist_id
                JOIN residents r ON r.resident_id = q.resident_id
                WHERE v.request_id = ? AND q.user_id = ? AND r.user_id = ? AND r.active = 1""",
                (request_id, owner_user_id, owner_user_id)).fetchone()
            if row is None or row[5] != token or row[6] != "pending" or row[7] not in {"review", "in_progress"} or row[8] != row[9]:
                return None
            updated = self._execute(connection, """UPDATE visit_proposals SET status = ?, responded_at = ?
                WHERE request_id = ? AND token = ? AND status = 'pending'""",
                ("accepted" if accept else "declined", datetime.now(UTC).isoformat(), request_id, token))
            if updated.rowcount != 1:
                return None
            if accept and row[3] is not None:
                self._execute(connection, """INSERT INTO visit_approval_notices
                    (request_id, specialist_user_id, created_at, notified_at)
                    VALUES (?, ?, ?, NULL)
                    ON CONFLICT(request_id) DO UPDATE SET
                    specialist_user_id = excluded.specialist_user_id,
                    created_at = excluded.created_at, notified_at = NULL""",
                    (request_id, int(row[3]), datetime.now(UTC).isoformat()))
        return {"date": row[0], "hour": row[1], "timezone": row[2],
                "specialist_user_id": row[3], "title": row[4], "accepted": accept}

    def pending_visit_approval_notices(self) -> list[dict[str, Any]]:
        with self._session() as connection:
            rows = self._execute(connection, """SELECT n.request_id, n.specialist_user_id,
                q.title, v.visit_date, v.visit_hour, v.timezone, r.unit, s.address
                FROM visit_approval_notices n
                JOIN visit_proposals v ON v.request_id = n.request_id
                JOIN service_requests q ON q.request_id = n.request_id
                JOIN residents r ON r.resident_id = q.resident_id
                JOIN hoa_spaces s ON s.space_id = q.space_id
                WHERE n.notified_at IS NULL AND v.status = 'accepted'
                ORDER BY n.created_at LIMIT 20""").fetchall()
        return [dict(zip(("request_id", "specialist_user_id", "title", "date",
                          "hour", "timezone", "unit", "address"), row)) for row in rows]

    def mark_visit_approval_notified(self, request_id: str) -> None:
        with self._session() as connection:
            self._execute(connection, """UPDATE visit_approval_notices SET notified_at = ?
                WHERE request_id = ? AND notified_at IS NULL""",
                (datetime.now(UTC).isoformat(), request_id))

    def cancel_visit_proposal(self, request_id: str, token: str) -> None:
        with self._session() as connection:
            self._execute(connection, """UPDATE visit_proposals SET status = 'cancelled'
                WHERE request_id = ? AND token = ? AND status = 'awaiting_owner'""", (request_id, token))

    def set_quick_draft(
        self, user_id: int, resident_id: str, stage: str, *,
        title: str = "", description: str = "", specialty: str = "",
    ) -> None:
        if stage not in {"awaiting_unit", "awaiting_description", "awaiting_photos",
                         "awaiting_photos_chairman", "awaiting_fallback"}:
            raise ValueError("Неизвестный этап заявки")
        with self._session() as connection:
            self._execute(
                connection,
                """INSERT INTO quick_request_drafts
                   (user_id, resident_id, stage, title, description, specialty, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                   resident_id = excluded.resident_id, stage = excluded.stage,
                   title = excluded.title, description = excluded.description,
                   specialty = excluded.specialty, updated_at = excluded.updated_at""",
                (user_id, resident_id, stage, title, description, specialty, time.time()),
            )

    def get_quick_draft(self, user_id: int) -> dict[str, Any] | None:
        with self._session() as connection:
            row = self._execute(
                connection,
                """SELECT resident_id, stage, title, description, specialty, updated_at
                   FROM quick_request_drafts WHERE user_id = ?""",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        if time.time() - float(row[5]) > 1800:
            self.clear_quick_draft(user_id)
            return None
        return dict(zip(("resident_id", "stage", "title", "description", "specialty"), row[:5]))

    def clear_quick_draft(self, user_id: int) -> None:
        with self._session() as connection:
            self._execute(connection, "DELETE FROM draft_request_photos WHERE user_id = ?", (user_id,))
            self._execute(connection, "DELETE FROM quick_request_drafts WHERE user_id = ?", (user_id,))

    def quick_photo_count(self, user_id: int) -> int:
        with self._session() as connection:
            row = self._execute(
                connection, "SELECT count(*) FROM draft_request_photos WHERE user_id = ?", (user_id,),
            ).fetchone()
        return int(row[0])

    def add_quick_photos(self, user_id: int, photos: list[tuple[str, bytes]]) -> int:
        if not photos or len(photos) > 3:
            raise ValueError("Отправьте от 1 до 3 фотографий")
        if any(mime not in {"image/jpeg", "image/png", "image/webp", "image/gif", "image/heic"}
               or not content or len(content) > 10 * 1024 * 1024 for mime, content in photos):
            raise ValueError("Каждое фото должно быть не больше 10 МБ")
        with self._session() as connection:
            if not self._database_url:
                self._execute(connection, "BEGIN IMMEDIATE")
            draft = self._execute(
                connection, "SELECT stage FROM quick_request_drafts WHERE user_id = ?", (user_id,),
            ).fetchone()
            if draft is None or draft[0] not in {"awaiting_photos", "awaiting_photos_chairman"}:
                raise ValueError("Создание заявки не ожидает фотографий")
            count = int(self._execute(
                connection, "SELECT count(*) FROM draft_request_photos WHERE user_id = ?", (user_id,),
            ).fetchone()[0])
            if count + len(photos) > 3:
                raise ValueError("К заявке можно прикрепить не больше 3 фотографий")
            now = datetime.now(UTC).isoformat()
            for index, (mime_type, content) in enumerate(photos, count + 1):
                self._execute(
                    connection,
                    """INSERT INTO draft_request_photos
                       (user_id, position, mime_type, content, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (user_id, index, mime_type, content, now),
                )
            self._execute(
                connection, "UPDATE quick_request_drafts SET updated_at = ? WHERE user_id = ?",
                (time.time(), user_id),
            )
        return count + len(photos)

    def import_registry(
        self,
        space_id: str,
        filename: str,
        content: bytes,
        residents: list[ResidentEntry],
        skipped_units: list[str],
    ) -> None:
        if not residents:
            raise ValueError("Нельзя импортировать пустой реестр")
        now = datetime.now(UTC).isoformat()
        digest = hashlib.sha256(content).hexdigest()
        with self._session() as connection:
            self._execute(connection, "UPDATE residents SET active = 0 WHERE space_id = ?", (space_id,))
            for entry in residents:
                self._execute(
                    connection,
                    """INSERT INTO residents
                        (resident_id, space_id, unit, full_name, area, share, ownership,
                         code_prefix, code_secret, active, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                       ON CONFLICT(space_id, unit, full_name) DO UPDATE SET
                         area = excluded.area, share = excluded.share,
                         ownership = excluded.ownership, active = 1,
                         updated_at = excluded.updated_at""",
                    (
                        uuid.uuid4().hex, space_id, entry.unit, entry.full_name,
                        entry.area, entry.share, entry.ownership,
                        secrets.token_hex(5).upper(), secrets.token_hex(32), now,
                    ),
                )
            self._execute(
                connection,
                """INSERT INTO registry_imports
                   (import_id, space_id, filename, document_sha256, resident_count,
                    skipped_units, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (uuid.uuid4().hex, space_id, filename, digest, len(residents),
                 ", ".join(skipped_units), now),
            )

    @staticmethod
    def _code(prefix: str, secret: str, window: int) -> str:
        suffix = hmac.new(bytes.fromhex(secret), str(window).encode(), hashlib.sha256).hexdigest()[:10].upper()
        return f"{prefix}-{suffix}"

    def list_residents(self, space_id: str, *, now: float | None = None) -> list[dict[str, Any]]:
        current = time.time() if now is None else now
        window = int(current // 7200)
        expires_at = datetime.fromtimestamp((window + 1) * 7200, UTC).isoformat()
        with self._session() as connection:
            rows = self._execute(
                connection,
                """SELECT resident_id, unit, full_name, area, share, ownership,
                          code_prefix, code_secret, user_id
                   FROM residents WHERE space_id = ? AND active = 1
                   ORDER BY unit, full_name""",
                (space_id,),
            ).fetchall()
        return [
            {
                "resident_id": row[0], "unit": row[1], "full_name": row[2],
                "area": row[3], "share": row[4], "ownership": row[5],
                "code": self._code(row[6], row[7], window),
                "code_expires_at": expires_at, "linked": row[8] is not None,
            }
            for row in rows
        ]

    def claim_code(self, user_id: int, phone: str, code: str, *, now: float | None = None) -> dict[str, Any]:
        window = int((time.time() if now is None else now) // 7200)
        cleaned = code.strip().upper().replace(" ", "")
        with self._session() as connection:
            if not self._database_url:
                self._execute(connection, "BEGIN IMMEDIATE")
            prior = self._execute(
                connection, "SELECT window_number, attempts FROM code_attempts WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            attempts = prior[1] if prior and prior[0] == window else 0
            if attempts >= 5:
                raise CodeError("Слишком много попыток. Попросите новый код через два часа")
            self._execute(
                connection,
                """INSERT INTO code_attempts(user_id, window_number, attempts)
                   VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET
                   window_number = excluded.window_number, attempts = excluded.attempts""",
                (user_id, window, attempts + 1),
            )
        if not re.fullmatch(r"[0-9A-F]{10}-[0-9A-F]{10}", cleaned):
            raise CodeError("Код неверный или срок его действия истёк")
        with self._session() as connection:
            if not self._database_url:
                self._execute(connection, "BEGIN IMMEDIATE")
            prefix = cleaned[:10]
            query = """SELECT resident_id, space_id, unit, full_name, code_secret, user_id
                       FROM residents WHERE code_prefix = ? AND active = 1"""
            if self._database_url:
                query += " FOR UPDATE"
            row = self._execute(connection, query, (prefix,)).fetchone()
            if row is None or not hmac.compare_digest(cleaned, self._code(prefix, row[4], window)):
                raise CodeError("Код неверный или срок его действия истёк")
            if row[5] is not None and int(row[5]) != user_id:
                raise CodeError("Этот собственник уже привязан к другому аккаунту MAX")
            self._execute(
                connection, "UPDATE residents SET user_id = ?, phone = ? WHERE resident_id = ?",
                (user_id, phone, row[0]),
            )
            return {"resident_id": row[0], "space_id": row[1], "unit": row[2], "full_name": row[3]}

    def memberships(self, user_id: int) -> list[dict[str, Any]]:
        with self._session() as connection:
            rows = self._execute(
                connection,
                """SELECT r.resident_id, r.space_id, r.unit, r.full_name, r.area,
                          r.share, r.ownership, s.name, s.address
                   FROM residents r JOIN hoa_spaces s ON s.space_id = r.space_id
                   WHERE r.user_id = ? AND r.active = 1 ORDER BY s.name, r.unit""",
                (user_id,),
            ).fetchall()
        return [dict(zip(
            ("resident_id", "space_id", "unit", "full_name", "area", "share",
             "ownership", "hoa_name", "address"), row
        )) for row in rows]

    def create_request(
        self, user_id: int, resident_id: str, title: str, description: str,
        *, specialist_id: str | None = None, specialty: str = "other", source: str = "mini_app",
        use_draft_photos: bool = False,
    ) -> dict[str, Any]:
        title = title.strip()
        if not 3 <= len(title) <= 120 or len(description.strip()) < 5 or len(description) > 3000:
            raise ValueError("Укажите тему (3–120 символов) и описание (5–3000 символов)")
        if specialty not in {"plumber", "electrician", "other"}:
            raise ValueError("Неизвестная категория заявки")
        now = datetime.now(UTC).isoformat()
        initial_status = "review"
        request_id = uuid.uuid4().hex
        with self._session() as connection:
            row = self._execute(
                connection,
                "SELECT space_id FROM residents WHERE resident_id = ? AND user_id = ? AND active = 1",
                (resident_id, user_id),
            ).fetchone()
            if row is None:
                raise PermissionError("Собственник не авторизован для этого помещения")
            if use_draft_photos:
                photo_count = int(self._execute(
                    connection,
                    "SELECT count(*) FROM draft_request_photos WHERE user_id = ?", (user_id,),
                ).fetchone()[0])
                if not 1 <= photo_count <= 3:
                    raise ValueError("Для заявки нужно от 1 до 3 фотографий")
            if specialist_id is not None:
                specialist = self._execute(
                    connection,
                    """SELECT specialist_id FROM specialists
                       WHERE specialist_id = ? AND space_id = ? AND specialty = ?""",
                    (specialist_id, row[0], specialty),
                ).fetchone()
                if specialist is None:
                    raise ValueError("Выбранный специалист недоступен в этом ТСЖ")
            self._execute(
                connection,
                """INSERT INTO service_requests
                   (request_id, space_id, resident_id, user_id, title, description,
                    status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (request_id, row[0], resident_id, user_id, title, description,
                 initial_status, now, now),
            )
            self._execute(
                connection,
                """INSERT INTO request_assignments
                   (request_id, target, specialty, specialist_id, source, assigned_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (request_id, "specialist" if specialist_id else "chairman",
                 specialty, specialist_id, source, now),
            )
            if use_draft_photos:
                self._execute(
                    connection,
                    """INSERT INTO request_photos
                       (request_id, position, mime_type, content, created_at)
                       SELECT ?, position, mime_type, content, created_at
                       FROM draft_request_photos WHERE user_id = ?""",
                    (request_id, user_id),
                )
                self._execute(connection, "DELETE FROM draft_request_photos WHERE user_id = ?", (user_id,))
                self._execute(connection, "DELETE FROM quick_request_drafts WHERE user_id = ?", (user_id,))
        return {
            "request_id": request_id, "status": initial_status, "created_at": now,
            "target": "specialist" if specialist_id else "chairman",
        }

    def list_requests(self, *, user_id: int | None = None, space_id: str | None = None) -> list[dict[str, Any]]:
        if (user_id is None) == (space_id is None):
            raise ValueError("Укажите пользователя или пространство")
        column, value = ("q.user_id", user_id) if user_id is not None else ("q.space_id", space_id)
        with self._session() as connection:
            rows = self._execute(
                connection,
                f"""SELECT q.request_id, q.resident_id, q.title, q.description,
                           q.status, q.created_at, q.updated_at, r.unit, r.full_name,
                           COALESCE(a.target, 'chairman'), a.specialty, p.full_name,
                           (SELECT count(*) FROM request_photos ph WHERE ph.request_id = q.request_id),
                           q.priority, s.name, s.address, a.specialist_id, q.space_id,
                           v.visit_date, v.visit_hour, v.timezone, v.status
                    FROM service_requests q JOIN residents r ON r.resident_id = q.resident_id
                    JOIN hoa_spaces s ON s.space_id = q.space_id
                    LEFT JOIN request_assignments a ON a.request_id = q.request_id
                    LEFT JOIN specialists p ON p.specialist_id = a.specialist_id
                    LEFT JOIN visit_proposals v ON v.request_id = q.request_id
                    WHERE {column} = ? {"AND r.user_id = ? AND r.active = 1" if user_id is not None else ""}
                    ORDER BY q.created_at DESC LIMIT 200""",
                (value, user_id) if user_id is not None else (value,),
            ).fetchall()
        return [dict(zip(
            ("request_id", "resident_id", "title", "description", "status", "created_at",
             "updated_at", "unit", "full_name", "target", "specialty", "specialist_name",
             "photo_count", "priority", "hoa_name", "address", "specialist_id",
             "space_id", "visit_date", "visit_hour", "visit_timezone", "visit_status"), row
        )) for row in rows]

    def list_specialist_requests(self, user_id: int, phone: str) -> list[dict[str, Any]]:
        with self._session() as connection:
            rows = self._execute(
                connection,
                """SELECT q.request_id, q.resident_id, q.title, q.description,
                          q.status, q.created_at, q.updated_at, r.unit, r.full_name,
                          a.target, a.specialty, p.full_name,
                          (SELECT count(*) FROM request_photos ph WHERE ph.request_id = q.request_id),
                          q.priority, s.name, s.address, a.specialist_id, q.space_id,
                          v.visit_date, v.visit_hour, v.timezone, v.status,
                          EXISTS (SELECT 1 FROM service_requests emergency
                              JOIN request_assignments assigned ON assigned.request_id = emergency.request_id
                              WHERE assigned.specialist_id = p.specialist_id
                                AND emergency.priority = 'emergency'
                                AND emergency.status <> 'done')
                   FROM service_requests q
                   JOIN request_assignments a ON a.request_id = q.request_id
                   JOIN specialists p ON p.specialist_id = a.specialist_id
                   JOIN residents r ON r.resident_id = q.resident_id
                   JOIN hoa_spaces s ON s.space_id = q.space_id
                   LEFT JOIN visit_proposals v ON v.request_id = q.request_id
                   WHERE p.user_id = ? AND p.phone = ? AND q.space_id = p.space_id
                   ORDER BY q.created_at DESC LIMIT 200""",
                (user_id, phone),
            ).fetchall()
        return [dict(zip(
            ("request_id", "resident_id", "title", "description", "status", "created_at",
             "updated_at", "unit", "full_name", "target", "specialty", "specialist_name",
             "photo_count", "priority", "hoa_name", "address", "specialist_id",
             "space_id", "visit_date", "visit_hour", "visit_timezone", "visit_status",
             "emergency_busy"), row
        )) for row in rows]

    def get_request_photo(
        self, user_id: int, request_id: str, position: int,
        *, chairman_space_id: str = "", specialist_phone: str = "",
    ) -> tuple[str, bytes] | None:
        if position not in {1, 2, 3}:
            return None
        with self._session() as connection:
            row = self._execute(
                connection,
                """SELECT ph.mime_type, ph.content FROM request_photos ph
                   JOIN service_requests q ON q.request_id = ph.request_id
                   JOIN residents r ON r.resident_id = q.resident_id
                   LEFT JOIN request_assignments a ON a.request_id = q.request_id
                   LEFT JOIN specialists p ON p.specialist_id = a.specialist_id
                   WHERE q.request_id = ? AND ph.position = ? AND (
                     (q.user_id = ? AND r.user_id = ? AND r.active = 1)
                     OR (q.space_id = ? AND ? <> '')
                     OR (p.user_id = ? AND p.phone = ? AND p.space_id = q.space_id)
                   )""",
                (request_id, position, user_id, user_id,
                 chairman_space_id, chairman_space_id, user_id, specialist_phone),
            ).fetchone()
        return (str(row[0]), bytes(row[1])) if row else None

    def get_request_notification_context(self, space_id: str, request_id: str) -> dict[str, Any] | None:
        with self._session() as connection:
            row = self._execute(
                connection,
                """SELECT q.user_id, q.status, q.title, r.unit, s.name,
                          a.specialist_id, p.user_id, p.full_name,
                          v.status, v.visit_date, v.visit_hour, v.timezone, q.priority,
                          q.space_id
                   FROM service_requests q
                   JOIN residents r ON r.resident_id = q.resident_id
                   JOIN hoa_spaces s ON s.space_id = q.space_id
                   LEFT JOIN request_assignments a ON a.request_id = q.request_id
                   LEFT JOIN specialists p ON p.specialist_id = a.specialist_id
                   LEFT JOIN visit_proposals v ON v.request_id = q.request_id
                   WHERE q.space_id = ? AND q.request_id = ?""",
                (space_id, request_id),
            ).fetchone()
        return dict(zip(("owner_user_id", "status", "title", "unit", "hoa_name",
                         "specialist_id", "specialist_user_id", "specialist_name",
                         "visit_status", "visit_date", "visit_hour", "visit_timezone",
                         "priority", "space_id"), row)) if row else None

    def set_request_controls(
        self, space_id: str, request_id: str, *,
        status: str | None = None, priority: str | None = None,
        specialist_id: str | None = None,
    ) -> bool:
        if status is None and priority is None and specialist_id is None:
            raise ValueError("Укажите изменение заявки")
        if status is not None and status not in {"review", "in_progress", "done", "rejected"}:
            raise ValueError("Неизвестный статус заявки")
        if priority is not None and priority not in {"emergency", "urgent", "today", "planned"}:
            raise ValueError("Неизвестный приоритет заявки")
        with self._session() as connection:
            current = self._execute(
                connection,
            """SELECT q.status, a.specialist_id, q.priority FROM service_requests q
                   LEFT JOIN request_assignments a ON a.request_id = q.request_id
                   WHERE q.request_id = ? AND q.space_id = ?""",
                (request_id, space_id),
            ).fetchone()
            if current is None:
                return False
            resulting_status = status if status is not None else current[0]
            resulting_priority = priority if priority is not None else current[2]
            if resulting_priority == "emergency" and resulting_status == "rejected":
                raise ValueError("Аварийную заявку нельзя отклонить. Переназначьте исполнителя")
            if current[2] == "emergency" and resulting_status != "done":
                if priority is not None and priority != "emergency":
                    raise ValueError("Приоритет «Авария» сохраняется до исполнения заявки")
            specialty = None
            if specialist_id:
                specialist = self._execute(
                    connection,
                    """SELECT specialty FROM specialists
                       WHERE specialist_id = ? AND space_id = ? AND user_id IS NOT NULL""",
                    (specialist_id, space_id),
                ).fetchone()
                if specialist is None:
                    raise ValueError("Выбранный специалист не авторизован в этом ТСЖ")
                specialty = specialist[0]
            now = datetime.now(UTC).isoformat()
            updates: list[str] = []
            values: list[Any] = []
            effective_status = status if status is not None else (
                "review" if specialist_id and specialist_id != current[1]
                and current[0] == "in_progress" else None
            )
            if effective_status is not None:
                updates.append("status = ?")
                values.append(effective_status)
            if priority is not None:
                updates.append("priority = ?")
                values.append(priority)
            updates.append("updated_at = ?")
            values.extend((now, request_id, space_id))
            cursor = self._execute(
                connection,
                f"UPDATE service_requests SET {', '.join(updates)} WHERE request_id = ? AND space_id = ?",
                tuple(values),
            )
            if specialist_id is not None:
                self._execute(
                    connection,
                    """UPDATE request_assignments
                       SET target = ?, specialist_id = ?, specialty = COALESCE(?, specialty),
                           source = 'chairman', assigned_at = ?
                       WHERE request_id = ?""",
                    ("specialist" if specialist_id else "chairman",
                     specialist_id or None, specialty, now, request_id),
                )
                if (specialist_id or None) != current[1]:
                    self._execute(connection, """UPDATE visit_proposals SET status = 'cancelled'
                        WHERE request_id = ? AND status IN ('awaiting_owner', 'pending', 'accepted')""", (request_id,))
            if effective_status in {"done", "rejected"}:
                self._execute(connection, """UPDATE visit_proposals SET status = 'cancelled'
                    WHERE request_id = ? AND status IN ('awaiting_owner', 'pending')""", (request_id,))
            return cursor.rowcount == 1

    def set_request_status(self, space_id: str, request_id: str, status: str) -> bool:
        return self.set_request_controls(space_id, request_id, status=status)

    def set_request_priority(self, space_id: str, request_id: str, priority: str) -> bool:
        return self.set_request_controls(space_id, request_id, priority=priority)

    def set_specialist_request_controls(
        self, user_id: int, phone: str, request_id: str, *,
        status: str | None = None, priority: str | None = None,
    ) -> bool:
        if status is None and priority is None:
            raise ValueError("Укажите статус или приоритет")
        if status is not None and status not in {"in_progress", "done", "rejected"}:
            raise ValueError("Неизвестный статус заявки")
        if priority is not None and priority not in {"emergency", "urgent", "today", "planned"}:
            raise ValueError("Неизвестный приоритет заявки")
        with self._session() as connection:
            row = self._execute(
                connection,
                """SELECT q.status, q.priority FROM service_requests q
                   JOIN request_assignments a ON a.request_id = q.request_id
                   JOIN specialists p ON p.specialist_id = a.specialist_id
                   WHERE q.request_id = ? AND p.user_id = ? AND p.phone = ?
                     AND p.space_id = q.space_id""",
                (request_id, user_id, phone),
            ).fetchone()
            if row is None:
                return False
            current = str(row[0])
            resulting_status = status if status is not None else current
            resulting_priority = priority if priority is not None else row[1]
            if resulting_priority == "emergency" and resulting_status == "rejected":
                raise ValueError("Аварийную заявку нельзя отклонить. Попросите председателя сменить исполнителя")
            if row[1] == "emergency" and resulting_status != "done":
                if priority is not None and priority != "emergency":
                    raise ValueError("Приоритет «Авария» сохраняется до исполнения заявки")
            if status is not None and status != current:
                allowed = {"review": {"in_progress", "rejected"}, "in_progress": {"done"}}
                if status not in allowed.get(current, set()):
                    raise ValueError("Недопустимый переход статуса заявки")
            updates: list[str] = []
            values: list[Any] = []
            if status is not None:
                updates.append("status = ?")
                values.append(status)
            if priority is not None:
                updates.append("priority = ?")
                values.append(priority)
            updates.append("updated_at = ?")
            values.extend((datetime.now(UTC).isoformat(), request_id, current, user_id, phone))
            cursor = self._execute(
                connection,
                f"""UPDATE service_requests SET {', '.join(updates)}
                    WHERE request_id = ? AND status = ? AND EXISTS (
                      SELECT 1 FROM request_assignments a
                      JOIN specialists p ON p.specialist_id = a.specialist_id
                      WHERE a.request_id = service_requests.request_id
                        AND p.user_id = ? AND p.phone = ?
                        AND p.space_id = service_requests.space_id)""",
                tuple(values),
            )
            if cursor.rowcount == 1 and status in {"done", "rejected"}:
                self._execute(connection, """UPDATE visit_proposals SET status = 'cancelled'
                    WHERE request_id = ? AND status IN ('awaiting_owner', 'pending')""", (request_id,))
            return cursor.rowcount == 1

    def set_specialist_request_status(
        self, user_id: int, phone: str, request_id: str, status: str,
    ) -> bool:
        return self.set_specialist_request_controls(user_id, phone, request_id, status=status)

    def set_specialist_request_priority(
        self, user_id: int, phone: str, request_id: str, priority: str,
    ) -> bool:
        return self.set_specialist_request_controls(user_id, phone, request_id, priority=priority)
