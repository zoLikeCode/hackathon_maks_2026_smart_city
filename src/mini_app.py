from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from collections import Counter
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from urllib.parse import parse_qsl, urlparse

from src.auth_store import ChairmanAuthorizationRepository
from src.phone_verification import masked_phone
from src.request_store import PRIORITIES, STATUSES, RequestRepository, validate_scheduled_for

logger = logging.getLogger(__name__)


class MiniAppAuthorizationError(ValueError):
    """Стартовые данные mini app не прошли проверку MAX."""


def validate_max_init_data(
    init_data: str,
    bot_token: str,
    *,
    now: int | None = None,
    max_age_seconds: int = 3600,
) -> dict[str, Any]:
    if not bot_token:
        raise MiniAppAuthorizationError("Токен бота не настроен")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError as error:
        raise MiniAppAuthorizationError("Некорректные параметры MAX") from error
    counts = Counter(key for key, _value in pairs)
    if counts.get("hash") != 1 or any(count > 1 for count in counts.values()):
        raise MiniAppAuthorizationError("Некорректный набор параметров MAX")
    values = dict(pairs)
    original_hash = values.pop("hash")
    launch_params = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(
        secret_key,
        launch_params.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_hash, original_hash):
        raise MiniAppAuthorizationError("Подпись MAX недействительна")

    try:
        auth_date = int(values["auth_date"])
        user = json.loads(values["user"])
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise MiniAppAuthorizationError("MAX не передал данные пользователя") from error
    current_time = int(time.time()) if now is None else now
    if auth_date > current_time + 60 or current_time - auth_date > max_age_seconds:
        raise MiniAppAuthorizationError("Сессия MAX истекла")
    if not isinstance(user, dict):
        raise MiniAppAuthorizationError("Некорректный профиль пользователя MAX")
    user["id"] = user_id
    return user


