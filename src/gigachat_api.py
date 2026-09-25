from __future__ import annotations

import json
import mimetypes
import os
import ssl
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class GigaChatApiError(RuntimeError):
    """Ошибка запроса к GigaChat API."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class GigaChatConfig:
    credentials: str
    scope: str = "GIGACHAT_API_PERS"
    model: str = "GigaChat-3-Ultra"
    base_url: str = "https://api.giga.chat/v1"
    oauth_url: str = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    timeout: int = 90
    ca_bundle: Path | None = None

    @classmethod
    def from_env(cls) -> GigaChatConfig:
        credentials = os.getenv("GIGACHAT_CREDENTIALS", "").strip()
        if not credentials:
            raise ValueError("GIGACHAT_CREDENTIALS не задан")

        default_bundle = (
            Path(__file__).resolve().parent.parent / "certs" / "russiantrustedca.pem"
        )
        configured_bundle = os.getenv("GIGACHAT_CA_BUNDLE", "").strip()
        ca_bundle = Path(configured_bundle) if configured_bundle else default_bundle

        return cls(
            credentials=credentials,
            scope=os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"),
            model=os.getenv("GIGACHAT_MODEL", "GigaChat-3-Ultra"),
            base_url=os.getenv("GIGACHAT_BASE_URL", "https://api.giga.chat/v1"),
            oauth_url=os.getenv(
                "GIGACHAT_OAUTH_URL",
                "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
            ),
            timeout=int(os.getenv("GIGACHAT_TIMEOUT", "90")),
            ca_bundle=ca_bundle,
        )


class GigaChatApi:
    """Минимальный REST-клиент GigaChat с кешированием access token."""

    _SUPPORTED_IMAGE_TYPES = {
        ".bmp": "image/bmp",
        ".jpeg": "image/jpeg",
        ".jpg": "image/jpeg",
        ".png": "image/png",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
    }
    _MAX_IMAGE_BYTES = 15 * 1024 * 1024
    _SUPPORTED_DOCUMENT_TYPES = {
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".pdf": "application/pdf",
    }
    _MAX_DOCUMENT_BYTES = 40 * 1024 * 1024

    def __init__(self, config: GigaChatConfig) -> None:
        self.config = config
        self._base_url = config.base_url.rstrip("/")
        self._ssl_context = ssl.create_default_context()
        if config.ca_bundle is not None and config.ca_bundle.exists():
            self._ssl_context.load_verify_locations(cafile=str(config.ca_bundle))

        self._access_token: str | None = None
        self._access_token_expires_at = 0.0

    @classmethod
    def from_env(cls) -> GigaChatApi:
        return cls(GigaChatConfig.from_env())

    def _request_bytes(
        self,
        request: Request,
        *,
        timeout: int | None = None,
    ) -> bytes:
        try:
            with urlopen(  # noqa: S310
                request,
                timeout=timeout or self.config.timeout,
                context=self._ssl_context,
            ) as response:
                return response.read()
        except HTTPError as error:
            details = error.read().decode("utf-8", errors="replace")
            raise GigaChatApiError(
                f"GigaChat API вернул HTTP {error.code}: {details}",
                status=error.code,
            ) from error
        except URLError as error:
            raise GigaChatApiError(
                f"Не удалось подключиться к GigaChat API: {error.reason}"
            ) from error
        except TimeoutError as error:
            raise GigaChatApiError("GigaChat API не ответил вовремя") from error

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        request = Request(url, data=body, headers=headers or {}, method=method)
        raw = self._request_bytes(request, timeout=timeout)
        if not raw:
            return {}
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GigaChatApiError("GigaChat API вернул некорректный JSON") from error
        if not isinstance(result, dict):
            raise GigaChatApiError("GigaChat API вернул неожиданный формат ответа")
        return result

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        now = time.time()
        if (
            not force_refresh
            and self._access_token
            and now < self._access_token_expires_at - 30
        ):
            return self._access_token

        body = urlencode({"scope": self.config.scope}).encode("utf-8")
        result = self._request_json(
            "POST",
            self.config.oauth_url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Basic {self.config.credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
                "RqUID": str(uuid.uuid4()),
            },
            body=body,
        )

        token = result.get("access_token")
        expires_at = result.get("expires_at")
        if not isinstance(token, str) or not token:
            raise GigaChatApiError("GigaChat API не вернул access_token")

        if isinstance(expires_at, (int, float)):
            expiry = float(expires_at)
            if expiry > 10_000_000_000:
                expiry /= 1000
        else:
            expiry = now + 25 * 60

        self._access_token = token
        self._access_token_expires_at = expiry
        return token

    def _authorized_json(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        retry_auth: bool = True,
    ) -> dict[str, Any]:
        token = self.get_access_token()
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }
        if content_type:
            headers["Content-Type"] = content_type
        try:
            return self._request_json(
                method,
                f"{self._base_url}{path}",
                headers=headers,
                body=body,
            )
        except GigaChatApiError as error:
            if error.status != 401 or not retry_auth:
                raise
            self.get_access_token(force_refresh=True)
            return self._authorized_json(
                method,
                path,
                body=body,
                content_type=content_type,
                retry_auth=False,
            )

    def list_models(self) -> list[dict[str, Any]]:
        result = self._authorized_json("GET", "/models")
        models = result.get("data")
        if not isinstance(models, list):
            raise GigaChatApiError("GigaChat API не вернул список моделей")
        return [item for item in models if isinstance(item, dict)]

    def chat(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        payload = json.dumps(
            {
                "model": self.config.model,
                "messages": messages,
                "stream": False,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        return self._authorized_json(
            "POST",
            "/chat/completions",
            body=payload,
            content_type="application/json",
        )

    @staticmethod
    def _answer_text(result: dict[str, Any]) -> str:
        choices = result.get("choices")
        if not isinstance(choices, list) or not choices:
            raise GigaChatApiError("GigaChat API не вернул варианты ответа")
        first = choices[0]
        if not isinstance(first, dict):
            raise GigaChatApiError("GigaChat API вернул некорректный вариант ответа")
        message = first.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise GigaChatApiError("GigaChat API не вернул текст ответа")
        return content

    def generate_text(self, prompt: str, *, system_prompt: str | None = None) -> str:
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        return self._answer_text(self.chat(messages))

    @staticmethod
    def _multipart_file(
        content: bytes,
        filename: str,
        content_type: str,
    ) -> tuple[bytes, str]:
        boundary = f"----codex-{uuid.uuid4().hex}"
        safe_filename = Path(filename).name.replace('"', "")
        lines = [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="purpose"\r\n\r\n',
            b"general\r\n",
            f"--{boundary}\r\n".encode(),
            (
                'Content-Disposition: form-data; name="file"; '
                f'filename="{safe_filename}"\r\n'
            ).encode("utf-8"),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            content,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
        return b"".join(lines), f"multipart/form-data; boundary={boundary}"

    def upload_file(
        self,
        content: bytes,
        filename: str,
        *,
        content_type: str | None = None,
    ) -> str:
        mime_type = content_type or mimetypes.guess_type(filename)[0]
        if mime_type is None:
            mime_type = "application/octet-stream"
        body, multipart_type = self._multipart_file(content, filename, mime_type)
        result = self._authorized_json(
            "POST",
            "/files",
            body=body,
            content_type=multipart_type,
        )
        file_id = result.get("id")
        if not isinstance(file_id, str) or not file_id:
            raise GigaChatApiError("GigaChat API не вернул идентификатор файла")
        return file_id

    def delete_file(self, file_id: str) -> None:
        self._authorized_json("POST", f"/files/{file_id}/delete")

    def analyze_image(self, content: bytes, filename: str, *, prompt: str) -> str:
        suffix = Path(filename).suffix.lower()
        content_type = self._SUPPORTED_IMAGE_TYPES.get(suffix)
        if content_type is None:
            supported = ", ".join(sorted(self._SUPPORTED_IMAGE_TYPES))
            raise ValueError(f"Неподдерживаемый формат изображения. Допустимы: {supported}")
        if not content:
            raise ValueError("Изображение пустое")
        if len(content) > self._MAX_IMAGE_BYTES:
            raise ValueError("Размер изображения превышает 15 МБ")

        file_id = self.upload_file(
            content,
            filename,
            content_type=content_type,
        )
        try:
            result = self.chat(
                [
                    {
                        "role": "user",
                        "content": prompt,
                        "attachments": [file_id],
                    }
                ]
            )
            return self._answer_text(result)
        finally:
            self.delete_file(file_id)

    def analyze_image_path(self, path: Path, *, prompt: str) -> str:
        return self.analyze_image(path.read_bytes(), path.name, prompt=prompt)

    def analyze_document(self, content: bytes, filename: str, *, prompt: str) -> str:
        suffix = Path(filename).suffix.lower()
        content_type = self._SUPPORTED_DOCUMENT_TYPES.get(suffix)
        if content_type is None:
            supported = ", ".join(sorted(self._SUPPORTED_DOCUMENT_TYPES))
            raise ValueError(f"Неподдерживаемый формат документа. Допустимы: {supported}")
        if not content:
            raise ValueError("Документ пустой")
        if len(content) > self._MAX_DOCUMENT_BYTES:
            raise ValueError("Размер документа превышает 40 МБ")

        file_id = self.upload_file(content, filename, content_type=content_type)
        try:
            result = self.chat(
                [
                    {
                        "role": "user",
                        "content": prompt,
                        "attachments": [file_id],
                    }
                ]
            )
            return self._answer_text(result)
        finally:
            self.delete_file(file_id)

    def analyze_document_path(self, path: Path, *, prompt: str) -> str:
        return self.analyze_document(path.read_bytes(), path.name, prompt=prompt)
