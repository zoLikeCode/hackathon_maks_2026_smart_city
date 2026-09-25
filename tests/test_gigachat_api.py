from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src.gigachat_api import GigaChatApi, GigaChatConfig


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class StubImageApi(GigaChatApi):
    def __init__(self) -> None:
        super().__init__(GigaChatConfig(credentials="test", ca_bundle=Path("missing")))
        self.deleted: list[str] = []
        self.messages: list[dict] = []

    def upload_file(
        self,
        content: bytes,
        filename: str,
        *,
        content_type: str | None = None,
    ) -> str:
        self.uploaded = (content, filename, content_type)
        return "file-123"

    def chat(self, messages: list[dict]) -> dict:
        self.messages = messages
        return {"choices": [{"message": {"content": "На фото протечка"}}]}

    def delete_file(self, file_id: str) -> None:
        self.deleted.append(file_id)


class GigaChatApiTests(unittest.TestCase):
    def test_token_is_cached(self) -> None:
        api = GigaChatApi(GigaChatConfig(credentials="secret", ca_bundle=Path("missing")))
        response = FakeResponse(
            {
                "access_token": "token-123",
                "expires_at": int(time.time()) + 1800,
            }
        )

        with patch("src.gigachat_api.urlopen", return_value=response) as mocked:
            self.assertEqual(api.get_access_token(), "token-123")
            self.assertEqual(api.get_access_token(), "token-123")

        self.assertEqual(mocked.call_count, 1)

    def test_generate_text_uses_ultra_by_default(self) -> None:
        api = GigaChatApi(GigaChatConfig(credentials="secret", ca_bundle=Path("missing")))
        responses = [
            FakeResponse(
                {
                    "access_token": "token-123",
                    "expires_at": int(time.time()) + 1800,
                }
            ),
            FakeResponse({"choices": [{"message": {"content": "Ответ"}}]}),
        ]

        with patch("src.gigachat_api.urlopen", side_effect=responses) as mocked:
            self.assertEqual(api.generate_text("Привет"), "Ответ")

        chat_request = mocked.call_args_list[1].args[0]
        body = json.loads(chat_request.data.decode("utf-8"))
        self.assertEqual(body["model"], "GigaChat-3-Ultra")
        self.assertEqual(body["messages"][0]["content"], "Привет")

    def test_image_is_deleted_after_analysis(self) -> None:
        api = StubImageApi()

        answer = api.analyze_image(b"image-data", "photo.jpg", prompt="Что на фото?")

        self.assertEqual(answer, "На фото протечка")
        self.assertEqual(api.messages[0]["attachments"], ["file-123"])
        self.assertEqual(api.deleted, ["file-123"])

    def test_rejects_unsupported_image(self) -> None:
        api = StubImageApi()

        with self.assertRaisesRegex(ValueError, "Неподдерживаемый формат"):
            api.analyze_image(b"image-data", "photo.webp", prompt="Что на фото?")

    def test_pdf_is_deleted_after_analysis(self) -> None:
        api = StubImageApi()

        answer = api.analyze_document(b"pdf-data", "protocol.pdf", prompt="Проверь")

        self.assertEqual(answer, "На фото протечка")
        self.assertEqual(api.uploaded[2], "application/pdf")
        self.assertEqual(api.deleted, ["file-123"])


if __name__ == "__main__":
    unittest.main()