class MiniAppServer:
    def __init__(
        self,
        repository: ChairmanAuthorizationRepository,
        bot_token: str,
        *,
        request_repository: RequestRepository | None = None,
        specialist_user_ids: set[int] | frozenset[int] | None = None,
        host: str = "0.0.0.0",
        port: int = 8080,
        static_dir: Path | None = None,
    ) -> None:
        self._repository = repository
        self._bot_token = bot_token
        self._request_repository = request_repository
        self._specialist_user_ids = frozenset(specialist_user_ids or ())
        self._host = host
        self._port = port
        self._static_dir = static_dir or Path(__file__).resolve().parent.parent / "webapp"
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        repository = self._repository
        request_repository = self._request_repository
        specialist_user_ids = self._specialist_user_ids
        bot_token = self._bot_token
        static_dir = self._static_dir

        class Handler(BaseHTTPRequestHandler):
            server_version = "SmartCityMiniApp/1.0"

            def log_message(self, message: str, *args: object) -> None:
                logger.info("mini-app: " + message, *args)

            def _headers(self, status: HTTPStatus, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'self' https://st.max.ru; "
                    "style-src 'self'; img-src 'self' data: https:; connect-src 'self'",
                )
                self.end_headers()

            def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self._headers(status, "application/json; charset=utf-8")
                self.wfile.write(body)

            def _signed_user(self) -> dict[str, Any] | None:
                authorization = self.headers.get("Authorization", "")
                if not authorization.startswith("tma "):
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "Откройте профиль через MAX"})
                    return None
                try:
                    return validate_max_init_data(authorization[4:], bot_token)
                except MiniAppAuthorizationError as error:
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": str(error)})
                    return None

            def _authorized(self) -> tuple[dict[str, Any], Any, str] | None:
                user = self._signed_user()
                if user is None:
                    return None
                profile = repository.get_profile_by_user_id(int(user["id"]))
                role = "specialist" if int(user["id"]) in specialist_user_ids else "chairman"
                if role == "specialist" or profile is not None:
                    return user, profile, role
                self._json(HTTPStatus.FORBIDDEN, {"error": "Доступ к мини-приложению не подтверждён"})
                return None

            def _profile(self) -> None:
                user = self._signed_user()
                if user is None:
                    return
                profile = repository.get_profile_by_user_id(int(user["id"]))
                if profile is None:
                    self._json(
                        HTTPStatus.FORBIDDEN,
                        {"error": "Профиль председателя ещё не подтверждён в чате"},
                    )
                    return
                self._json(
                    HTTPStatus.OK,
                    {
                        "role": "Председатель ТСЖ",
                        "full_name": profile.full_name,
                        "hoa_name": profile.hoa_name,
                        "address": profile.address,
                        "phone": masked_phone(profile.phone),
                        "space_id": profile.space_id,
                        "protocol_filename": profile.protocol_filename,
                        "verified_at": profile.verified_at,
                    },
                )

            def _session(self) -> None:
                authorized = self._authorized()
                if authorized is None:
                    return
                user, profile, role = authorized
                display_name = " ".join(
                    str(user.get(key) or "").strip()
                    for key in ("first_name", "last_name")
                ).strip()
                if not display_name:
                    display_name = str(user.get("name") or user.get("username") or "Пользователь")
                data: dict[str, Any] = {
                    "role": role,
                    "display_name": display_name,
                    "user_id": int(user["id"]),
                }
                if profile is not None:
                    data.update(
                        full_name=profile.full_name,
                        hoa_name=profile.hoa_name,
                        address=profile.address,
                        phone=masked_phone(profile.phone),
                        space_id=profile.space_id,
                        protocol_filename=profile.protocol_filename,
                        verified_at=profile.verified_at,
                    )
                    if role == "chairman":
                        data["display_name"] = profile.full_name
                self._json(HTTPStatus.OK, data)

            def _requests(self) -> None:
                authorized = self._authorized()
                if authorized is None:
                    return
                _user, profile, role = authorized
                if request_repository is None:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Заявки недоступны"})
                    return
                requests = (
                    request_repository.list_all()
                    if role == "specialist"
                    else request_repository.list_for_space(profile.space_id)
                )
                self._json(HTTPStatus.OK, {"requests": [item.as_dict() for item in requests]})

            def _body(self) -> dict[str, Any] | None:
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "Требуется application/json"})
                    return None
                try:
                    length = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    self._json(HTTPStatus.LENGTH_REQUIRED, {"error": "Укажите длину запроса"})
                    return None
                if length < 1:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "Пустой запрос"})
                    return None
                if length > 16_384:
                    self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Запрос слишком большой"})
                    return None
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "Некорректный JSON"})
                    return None
                if not isinstance(payload, dict):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "Ожидается объект JSON"})
                    return None
                return payload

            def _create_request(self) -> None:
                authorized = self._authorized()
                if authorized is None:
                    return
                user, profile, role = authorized
                if role != "chairman" or profile is None:
                    self._json(HTTPStatus.FORBIDDEN, {"error": "Создать заявку может председатель"})
                    return
                if request_repository is None:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Заявки недоступны"})
                    return
                payload = self._body()
                if payload is None:
                    return
                if set(payload) - {"title", "category", "description", "scheduled_for"}:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "Неизвестные поля заявки"})
                    return
                for key, limit in (("title", 160), ("category", 80), ("description", 4000)):
                    value = payload.get(key)
                    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": f"Некорректное поле {key}"})
                        return
                    payload[key] = value.strip()
                scheduled_for = payload.get("scheduled_for")
                if scheduled_for == "":
                    scheduled_for = None
                try:
                    scheduled_for = validate_scheduled_for(scheduled_for)
                except ValueError as error:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                    return
                item = request_repository.create(
                    space_id=profile.space_id,
                    created_by_user_id=int(user["id"]),
                    title=payload["title"],
                    category=payload["category"],
                    description=payload["description"],
                    address=profile.address,
                    scheduled_for=scheduled_for,
                )
                self._json(HTTPStatus.CREATED, {"request": item.as_dict()})

            def _update_request(self, request_id: str, action: str) -> None:
                authorized = self._authorized()
                if authorized is None:
                    return
                _user, _profile, role = authorized
                if role != "specialist":
                    self._json(HTTPStatus.FORBIDDEN, {"error": "Изменять заявки может специалист"})
                    return
                if request_repository is None:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Заявки недоступны"})
                    return
                payload = self._body()
                if payload is None:
                    return
                field = "scheduled_for" if action == "schedule" else action
                if set(payload) != {field}:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": f"Ожидается поле {field}"})
                    return
                value = payload[field]
                if action == "priority":
                    if not isinstance(value, str) or value not in PRIORITIES:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "Некорректный приоритет"})
                        return
                    item = request_repository.set_priority(request_id, value)
                elif action == "status":
                    if not isinstance(value, str) or value not in STATUSES:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "Некорректный статус"})
                        return
                    item = request_repository.set_status(request_id, value)
                else:
                    try:
                        scheduled_for = validate_scheduled_for(value)
                    except ValueError as error:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                        return
                    item = request_repository.set_schedule(request_id, scheduled_for)
                if item is None:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "Заявка не найдена"})
                    return
                self._json(HTTPStatus.OK, {"request": item.as_dict()})

            def _static(self, requested_path: str) -> None:
                files = {
                    "/": ("index.html", "text/html; charset=utf-8"),
                    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
                    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
                }
                item = files.get(requested_path)
                if item is None:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "Страница не найдена"})
                    return
                filename, content_type = item
                try:
                    body = (static_dir / filename).read_bytes()
                except OSError:
                    self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Файл недоступен"})
                    return
                self._headers(HTTPStatus.OK, content_type)
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                if path == "/health":
                    self._json(HTTPStatus.OK, {"status": "ok"})
                    return
                if path == "/api/profile":
                    self._profile()
                    return
                if path == "/api/session":
                    self._session()
                    return
                if path == "/api/requests":
                    self._requests()
                    return
                self._static(path)

            def do_POST(self) -> None:  # noqa: N802
                if urlparse(self.path).path == "/api/requests":
                    self._create_request()
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "Страница не найдена"})

            def do_PATCH(self) -> None:  # noqa: N802
                match = re.fullmatch(
                    r"/api/requests/([0-9a-fA-F-]{36})/(priority|status|schedule)",
                    urlparse(self.path).path,
                )
                if match is not None:
                    self._update_request(match.group(1), match.group(2))
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "Страница не найдена"})

        return Handler

    def start(self) -> None:
        if self._server is not None:
            return
        self._server = ThreadingHTTPServer((self._host, self._port), self._handler())
        self._thread = Thread(target=self._server.serve_forever, name="mini-app", daemon=True)
        self._thread.start()
        logger.info("Mini app слушает http://%s:%s", self._host, self._port)

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
