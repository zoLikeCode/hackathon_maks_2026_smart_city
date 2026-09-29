from __future__ import annotations

import json
import ssl
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from src.phone_verification import verify_contact_signature


class MaxApiError(RuntimeError):
    """Ошибка запроса к MAX Bot API."""


class MaxApi:
    def __init__(
        self,
        token: str,
        base_url: str = "https://platform-api2.max.ru",
        ca_bundle: Path | None = None,
    ) -> None:
        if not token:
            raise ValueError("MAX_BOT_TOKEN не задан")
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._ssl_context = ssl.create_default_context()
        bundle = (
            ca_bundle
            or Path(__file__).resolve().parent.parent / "certs" / "russiantrustedca.pem"
        )
        if bundle.exists():
            self._ssl_context.load_verify_locations(cafile=str(bundle))

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        timeout: int = 40,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        if query:
            values = {key: value for key, value in query.items() if value is not None}
            if values:
                url = f"{url}?{urlencode(values)}"

        payload = None
        headers = {"Authorization": self._token, "Accept": "application/json"}
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(url, data=payload, headers=headers, method=method)
        try:
            with urlopen(  # noqa: S310
                request,
                timeout=timeout,
                context=self._ssl_context,
            ) as response:
                response_body = response.read().decode("utf-8")
        except HTTPError as error:
            details = error.read().decode("utf-8", errors="replace")
            raise MaxApiError(f"MAX API вернул HTTP {error.code}: {details}") from error
        except URLError as error:
            raise MaxApiError(f"Не удалось подключиться к MAX API: {error.reason}") from error
        except TimeoutError as error:
            raise MaxApiError("MAX API не ответил вовремя") from error

        if not response_body:
            return {}
        try:
            return json.loads(response_body)
        except json.JSONDecodeError as error:
            raise MaxApiError("MAX API вернул некорректный JSON") from error

    def get_me(self) -> dict[str, Any]:
        return self._request("GET", "/me")

    def set_commands(self, commands: list[dict[str, str]]) -> dict[str, Any]:
        return self._request("PATCH", "/me/commands", body={"commands": commands})

    def get_updates(
        self,
        *,
        marker: int | None = None,
        timeout: int = 30,
        limit: int = 100,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            "/updates",
            query={
                "marker": marker,
                "timeout": timeout,
                "limit": limit,
                "types": "bot_started,bot_added,bot_removed,bot_admin_permissions_changed,message_created,message_callback",
            },
            timeout=timeout + 10,
        )

    def send_message(
        self,
        text: str,
        *,
        user_id: int | None = None,
        chat_id: int | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if (user_id is None) == (chat_id is None):
            raise ValueError("Нужно указать ровно один параметр: user_id или chat_id")
        body: dict[str, Any] = {"text": text}
        if attachments is not None:
            body["attachments"] = attachments
        return self._request(
            "POST",
            "/messages",
            query={"user_id": user_id, "chat_id": chat_id},
            body=body,
        )

    def get_chat(self, chat_id: int) -> dict[str, Any]:
        return self._request("GET", f"/chats/{chat_id}", timeout=10)

    def leave_chat(self, chat_id: int) -> None:
        result = self._request("DELETE", f"/chats/{chat_id}/members/me", timeout=10)
        if result.get("success") is not True:
            raise MaxApiError(f"MAX не подтвердил выход из чата: {result.get('message') or 'неизвестная ошибка'}")

    def verify_contact(self, vcf_info: str, signature: str) -> bool:
        """Проверяет, что контакт отправлен кнопкой request_contact этого бота."""
        return verify_contact_signature(self._token, vcf_info, signature)

    def answer_callback(self, callback_id: str, notification: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if notification:
            body["notification"] = notification
        return self._request(
            "POST",
            "/answers",
            query={"callback_id": callback_id},
            body=body,
        )

    def download_attachment(self, url: str, *, max_bytes: int = 40 * 1024 * 1024) -> bytes:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise MaxApiError("MAX передал некорректную ссылку на файл")
        request = Request(url, headers={"Accept": "application/octet-stream"}, method="GET")
        size_error = f"Файл превышает допустимый размер {max_bytes // (1024 * 1024)} МБ"
        try:
            with urlopen(  # noqa: S310
                request,
                timeout=90,
                context=self._ssl_context,
            ) as response:
                declared_size = response.headers.get("Content-Length")
                if declared_size and int(declared_size) > max_bytes:
                    raise MaxApiError(size_error)
                content = response.read(max_bytes + 1)
        except HTTPError as error:
            raise MaxApiError(f"MAX не отдал файл: HTTP {error.code}") from error
        except URLError as error:
            raise MaxApiError(f"Не удалось скачать файл из MAX: {error.reason}") from error
        except TimeoutError as error:
            raise MaxApiError("MAX не отдал файл вовремя") from error
        if len(content) > max_bytes:
            raise MaxApiError(size_error)
        return content
