"""Seed three repeatable filming flows for one MAX-verified account.

Only rows with deterministic demo identifiers are reset. User-created requests,
verified contacts, existing chairmen and other HOA records are not deleted.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import sqlite3
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.role_access import ROLE_SWITCH_PHONE

PHONE = ROLE_SWITCH_PHONE
NAMESPACE = uuid.UUID("20719d89-c439-44de-b2ac-01d355836361")
DEMO_UNIT = "ДЕМО-0500"
DEMO_NAME = "Демо Собственник"
DEMO_SPACE_NAME = "Демонстрационное ТСЖ"
DEMO_ADDRESS = "Москва, ул. Садовая, 18"
DEMO_PROFILE_FILENAME = "ДЕМО: профиль для видеозаписи, протокол не проверен"
DEMO_SPECIALIST_PHONE = "+70000000501"
ASSETS = Path(__file__).resolve().parent / "assets"


class DemoSetupError(RuntimeError):
    """The seed would be incomplete or might alter an unrelated record."""


def _id(kind: str, space_id: str = "") -> str:
    return uuid.uuid5(NAMESPACE, f"{PHONE}:{space_id}:{kind}").hex


def _execute(connection: Any, query: str, params: tuple[Any, ...] = ()) -> Any:
    if isinstance(connection, sqlite3.Connection):
        query = query.replace("%s", "?")
    return connection.execute(query, params)


def _one(connection: Any, query: str, params: tuple[Any, ...] = ()) -> Any:
    return _execute(connection, query, params).fetchone()


def _verified_user_id(connection: Any) -> int:
    rows = _execute(
        connection, "SELECT user_id, verified_at FROM phone_verifications WHERE phone = %s", (PHONE,)
    ).fetchall()
    if not rows:
        raise DemoSetupError(
            "Номер 0500 ещё не подтверждён в MAX. Отправьте боту /auth или /phone, "
            "поделитесь своим контактом и повторите команду."
        )
    if len(rows) != 1:
        raise DemoSetupError("Номер 0500 привязан к нескольким MAX ID; подготовка остановлена.")
    try:
        verified_at = datetime.fromisoformat(str(rows[0][1]))
        if verified_at.tzinfo is None:
            verified_at = verified_at.replace(tzinfo=UTC)
    except (TypeError, ValueError) as error:
        raise DemoSetupError("Не удалось проверить время подтверждения номера 0500.") from error
    age = datetime.now(UTC) - verified_at.astimezone(UTC)
    if age < timedelta(minutes=-5) or age > timedelta(hours=24):
        raise DemoSetupError(
            "Контакт 0500 нужно подтвердить заново перед записью: /phone → «Поделиться номером»."
        )
    return int(rows[0][0])


def _space(
    connection: Any, user_id: int, now: str, *, allow_existing_hoa: bool,
) -> tuple[str, str, str, bool]:
    profiles = _execute(
        connection,
        "SELECT user_id, phone, space_id, protocol_filename FROM chairman_profiles "
        "WHERE user_id = %s OR phone = %s",
        (user_id, PHONE),
    ).fetchall()
    if profiles:
        if len(profiles) != 1 or int(profiles[0][0]) != user_id or profiles[0][1] != PHONE:
            raise DemoSetupError(
                "У аккаунта или номера 0500 есть несовместимый профиль председателя. "
                "Скрипт не меняет существующие полномочия."
            )
        space_id = str(profiles[0][2])
        real_profile = (space_id != _id("demo-space")
                        or profiles[0][3] != DEMO_PROFILE_FILENAME)
        if real_profile and not allow_existing_hoa:
            raise DemoSetupError(
                "У 0500 уже есть реальное ТСЖ. Чтобы добавить в него помеченные "
                "демоданные, повторите команду с --use-existing-hoa."
            )
        space = _one(connection, "SELECT name, address FROM hoa_spaces WHERE space_id = %s", (space_id,))
        if space is None:
            raise DemoSetupError("Профиль председателя ссылается на отсутствующее ТСЖ.")
        return space_id, str(space[0]), str(space[1]), real_profile

    space_id = _id("demo-space")
    existing = _one(connection, "SELECT name, address FROM hoa_spaces WHERE space_id = %s", (space_id,))
    if existing is not None and tuple(existing) != (DEMO_SPACE_NAME, DEMO_ADDRESS):
        raise DemoSetupError("Идентификатор демонстрационного ТСЖ занят другой записью.")
    _execute(
        connection,
        "INSERT INTO hoa_spaces(space_id, name, address, created_at) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT(space_id) DO NOTHING",
        (space_id, DEMO_SPACE_NAME, DEMO_ADDRESS, now),
    )
    protocol_digest = hashlib.sha256(f"demo-profile:{user_id}:{space_id}".encode()).hexdigest()
    _execute(
        connection,
        "INSERT INTO chairman_profiles"
        "(phone, user_id, full_name, space_id, protocol_sha256, protocol_filename, verified_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (PHONE, user_id, "Демо Председатель", space_id, protocol_digest,
         DEMO_PROFILE_FILENAME, now),
    )
    return space_id, DEMO_SPACE_NAME, DEMO_ADDRESS, False


def _resident(connection: Any, user_id: int, space_id: str, now: str) -> str:
    resident_id = _id("resident", space_id)
    collision = _one(
        connection,
        "SELECT resident_id FROM residents WHERE space_id = %s AND unit = %s "
        "AND full_name = %s",
        (space_id, DEMO_UNIT, DEMO_NAME),
    )
    if collision is not None and collision[0] != resident_id:
        raise DemoSetupError("Демонстрационное помещение занято другой записью реестра.")
    current = _one(
        connection,
        "SELECT space_id, user_id, unit, full_name FROM residents WHERE resident_id = %s",
        (resident_id,),
    )
    if current is not None:
        if current[0] != space_id or current[1] not in (None, user_id):
            raise DemoSetupError("Демонстрационное помещение привязано к другому MAX ID.")
        _execute(
            connection,
            "UPDATE residents SET unit = %s, full_name = %s, user_id = %s, phone = %s, "
            "active = 1, updated_at = %s "
            "WHERE resident_id = %s",
            (DEMO_UNIT, DEMO_NAME, user_id, PHONE, now, resident_id),
        )
        return resident_id

    prefix = hashlib.sha256(f"demo-code:{resident_id}".encode()).hexdigest().upper()[:10]
    code_owner = _one(connection, "SELECT resident_id FROM residents WHERE code_prefix = %s", (prefix,))
    if code_owner is not None:
        raise DemoSetupError("Префикс демонстрационного кода занят другой записью.")
    _execute(
        connection,
        "INSERT INTO residents(resident_id, space_id, unit, full_name, area, share, "
        "ownership, code_prefix, code_secret, user_id, phone, active, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s)",
        (resident_id, space_id, DEMO_UNIT, DEMO_NAME, "52.4", "1/1", "собственность",
         prefix, secrets.token_hex(32), user_id, PHONE, now),
    )
    return resident_id


def _specialist(connection: Any, user_id: int, space_id: str, now: str) -> tuple[str, str]:
    preferred_id = _id("specialist", space_id)
    by_id = _one(
        connection,
        "SELECT space_id, phone FROM specialists WHERE specialist_id = %s", (preferred_id,)
    )
    if by_id is not None and tuple(by_id) != (space_id, PHONE):
        raise DemoSetupError("Идентификатор демонстрационного специалиста занят другой записью.")
    existing = _one(
        connection,
        "SELECT specialist_id, specialty, user_id FROM specialists "
        "WHERE space_id = %s AND phone = %s",
        (space_id, PHONE),
    )
    if existing is not None:
        specialist_id, specialty, linked_user = existing
        if linked_user not in (None, user_id):
            raise DemoSetupError("Специалист с номером 0500 привязан к другому MAX ID.")
        if specialty not in {"plumber", "electrician"}:
            raise DemoSetupError("У специалиста 0500 неизвестная специализация.")
        if linked_user is None:
            _execute(
                connection,
                "UPDATE specialists SET user_id = %s, updated_at = %s WHERE specialist_id = %s",
                (user_id, now, specialist_id),
            )
    else:
        specialist_id, specialty = preferred_id, "plumber"
        _execute(
            connection,
            "INSERT INTO specialists(specialist_id, space_id, specialty, full_name, phone, "
            "user_id, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (specialist_id, space_id, specialty, "Демо Мастер", PHONE, user_id, now, now),
        )
    hours = _one(
        connection, "SELECT count(*) FROM specialist_hours WHERE specialist_id = %s", (specialist_id,)
    )[0]
    if not hours:
        if specialist_id != preferred_id:
            raise DemoSetupError(
                "У существующего специалиста 0500 не заданы рабочие часы. "
                "Настройте их в кабинете председателя и повторите подготовку."
            )
        for weekday in range(7):
            _execute(
                connection,
                "INSERT INTO specialist_hours(specialist_id, weekday, start_hour, end_hour) "
                "VALUES (%s, %s, 10, 18)",
                (specialist_id, weekday),
            )
    return str(specialist_id), str(specialty)


def _ticket(
    connection: Any, *, space_id: str, resident_id: str, user_id: int,
    kind: str, title: str, description: str, status: str, priority: str,
    specialty: str, specialist_id: str | None, created_at: str, now: str,
) -> str:
    request_id = _id(kind, space_id)
    existing = _one(
        connection,
        "SELECT space_id, resident_id, user_id, title FROM service_requests WHERE request_id = %s",
        (request_id,),
    )
    if existing is not None:
        if (existing[0] != space_id or existing[1] != resident_id
                or int(existing[2]) != user_id or not str(existing[3]).startswith("ДЕМО ·")):
            raise DemoSetupError("Идентификатор демонстрационной заявки занят другой записью.")
        _execute(
            connection,
            "UPDATE service_requests SET title = %s, description = %s, status = %s, "
            "priority = %s, created_at = %s, updated_at = %s WHERE request_id = %s",
            (title, description, status, priority, created_at, now, request_id),
        )
    else:
        _execute(
            connection,
            "INSERT INTO service_requests(request_id, space_id, resident_id, user_id, title, "
            "description, status, priority, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (request_id, space_id, resident_id, user_id, title, description, status,
             priority, created_at, now),
        )
    _execute(
        connection,
        "INSERT INTO request_assignments(request_id, target, specialty, specialist_id, "
        "source, assigned_at) VALUES (%s, %s, %s, %s, 'recording_demo', %s) "
        "ON CONFLICT(request_id) DO UPDATE SET target = excluded.target, "
        "specialty = excluded.specialty, specialist_id = excluded.specialist_id, "
        "source = excluded.source, assigned_at = excluded.assigned_at",
        (request_id, "specialist" if specialist_id else "chairman", specialty,
         specialist_id, now),
    )
    return request_id


def _photo(connection: Any, request_id: str, content: bytes, now: str) -> None:
    if not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise DemoSetupError("Демонстрационное фото повреждено или имеет неверный формат PNG.")
    _execute(
        connection,
        "INSERT INTO request_photos(request_id, position, mime_type, content, created_at) "
        "VALUES (%s, 1, 'image/png', %s, %s) "
        "ON CONFLICT(request_id, position) DO UPDATE SET mime_type = excluded.mime_type, "
        "content = excluded.content, created_at = excluded.created_at",
        (request_id, content, now),
    )


def _timezone(connection: Any, space_id: str, address: str) -> str:
    row = _one(connection, "SELECT timezone FROM hoa_timezones WHERE space_id = %s", (space_id,))
    if row:
        return str(row[0])
    if any(name in address.lower() for name in ("самар", "тольятти", "сызран", "новокуйбышев")):
        return "Europe/Samara"
    return "Europe/Moscow"


def _calendar_visit(
    connection: Any, request_id: str, specialist_id: str, timezone: str, now: str,
) -> tuple[str, int]:
    hours = _execute(
        connection,
        "SELECT weekday, start_hour, end_hour FROM specialist_hours WHERE specialist_id = %s",
        (specialist_id,),
    ).fetchall()
    by_day = {int(day): (int(start), int(end)) for day, start, end in hours}
    today = datetime.now(ZoneInfo(timezone)).date()
    for offset in range(1, 10):
        date_text = (today + timedelta(days=offset)).isoformat()
        window = by_day.get((today + timedelta(days=offset)).weekday())
        if window is None:
            continue
        taken = {
            int(row[0]) for row in _execute(
                connection,
                "SELECT visit_hour FROM visit_proposals WHERE specialist_id = %s "
                "AND visit_date = %s AND request_id <> %s "
                "AND status IN ('pending', 'accepted')",
                (specialist_id, date_text, request_id),
            ).fetchall()
        }
        work_hours = list(range(window[0], window[1]))
        preferred_hours = [hour for hour in work_hours if hour >= 10]
        preferred_hours += [hour for hour in work_hours if hour < 10]
        for hour in preferred_hours:
            if hour not in taken:
                _execute(
                    connection,
                    "INSERT INTO visit_proposals(request_id, specialist_id, visit_date, "
                    "visit_hour, timezone, token, status, created_at, responded_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, 'accepted', %s, %s) "
                    "ON CONFLICT(request_id) DO UPDATE SET specialist_id = excluded.specialist_id, "
                    "visit_date = excluded.visit_date, visit_hour = excluded.visit_hour, "
                    "timezone = excluded.timezone, token = excluded.token, status = 'accepted', "
                    "created_at = excluded.created_at, responded_at = excluded.responded_at",
                    (request_id, specialist_id, date_text, hour, timezone,
                     secrets.token_urlsafe(12), now, now),
                )
                return date_text, hour
    raise DemoSetupError(
        "У специалиста 0500 нет свободного часа в ближайшие 10 дней. "
        "Проверьте его рабочие часы и действующие выезды."
    )


def prepare_demo_connection(
    connection: Any, *, role: str, pipe_photo: bytes, light_photo: bytes,
    allow_existing_hoa: bool = False,
) -> dict[str, Any]:
    """Prepare all roles in the caller's transaction; safe to repeat for fixtures only."""
    if role not in {"owner", "specialist", "chairman"}:
        raise ValueError("Неизвестная роль видеосценария")
    if not isinstance(connection, sqlite3.Connection):
        _execute(connection, "SELECT pg_advisory_xact_lock(9872660500)")
    user_id = _verified_user_id(connection)
    now = datetime.now(UTC)
    now_text = now.isoformat()
    space_id, hoa_name, address, reused_real_hoa = _space(
        connection, user_id, now_text, allow_existing_hoa=allow_existing_hoa
    )
    resident_id = _resident(connection, user_id, space_id, now_text)
    specialist_id, specialty = _specialist(connection, user_id, space_id, now_text)

    fixture_ids = [_id(kind, space_id) for kind in ("owner-done", "specialist-open", "chairman-open", "calendar")]
    active_emergency = _one(
        connection,
        "SELECT q.request_id FROM service_requests q JOIN request_assignments a "
        "ON a.request_id = q.request_id WHERE a.specialist_id = %s "
        "AND q.priority = 'emergency' AND q.status <> 'done' "
        "AND q.request_id NOT IN (%s, %s, %s, %s) LIMIT 1",
        (specialist_id, *fixture_ids),
    )
    if active_emergency is not None:
        raise DemoSetupError(
            "У специалиста 0500 уже есть активная аварийная заявка. Согласование визита "
            "будет заблокировано; завершите её в интерфейсе и повторите подготовку."
        )
    matching_other_specialists = _one(
        connection,
        "SELECT count(*) FROM specialists WHERE space_id = %s AND specialty = %s "
        "AND user_id IS NOT NULL AND specialist_id <> %s",
        (space_id, specialty, specialist_id),
    )[0]

    specialist_issue = (
        ("ДЕМО · Не горит свет в прихожей", "В прихожей перестал работать свет. "
          "Нужно проверить светильник и выключатель.", light_photo)
        if specialty == "electrician" else
        ("ДЕМО · Подтекает соединение трубы", "Под раковиной подтекает соединение трубы. "
         "Нужен осмотр и ремонт сантехника.", pipe_photo)
    )
    owner_done = _ticket(
        connection, space_id=space_id, resident_id=resident_id, user_id=user_id,
        kind="owner-done", title="ДЕМО · Протечка под раковиной устранена",
        description="Под раковиной подтекало соединение. Специалист устранил протечку; "
                    "демонстрационная фотография приложена к заявке.",
        status="done", priority="planned", specialty="plumber", specialist_id=None,
        created_at=(now - timedelta(hours=4)).isoformat(), now=now_text,
    )
    specialist_open = _ticket(
        connection, space_id=space_id, resident_id=resident_id, user_id=user_id,
        kind="specialist-open", title=specialist_issue[0], description=specialist_issue[1],
        status="review", priority="urgent", specialty=specialty, specialist_id=specialist_id,
        created_at=(now - timedelta(hours=2)).isoformat(), now=now_text,
    )
    chairman_open = _ticket(
        connection, space_id=space_id, resident_id=resident_id, user_id=user_id,
        kind="chairman-open", title="ДЕМО · Проверить счётчик воды",
        description="Просьба организовать осмотр счётчика воды в демонстрационном помещении.",
        status="review", priority="planned", specialty="plumber", specialist_id=None,
        created_at=(now - timedelta(hours=1)).isoformat(), now=now_text,
    )
    calendar = _ticket(
        connection, space_id=space_id, resident_id=resident_id, user_id=user_id,
        kind="calendar", title="ДЕМО · Плановый осмотр оборудования",
        description="Согласованный плановый осмотр в демонстрационном помещении.",
        status="in_progress", priority="today", specialty=specialty, specialist_id=specialist_id,
        created_at=(now - timedelta(hours=3)).isoformat(), now=now_text,
    )
    _photo(connection, owner_done, pipe_photo, now_text)
    _photo(connection, specialist_open, specialist_issue[2], now_text)
    _execute(
        connection,
        "UPDATE visit_proposals SET status = 'cancelled', responded_at = %s "
        "WHERE request_id = %s AND status <> 'cancelled'",
        (now_text, specialist_open),
    )
    date_text, hour = _calendar_visit(
        connection, calendar, specialist_id, _timezone(connection, space_id, address), now_text
    )
    _execute(
        connection,
        "INSERT INTO authorization_states(user_id, role, step, updated_at) "
        "VALUES (%s, %s, 'recording_demo_awaiting_phone', %s) "
        "ON CONFLICT(user_id) DO UPDATE SET role = excluded.role, "
        "step = excluded.step, updated_at = excluded.updated_at",
        (user_id, role, now_text),
    )
    return {
        "user_id": user_id, "space_id": space_id, "hoa_name": hoa_name,
        "address": address, "unit": DEMO_UNIT, "specialty": specialty,
        "owner_done": owner_done, "specialist_open": specialist_open,
        "chairman_open": chairman_open, "calendar": calendar,
        "calendar_date": date_text, "calendar_hour": hour,
        "reused_real_hoa": reused_real_hoa,
        "matching_other_specialists": int(matching_other_specialists),
    }


