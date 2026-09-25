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
from threading import Thread
from typing import Any
from urllib.parse import parse_qsl, urlparse

from src.auth_store import ChairmanAuthorizationRepository
from src.phone_verification import masked_phone

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
        host: str = "0.0.0.0",
        port: int = 8080,
        static_dir: Path | None = None,
    ) -> None:
        self._repository = repository
        self._bot_token = bot_token
        self._host = host
        self._port = port
        self._static_dir = static_dir or Path(__file__).resolve().parent.parent / "webapp"
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        repository = self._repository
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

            def _profile(self) -> None:
                authorization = self.headers.get("Authorization", "")
                if not authorization.startswith("tma "):
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "Откройте профиль через MAX"})
                    return
                try:
                    user = validate_max_init_data(authorization[4:], bot_token)
                except MiniAppAuthorizationError as error:
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": str(error)})
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
                self._static(path)

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
