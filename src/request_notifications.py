from __future__ import annotations

import logging
from typing import Any

from src.max_api import MaxApi, MaxApiError


logger = logging.getLogger(__name__)

STATUS_LABELS = {
    "review": "На рассмотрении",
    "in_progress": "В процессе выполнения",
    "done": "Исполнено",
    "rejected": "Отклонена",
}


def notify_request_changes(
    api: MaxApi | None,
    request_id: str,
    before: dict[str, Any],
    after: dict[str, Any],
    chairman_user_id: int | None = None,
    group_chat_id: int | None = None,
) -> list[str]:
    """Отправляет сообщения после сохранения заявки; ошибки доставки не откатывают её."""
    warnings: list[str] = []
    number = request_id[:8]
    location = f"{after['hoa_name']}, пом. {after['unit']}"

    def send(user_id: int | None, text: str, warning: str) -> None:
        if user_id is None or api is None:
            warnings.append(warning)
            return
        try:
            api.send_message(text, user_id=int(user_id))
        except MaxApiError:
            logger.exception("Не удалось отправить уведомление по заявке %s", request_id)
            warnings.append(warning)

    status_changed = before["status"] != after["status"]
    if status_changed:
        label = STATUS_LABELS.get(after["status"], after["status"])
        send(
            after["owner_user_id"],
            f"Статус заявки №{number} изменён: {label}.\n{location}\n"
            "Подробности доступны в мини-приложении через /profile.",
            "Не удалось уведомить собственника об изменении статуса.",
        )

    if before.get("priority") != "emergency" and after.get("priority") == "emergency" \
            and after["status"] in {"review", "in_progress"}:
        send(
            chairman_user_id,
            f"🚨 Авария по заявке №{number}.\n{location}\n{after['title']}\n"
            "Откройте заявку в мини-приложении через /profile.",
            "Не удалось уведомить председателя об аварии.",
        )

    emergency_started = before.get("priority") != "emergency" and after.get("priority") == "emergency"
    emergency_status_changed = status_changed and "emergency" in {
        before.get("priority"), after.get("priority"),
    }
    if emergency_started or emergency_status_changed:
        label = STATUS_LABELS.get(after["status"], after["status"])
        if after["status"] == "done":
            text = f"✅ Авария по заявке №{number} устранена.\nСтатус: {label}."
        else:
            text = f"🚨 Авария по заявке №{number}.\nСтатус: {label}."
        if group_chat_id is None:
            warnings.append("Чат ТСЖ ещё не подключён; уведомление об аварии не отправлено.")
        elif api is None:
            warnings.append("Не удалось отправить уведомление об аварии в чат ТСЖ.")
        else:
            try:
                api.send_message(text, chat_id=int(group_chat_id))
            except MaxApiError:
                logger.exception("Не удалось уведомить чат ТСЖ об аварии по заявке %s", request_id)
                warnings.append("Не удалось отправить уведомление об аварии в чат ТСЖ.")

    if before["specialist_id"] != after["specialist_id"]:
        if before.get("visit_status") in {"awaiting_owner", "pending", "accepted"} and after.get("visit_status") == "cancelled":
            send(
                after["owner_user_id"],
                f"Запрос доступа по заявке №{number} отменён из-за смены исполнителя. "
                "Новый исполнитель при необходимости отправит новый запрос на доступ.",
                "Не удалось уведомить собственника об отмене визита.",
            )
        if before["specialist_user_id"] is not None:
            send(
                before["specialist_user_id"],
                f"Заявка №{number} ({location}) больше не назначена вам: председатель сменил исполнителя.",
                "Не удалось уведомить прежнего специалиста.",
            )
        if after["specialist_id"]:
            send(
                after["specialist_user_id"],
                f"Председатель назначил вас исполнителем заявки №{number}.\n"
                f"{location}\n{after['title']}\n"
                "Откройте заявку в мини-приложении через /profile.",
                "Специалист ещё не вошёл в бот или уведомление ему не доставлено.",
            )
    return warnings
