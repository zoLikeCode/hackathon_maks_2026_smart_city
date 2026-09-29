from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from src.gigachat_api import GigaChatApi


class RegistryError(ValueError):
    """Ответ GigaChat нельзя использовать для выдачи кодов собственникам."""


@dataclass(frozen=True)
class ResidentEntry:
    unit: str
    full_name: str
    area: str
    share: str
    ownership: str


@dataclass(frozen=True)
class RegistryResult:
    address: str
    residents: list[ResidentEntry]
    skipped_units: list[str]


UNITS_PROMPT = """Прочитай весь приложенный PDF-реестр собственников ТСЖ, включая все страницы.
Верни строго один JSON-объект без markdown:
{"address":"адрес дома точно как в документе", "units":["1","2","3"]}
В units перечисли каждый уникальный номер помещения, встречающийся в реестре, включая нежилые.
Не заменяй перечень диапазоном, не пропускай страницы и не придумывай номера.
Если адрес или список определить нельзя, верни пустое значение для соответствующего поля."""


def _entries_prompt(units: list[str]) -> str:
    requested = json.dumps(units, ensure_ascii=False)
    return f"""Прочитай приложенный PDF-реестр собственников. Обработай ТОЛЬКО помещения {requested}.
Верни строго один JSON-объект без markdown:
{{"residents":[{{"unit":"номер помещения", "full_name":"Фамилия Имя Отчество",
"area":"полная площадь помещения в м²", "share":"доля именно этого собственника",
"ownership":"право собственности, включая номер и дату, если указаны"}}],
"unresolved_units":["номер помещения"]}}
Для каждого человека создай отдельный элемент. При долевой собственности укажи его личную долю.
При общей совместной собственности без индивидуальной доли пиши share="совместная".
Номер помещения, площадь и право переписывай из документа. Не путай площадь с количеством голосов.
Если в помещении есть "???", неизвестный/нечитаемый собственник, только организация,
неоднозначная строка или нет уверенности в полноте списка, внеси ВСЁ помещение в unresolved_units
и не добавляй его в residents. Ничего не додумывай. Каждое запрошенное помещение должно оказаться
либо в residents, либо в unresolved_units. Не обрабатывай помещения вне списка."""


def _json_object(answer: str) -> dict[str, Any]:
    text = answer.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.S | re.I)
    if fence:
        text = fence.group(1)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise RegistryError("GigaChat вернул неразборчивый ответ; загрузите реестр повторно") from error
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError as nested:
            raise RegistryError("GigaChat вернул неразборчивый ответ; загрузите реестр повторно") from nested
    if not isinstance(value, dict):
        raise RegistryError("GigaChat не вернул данные реестра")
    return value


def _clean(value: Any, maximum: int) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    return " ".join(str(value).split())[:maximum]


def _valid_entry(raw: dict[str, Any], unit: str) -> ResidentEntry | None:
    name = _clean(raw.get("full_name"), 180)
    area = _clean(raw.get("area"), 30)
    share = _clean(raw.get("share"), 80)
    ownership = _clean(raw.get("ownership"), 500)
    if not re.fullmatch(r"[А-ЯЁа-яё-]+(?:\s+[А-ЯЁа-яё-]+){2,3}", name):
        return None
    try:
        square_meters = Decimal(area.replace(",", "."))
    except InvalidOperation:
        return None
    if not 0 < square_meters < 100000 or not share or not ownership:
        return None
    return ResidentEntry(unit, name, str(square_meters).replace(".", ","), share, ownership)


def parse_registry_with_gigachat(
    gigachat: GigaChatApi,
    content: bytes,
    filename: str,
) -> RegistryResult:
    """Все сведения PDF извлекает GigaChat; локально проверяется только его JSON."""
    if not content.startswith(b"%PDF-") or len(content) > 15 * 1024 * 1024:
        raise RegistryError("Нужен PDF-файл реестра размером до 15 МБ")

    file_id = gigachat.upload_file(content, filename, content_type="application/pdf")
    try:
        def ask(prompt: str) -> dict[str, Any]:
            response = gigachat.chat([{
                "role": "user", "content": prompt, "attachments": [file_id],
            }])
            return _json_object(GigaChatApi._answer_text(response))

        summary = ask(UNITS_PROMPT)
        address = _clean(summary.get("address"), 300)
        raw_units = summary.get("units")
        if not address or not isinstance(raw_units, list):
            raise RegistryError("GigaChat не смог определить адрес и помещения реестра")
        units = list(dict.fromkeys(_clean(unit, 40) for unit in raw_units))
        if not units or len(units) > 500 or any(not unit for unit in units):
            raise RegistryError("GigaChat вернул некорректный список помещений")

        residents: list[ResidentEntry] = []
        skipped: set[str] = set()
        for offset in range(0, len(units), 12):
            batch = units[offset : offset + 12]
            answer = ask(_entries_prompt(batch))
            raw_entries = answer.get("residents")
            raw_skipped = answer.get("unresolved_units")
            if not isinstance(raw_entries, list) or not isinstance(raw_skipped, list):
                raise RegistryError("GigaChat вернул неполный список собственников")
            batch_entries: list[ResidentEntry] = []
            batch_skipped = {_clean(unit, 40) for unit in raw_skipped}
            if not batch_skipped.issubset(set(batch)):
                raise RegistryError("GigaChat указал посторонние помещения")
            for raw in raw_entries:
                if not isinstance(raw, dict):
                    raise RegistryError("GigaChat вернул некорректную запись собственника")
                unit = _clean(raw.get("unit"), 40)
                if unit not in batch:
                    raise RegistryError("GigaChat указал постороннее помещение")
                entry = _valid_entry(raw, unit)
                if entry is None:
                    batch_skipped.add(unit)
                else:
                    batch_entries.append(entry)
            seen = set()
            for entry in batch_entries:
                key = (entry.unit, entry.full_name.casefold())
                if key in seen:
                    batch_skipped.add(entry.unit)
                seen.add(key)
            covered = {entry.unit for entry in batch_entries} | batch_skipped
            batch_skipped.update(set(batch) - covered)
            residents.extend(entry for entry in batch_entries if entry.unit not in batch_skipped)
            skipped.update(batch_skipped)
        if not residents:
            raise RegistryError("GigaChat не определил собственников с достаточной уверенностью")
        return RegistryResult(address, residents, [unit for unit in units if unit in skipped])
    finally:
        gigachat.delete_file(file_id)
