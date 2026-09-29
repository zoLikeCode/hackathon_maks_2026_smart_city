from __future__ import annotations

import json
from dataclasses import dataclass

from src.gigachat_api import GigaChatApi


@dataclass(frozen=True)
class RequestAnalysis:
    title: str
    description: str
    specialty: str


_PROMPT = """Ты диспетчер ТСЖ. Определи только тип специалиста для обращения собственника.
Верни только JSON без markdown: {"specialty":"plumber|electrician|other"}.
plumber — вода, трубы, сантехника, канализация, отопление;
electrician — электроснабжение, проводка, свет, розетки;
other — иные проблемы или недостаточно данных для уверенного выбора.
Если проблема может относиться к нескольким специалистам, выбери other,
чтобы решение принял председатель. Не исправляй и не пересказывай сообщение.
Сообщение собственника:\n"""

def _parse_specialty(answer: str) -> str:
    content = answer.strip()
    if content.startswith("```"):
        content = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(content)
    except json.JSONDecodeError as error:
        raise ValueError("GigaChat не смог определить специалиста. Попробуйте описать проблему подробнее") from error
    if not isinstance(data, dict):
        raise ValueError("GigaChat вернул некорректную категорию")
    specialty = data.get("specialty")
    if specialty not in {"plumber", "electrician", "other"}:
        raise ValueError("Не удалось определить нужного специалиста. Попробуйте ещё раз")
    return specialty


def classify_text(gigachat: GigaChatApi, message: str) -> RequestAnalysis:
    if len(message.strip()) < 5 or len(message) > 3000:
        raise ValueError("Опишите проблему сообщением длиной от 5 до 3000 символов")
    specialty = _parse_specialty(gigachat.generate_text(_PROMPT + message))
    return RequestAnalysis("Заявка собственника", message, specialty)
