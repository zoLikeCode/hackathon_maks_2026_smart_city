from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from src.gigachat_api import GigaChatApi


PROTOCOL_REVIEW_PROMPT = """
Ты проверяешь протокол правления ТСЖ для авторизации председателя.
Проанализируй весь прикрепленный документ, включая сканы и подписи.

Верни только один JSON-объект без markdown и пояснений со следующими полями:
{
  "is_hoa_board_protocol": true или false,
  "hoa_name": "полное название ТСЖ или пустая строка",
  "address": "адрес ТСЖ/дома или пустая строка",
  "meeting_date": "дата в документе или пустая строка",
  "quorum_confirmed": true или false,
  "chairman_election_on_agenda": true или false,
  "chairman_elected": true или false,
  "chairman_name": "ФИО избранного председателя или пустая строка",
  "votes_for_percent": число от 0 до 100 либо null,
  "decision_confirmed": true или false,
  "signature_present": true или false,
  "rejection_reason": "краткая причина отказа или пустая строка",
  "confidence": число от 0 до 1
}

Ставь decision_confirmed=true только если из текста явно следует, что правомочное заседание
состоялось, вопрос об избрании председателя был рассмотрен, голосование завершилось принятием
решения и конкретный человек избран председателем правления ТСЖ. Ничего не додумывай.
""".strip()


class ProtocolAnalysisError(ValueError):
    """Ответ модели нельзя использовать как результат проверки протокола."""


@dataclass(frozen=True)
class ProtocolAnalysis:
    is_hoa_board_protocol: bool
    hoa_name: str
    address: str
    meeting_date: str
    quorum_confirmed: bool
    chairman_election_on_agenda: bool
    chairman_elected: bool
    chairman_name: str
    votes_for_percent: float | None
    decision_confirmed: bool
    signature_present: bool
    rejection_reason: str
    confidence: float

    @property
    def approved(self) -> bool:
        return all(
            (
                self.is_hoa_board_protocol,
                bool(self.hoa_name.strip()),
                bool(self.address.strip()),
                self.quorum_confirmed,
                self.chairman_election_on_agenda,
                self.chairman_elected,
                bool(self.chairman_name.strip()),
                self.decision_confirmed,
                self.signature_present,
                self.confidence >= 0.7,
            )
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _json_object(text: str) -> dict[str, Any]:
    value = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", value, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        value = fenced.group(1)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        start = value.find("{")
        end = value.rfind("}")
        if start < 0 or end <= start:
            raise ProtocolAnalysisError("GigaChat не вернул JSON") from error
        try:
            parsed = json.loads(value[start : end + 1])
        except json.JSONDecodeError as nested_error:
            raise ProtocolAnalysisError("GigaChat вернул некорректный JSON") from nested_error
    if not isinstance(parsed, dict):
        raise ProtocolAnalysisError("GigaChat вернул JSON другого типа")
    return parsed


def _required_bool(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ProtocolAnalysisError(f"Поле {key} должно быть boolean")
    return value


def parse_protocol_analysis(text: str) -> ProtocolAnalysis:
    data = _json_object(text)
    votes = data.get("votes_for_percent")
    if votes is not None and not isinstance(votes, (int, float)):
        raise ProtocolAnalysisError("Поле votes_for_percent должно быть числом или null")
    confidence = data.get("confidence")
    if not isinstance(confidence, (int, float)):
        raise ProtocolAnalysisError("Поле confidence должно быть числом")
    return ProtocolAnalysis(
        is_hoa_board_protocol=_required_bool(data, "is_hoa_board_protocol"),
        hoa_name=str(data.get("hoa_name") or "").strip(),
        address=str(data.get("address") or "").strip(),
        meeting_date=str(data.get("meeting_date") or "").strip(),
        quorum_confirmed=_required_bool(data, "quorum_confirmed"),
        chairman_election_on_agenda=_required_bool(data, "chairman_election_on_agenda"),
        chairman_elected=_required_bool(data, "chairman_elected"),
        chairman_name=str(data.get("chairman_name") or "").strip(),
        votes_for_percent=float(votes) if votes is not None else None,
        decision_confirmed=_required_bool(data, "decision_confirmed"),
        signature_present=_required_bool(data, "signature_present"),
        rejection_reason=str(data.get("rejection_reason") or "").strip(),
        confidence=max(0.0, min(1.0, float(confidence))),
    )


def analyze_chairman_protocol(
    gigachat: GigaChatApi,
    content: bytes,
    filename: str,
) -> ProtocolAnalysis:
    answer = gigachat.analyze_document(content, filename, prompt=PROTOCOL_REVIEW_PROMPT)
    return parse_protocol_analysis(answer)


def document_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def make_space_id(hoa_name: str, address: str) -> str:
    normalized = " ".join(f"{hoa_name} {address}".casefold().split())
    return f"hoa_{hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:24]}"
