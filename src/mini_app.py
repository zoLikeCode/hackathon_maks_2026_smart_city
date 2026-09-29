from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from collections import Counter
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from typing import Any
from urllib.parse import parse_qsl, unquote, urlparse

from src.auth_store import ChairmanAuthorizationRepository
from src.gigachat_api import GigaChatApi, GigaChatApiError
from src.hoa_store import HoaStore, timezone_label
from src.max_api import MaxApi, MaxApiError
from src.owner_registry import RegistryError, RegistryResult, parse_registry_with_gigachat
from src.phone_verification import ContactVerificationError, masked_phone
from src.request_notifications import notify_request_changes
from src.verification_store import PhoneVerificationRepository

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
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
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
        hoa_store: HoaStore | None = None,
        phone_store: PhoneVerificationRepository | None = None,
        gigachat: GigaChatApi | None = None,
        max_api: MaxApi | None = None,
        host: str = "0.0.0.0",
        port: int = 8080,
        static_dir: Path | None = None,
    ) -> None:
        self._repository = repository
        self._bot_token = bot_token
        self._hoa_store = hoa_store
        self._phone_store = phone_store
        self._gigachat = gigachat
        self._max_api = max_api
        self._pending_registry: dict[int, tuple[str, RegistryResult, float]] = {}
        self._registry_lock = Lock()
        self._host = host
        self._port = port
        self._static_dir = static_dir or Path(__file__).resolve().parent.parent / "webapp"
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        repository = self._repository
        bot_token = self._bot_token
        hoa_store = self._hoa_store
        phone_store = self._phone_store
        gigachat = self._gigachat
        max_api = self._max_api
        pending_registry = self._pending_registry
        registry_lock = self._registry_lock
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
                    "style-src 'self'; img-src 'self' data: blob: https:; connect-src 'self'",
                )
                self.end_headers()

            def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self._headers(status, "application/json; charset=utf-8")
                self.wfile.write(body)

            def _user_id(self) -> int | None:
                authorization = self.headers.get("Authorization", "")
                if not authorization.startswith("tma "):
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "Откройте профиль через MAX"})
                    return None
                try:
                    user = validate_max_init_data(authorization[4:], bot_token)
                except MiniAppAuthorizationError as error:
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": str(error)})
                    return None
                return int(user["id"])

            def _chairman(self, user_id: int) -> Any | None:
                profile = repository.get_profile_by_user_id(user_id)
                if profile is None:
                    self._json(
                        HTTPStatus.FORBIDDEN,
                        {"error": "Доступно только подтверждённому председателю ТСЖ"},
                    )
                return profile

            def _profile(self) -> None:
                user_id = self._user_id()
                if user_id is None:
                    return
                profile = repository.get_profile_by_user_id(user_id)
                if profile is None:
                    verification = phone_store.get(user_id) if phone_store else None
                    specialist_spaces = (
                        hoa_store.specialist_memberships(user_id, verification.phone)
                        if hoa_store and verification else []
                    )
                    if specialist_spaces:
                        self._json(HTTPStatus.OK, {
                            "role": "specialist", "full_name": specialist_spaces[0]["full_name"],
                            "hoa_name": specialist_spaces[0]["hoa_name"],
                            "address": specialist_spaces[0]["address"],
                            "specialty": specialist_spaces[0]["specialty"],
                            "phone": masked_phone(verification.phone),
                            "memberships": specialist_spaces,
                        })
                        return
                    memberships = hoa_store.memberships(user_id) if hoa_store else []
                    if not memberships:
                        self._json(HTTPStatus.FORBIDDEN, {"error": "Сначала войдите через чат с ботом"})
                        return
                    self._json(HTTPStatus.OK, {
                        "role": "owner", "full_name": memberships[0]["full_name"],
                        "hoa_name": memberships[0]["hoa_name"],
                        "address": memberships[0]["address"], "memberships": memberships,
                    })
                    return
                self._json(
                    HTTPStatus.OK,
                    {
                        "role": "chairman",
                        "full_name": profile.full_name,
                        "hoa_name": profile.hoa_name,
                        "address": profile.address,
                        "phone": masked_phone(profile.phone),
                        "space_id": profile.space_id,
                        "protocol_filename": profile.protocol_filename,
                        "verified_at": profile.verified_at,
                        "timezone": hoa_store.get_space_timezone(profile.space_id) if hoa_store else "Europe/Moscow",
                        "group_chat": hoa_store.get_group_chat(profile.space_id) if hoa_store else None,
                    },
                )

            def _read_body(self, maximum: int) -> bytes | None:
                try:
                    size = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    size = -1
                if size < 1 or size > maximum:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "Недопустимый размер запроса"})
                    return None
                return self.rfile.read(size)

            def _read_json(self) -> dict[str, Any] | None:
                raw = self._read_body(8192)
                if raw is None:
                    return None
                try:
                    data = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    data = None
                if not isinstance(data, dict):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "Нужен JSON-объект"})
                    return None
                return data

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
                parts = path.strip("/").split("/")
                if len(parts) == 5 and parts[:2] == ["api", "requests"] and parts[3] == "photos":
                    user_id = self._user_id()
                    if user_id is None:
                        return
                    try:
                        position = int(parts[4])
                    except ValueError:
                        position = 0
                    profile = repository.get_profile_by_user_id(user_id)
                    verification = phone_store.get(user_id) if phone_store else None
                    photo = hoa_store.get_request_photo(
                        user_id, parts[2], position,
                        chairman_space_id=profile.space_id if profile else "",
                        specialist_phone=verification.phone if verification else "",
                    ) if hoa_store else None
                    if photo is None:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "Фотография не найдена"})
                        return
                    self._headers(HTTPStatus.OK, photo[0])
                    self.wfile.write(photo[1])
                    return
                if path == "/api/profile":
                    self._profile()
                    return
                if path == "/api/residents":
                    user_id = self._user_id()
                    if user_id is None:
                        return
                    profile = self._chairman(user_id)
                    if profile is not None and hoa_store is not None:
                        self._json(HTTPStatus.OK, {"residents": hoa_store.list_residents(profile.space_id)})
                    return
                if path == "/api/specialists":
                    user_id = self._user_id()
                    if user_id is None:
                        return
                    profile = self._chairman(user_id)
                    if profile is not None and hoa_store is not None:
                        self._json(HTTPStatus.OK, {"specialists": hoa_store.list_specialists(profile.space_id)})
                    return
                if path == "/api/requests":
                    user_id = self._user_id()
                    if user_id is None:
                        return
                    profile = repository.get_profile_by_user_id(user_id)
                    memberships = hoa_store.memberships(user_id) if hoa_store else []
                    verification = phone_store.get(user_id) if phone_store else None
                    specialist_spaces = (
                        hoa_store.specialist_memberships(user_id, verification.phone)
                        if hoa_store and verification else []
                    )
                    if not profile and not memberships and not specialist_spaces:
                        self._json(HTTPStatus.FORBIDDEN, {"error": "Сначала войдите через чат"})
                    elif hoa_store is not None:
                        if profile:
                            requests = hoa_store.list_requests(space_id=profile.space_id)
                        elif specialist_spaces and verification:
                            requests = hoa_store.list_specialist_requests(user_id, verification.phone)
                        else:
                            requests = hoa_store.list_requests(user_id=user_id)
                        chairman_names: dict[str, str] = {}
                        for request in requests:
                            space_id = request["space_id"]
                            if space_id not in chairman_names:
                                chairman = repository.get_profile_by_space_id(space_id)
                                chairman_names[space_id] = chairman.full_name if chairman else "Председатель"
                            request["chairman_name"] = chairman_names[space_id]
                        self._json(HTTPStatus.OK, {"requests": requests})
                    return
                self._static(path)

            def do_POST(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                parts = path.strip("/").split("/")
                if len(parts) == 4 and parts[:2] == ["api", "requests"] and parts[3] == "visit":
                    user_id = self._user_id()
                    if user_id is None:
                        return
                    verification = phone_store.get(user_id) if phone_store else None
                    if not verification or not hoa_store:
                        self._json(HTTPStatus.FORBIDDEN, {"error": "Подтвердите номер специалиста"})
                        return
                    try:
                        proposal = hoa_store.request_visit_access(user_id, verification.phone, parts[2])
                    except ValueError as error:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                        return
                    buttons = [{"type": "inline_keyboard", "payload": {"buttons": [
                        [{"type": "callback", "text": "Выбрать время", "payload": f"visit:days:{parts[2]}:{proposal['token']}:0"}],
                        [{"type": "callback", "text": "Отказаться", "payload": f"visit:decline:{parts[2]}:{proposal['token']}"}],
                    ]}}]
                    try:
                        if max_api is None:
                            raise MaxApiError("MAX API недоступен")
                        max_api.send_message(
                            f"Специалист просит разрешить доступ в квартиру по заявке №{parts[2][:8]} «{proposal['title']}».\n"
                            f"Выберите свободный час из его расписания на ближайшие 10 дней "
                            f"(местное время ТСЖ: {timezone_label(proposal['timezone'])}), затем подтвердите визит.",
                            user_id=int(proposal["owner_user_id"]), attachments=buttons,
                        )
                    except MaxApiError:
                        logger.exception("Не удалось доставить запрос доступа %s", parts[2])
                        hoa_store.cancel_visit_proposal(parts[2], proposal["token"])
                        self._json(HTTPStatus.BAD_GATEWAY, {"error": "Не удалось отправить запрос собственнику в MAX"})
                        return
                    self._json(HTTPStatus.CREATED, {"sent": True})
                    return
                if path == "/api/specialists":
                    user_id = self._user_id()
                    if user_id is None:
                        return
                    profile = self._chairman(user_id)
                    if profile is None:
                        return
                    data = self._read_json()
                    if data is None:
                        return
                    if hoa_store is None:
                        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Хранилище недоступно"})
                        return
                    try:
                        created = hoa_store.create_specialist(
                            profile.space_id, str(data.get("specialty") or ""),
                            str(data.get("full_name") or ""), str(data.get("phone") or ""),
                        )
                    except (ValueError, ContactVerificationError) as error:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                        return
                    self._json(HTTPStatus.CREATED, created)
                    return
                if path == "/api/registry":
                    user_id = self._user_id()
                    if user_id is None:
                        return
                    profile = self._chairman(user_id)
                    if profile is None:
                        return
                    if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/pdf":
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "Отправьте PDF-файл"})
                        return
                    content = self._read_body(15 * 1024 * 1024)
                    if content is None:
                        return
                    digest = hashlib.sha256(content).hexdigest()
                    confirmation = self.headers.get("X-Confirm-Registry")
                    try:
                        if confirmation:
                            with registry_lock:
                                pending = pending_registry.get(user_id)
                                if (pending is None or pending[0] != digest
                                        or confirmation != digest or pending[2] < time.time()):
                                    self._json(HTTPStatus.CONFLICT, {"error": "Предварительная проверка истекла. Проверьте файл ещё раз"})
                                    return
                                result = pending[1]
                        else:
                            if gigachat is None:
                                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "GigaChat временно недоступен"})
                                return
                            filename = unquote(self.headers.get("X-File-Name", "registry.pdf"))[:180]
                            result = parse_registry_with_gigachat(gigachat, content, filename)
                            with registry_lock:
                                pending_registry[user_id] = (digest, result, time.time() + 1800)
                            self._json(HTTPStatus.OK, {
                                "preview": True, "address": result.address,
                                "resident_count": len(result.residents),
                                "skipped_units": result.skipped_units,
                                "residents": [entry.__dict__ for entry in result.residents],
                                "digest": digest,
                            })
                            return
                        filename = unquote(self.headers.get("X-File-Name", "registry.pdf"))[:180]
                        if hoa_store is None:
                            raise RuntimeError("Хранилище реестра недоступно")
                        hoa_store.import_registry(
                            profile.space_id, filename, content,
                            result.residents, result.skipped_units,
                        )
                        with registry_lock:
                            pending_registry.pop(user_id, None)
                        self._json(HTTPStatus.OK, {
                            "imported": len(result.residents), "skipped_units": result.skipped_units,
                        })
                    except RegistryError as error:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                    except GigaChatApiError:
                        logger.exception("GigaChat не разобрал реестр")
                        self._json(HTTPStatus.BAD_GATEWAY, {"error": "GigaChat не смог обработать PDF. Попробуйте ещё раз"})
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "Метод недоступен"})

            def do_PATCH(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                parts = path.strip("/").split("/")
                if path == "/api/group-chat":
                    user_id = self._user_id()
                    if user_id is None:
                        return
                    profile = self._chairman(user_id)
                    if profile is None or hoa_store is None:
                        return
                    data = self._read_json()
                    if data is None:
                        return
                    if set(data) != {"link"} or not isinstance(data["link"], str):
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "Укажите ссылку на чат MAX"})
                        return
                    try:
                        group_chat = hoa_store.set_group_chat_link(profile.space_id, data["link"])
                    except ValueError as error:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                        return
                    self._json(HTTPStatus.OK, {"group_chat": group_chat})
                    return
                if path == "/api/hoa-timezone":
                    user_id = self._user_id()
                    if user_id is None:
                        return
                    profile = self._chairman(user_id)
                    if profile is None or hoa_store is None:
                        return
                    data = self._read_json()
                    if data is None:
                        return
                    try:
                        hoa_store.set_space_timezone(profile.space_id, str(data.get("timezone") or ""))
                    except ValueError as error:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                        return
                    self._json(HTTPStatus.OK, {"timezone": hoa_store.get_space_timezone(profile.space_id)})
                    return
                if len(parts) == 3 and parts[:2] == ["api", "specialists"]:
                    user_id = self._user_id()
                    if user_id is None:
                        return
                    if hoa_store is None:
                        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Хранилище недоступно"})
                        return
                    data = self._read_json()
                    if data is None:
                        return
                    profile = self._chairman(user_id)
                    if profile is None:
                        return
                    try:
                        if set(data) != {"specialty", "full_name", "phone", "work_hours"}:
                            raise ValueError("Укажите данные специалиста и часы работы")
                        updated = hoa_store.update_specialist(
                            profile.space_id, parts[2], specialty=str(data["specialty"]),
                            full_name=str(data["full_name"]), phone=str(data["phone"]),
                            hours=data["work_hours"],
                        )
                        if updated is None:
                            self._json(HTTPStatus.NOT_FOUND, {"error": "Специалист не найден"})
                        else:
                            self._json(HTTPStatus.OK, {"specialist": updated})
                    except (ValueError, ContactVerificationError) as error:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                    return
                if len(parts) != 3 or parts[:2] != ["api", "requests"] or not parts[2]:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "Метод недоступен"})
                    return
                user_id = self._user_id()
                if user_id is None:
                    return
                profile = repository.get_profile_by_user_id(user_id)
                verification = phone_store.get(user_id) if phone_store else None
                specialist_spaces = (
                    hoa_store.specialist_memberships(user_id, verification.phone)
                    if hoa_store and verification else []
                )
                if profile is None and not specialist_spaces:
                    self._json(HTTPStatus.FORBIDDEN, {"error": "Доступно председателю или назначенному специалисту"})
                    return
                data = self._read_json()
                if data is None:
                    return
                allowed = {"status", "priority", "specialist_id"} if profile else {"status", "priority"}
                if not data or not set(data).issubset(allowed):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "Укажите допустимое изменение заявки"})
                    return
                try:
                    request_id = parts[2]
                    changes = {key: str(value) for key, value in data.items()}
                    if profile:
                        before = hoa_store.get_request_notification_context(
                            profile.space_id, request_id,
                        ) if hoa_store else None
                        changed = hoa_store.set_request_controls(
                            profile.space_id, request_id, **changes,
                        ) if hoa_store else False
                        after = hoa_store.get_request_notification_context(
                            profile.space_id, request_id,
                        ) if changed and hoa_store else None
                    else:
                        before = None
                        if hoa_store:
                            for space in specialist_spaces:
                                before = hoa_store.get_request_notification_context(
                                    space["space_id"], request_id,
                                )
                                if before is not None:
                                    break
                        changed = hoa_store.set_specialist_request_controls(
                            user_id, verification.phone, request_id, **changes,
                        ) if hoa_store and verification else False
                        after = hoa_store.get_request_notification_context(
                            space["space_id"], request_id,
                        ) if changed and hoa_store and before else None
                except ValueError as error:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                    return
                if not changed:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "Заявка не найдена"})
                    return
                chairman = repository.get_profile_by_space_id(after["space_id"]) if after else None
                group_chat = hoa_store.get_group_chat(after["space_id"]) if after and hoa_store else None
                warnings = notify_request_changes(
                    max_api, request_id, before, after,
                    chairman_user_id=chairman.user_id if chairman else None,
                    group_chat_id=group_chat["chat_id"] if group_chat else None,
                ) if before and after else []
                self._json(HTTPStatus.OK, {"updated": True, "request": after, "warnings": warnings})

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