def main(role: str) -> None:
    if role not in {"owner", "specialist", "chairman"}:
        raise ValueError("Неизвестная роль видеосценария")
    parser = argparse.ArgumentParser(description="Подготовка трёх ролей MAX для номера 0500")
    parser.add_argument(
        "--use-existing-hoa", action="store_true",
        help="добавить помеченные демоданные в уже существующее ТСЖ аккаунта 0500",
    )
    args = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith(("postgresql://", "postgres://")):
        raise SystemExit("Запускайте скрипт в контейнере bot с настроенным DATABASE_URL.")
    try:
        import psycopg

        pipe_photo = (ASSETS / "demo_pipe_leak.png").read_bytes()
        light_photo = (ASSETS / "demo_hall_light.png").read_bytes()
        with psycopg.connect(database_url, connect_timeout=5) as connection:
            result = prepare_demo_connection(
                connection, role=role, pipe_photo=pipe_photo, light_photo=light_photo,
                allow_existing_hoa=args.use_existing_hoa,
            )
    except (DemoSetupError, OSError) as error:
        raise SystemExit(f"Подготовка не применена: {error}") from error
    labels = {"owner": "СОБСТВЕННИК", "specialist": "СПЕЦИАЛИСТ", "chairman": "ПРЕДСЕДАТЕЛЬ"}
    print(f"Готово: {labels[role]} · подтверждённый MAX ID {result['user_id']}")
    print(f"ТСЖ: {result['hoa_name']} · {result['address']} · помещение {result['unit']}")
    if result["reused_real_hoa"]:
        print("Помеченные демоданные добавлены в существующее ТСЖ этого аккаунта.")
    if result["matching_other_specialists"]:
        print("В ТСЖ есть другие специалисты этой категории: новая заявка из /request "
              "может быть назначена им. Для ролика мастера используйте подготовленную заявку.")
    print("Специализация аккаунта 0500: " +
          ("электрик" if result["specialty"] == "electrician" else "сантехник"))
    print(f"Готовая заявка с фото: {result['owner_done'][:8]}")
    print(f"Активная заявка мастера с фото: {result['specialist_open'][:8]}")
    print(f"Плановый выезд: {result['calendar_date']} в {result['calendar_hour']}:00")
    if role == "chairman":
        print(f"Для формы добавления отдельного демонстрационного специалиста: {DEMO_SPECIALIST_PHONE}")
    print("Следующий шаг: /auth в MAX → поделиться номером → открыть кабинет подготовленной роли.")


if __name__ == "__main__":
    print("Запускайте scripts.demo_owner, scripts.demo_specialist или scripts.demo_chairman.", file=sys.stderr)
    raise SystemExit(2)
