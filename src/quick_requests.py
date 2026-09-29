from __future__ import annotations

import logging
from typing import Any

from src.auth_store import ChairmanAuthorizationRepository
from src.gigachat_api import GigaChatApi, GigaChatApiError
from src.hoa_store import HoaStore
from src.max_api import MaxApi, MaxApiError
from src.request_classifier import RequestAnalysis, classify_text

logger = logging.getLogger(__name__)
SPECIALTIES = {"plumber": "сантехник", "electrician": "электрик", "other": "специалист"}
_MAX_PHOTO_BYTES = 10 * 1024 * 1024


def _buttons(rows: list[list[tuple[str, str]]]) -> list[dict[str, Any]]:
    return [{"type": "inline_keyboard", "payload": {"buttons": [
        [{"type": "callback", "text": label, "payload": value} for label, value in row]
        for row in rows
    ]}}]


def start_quick_request(api: MaxApi, store: HoaStore, user_id: int) -> None:
    memberships = store.memberships(user_id)
    if not memberships:
        api.send_message("Сначала войдите как собственник: /auth", user_id=user_id)
        return
    store.clear_quick_draft(user_id)
    if len(memberships) == 1:
        store.set_quick_draft(user_id, memberships[0]["resident_id"], "awaiting_description")
        api.send_message(
            f"Новая заявка: {memberships[0]['hoa_name']}, помещение {memberships[0]['unit']}. "
            "Отправьте описание проблемы текстовым сообщением. Для отмены — /cancel.",
            user_id=user_id,
        )
        return
    store.set_quick_draft(user_id, "", "awaiting_unit")
    rows = [[(f"{item['hoa_name']} · пом. {item['unit']}", f"request:unit:{item['resident_id']}")]
            for item in memberships[:25]]
    api.send_message("Выберите помещение для заявки:", user_id=user_id, attachments=_buttons(rows))


def _membership(store: HoaStore, user_id: int, resident_id: str) -> dict[str, Any] | None:
    return next((item for item in store.memberships(user_id) if item["resident_id"] == resident_id), None)


def _notify(
    api: MaxApi, auth_store: ChairmanAuthorizationRepository | None,
    membership: dict[str, Any], analysis: RequestAnalysis,
    specialist: dict[str, Any] | None,
) -> None:
    if specialist and specialist.get("user_id") is not None:
        recipient = int(specialist["user_id"])
        prefix = "Вам назначена новая заявка"
    else:
        chairman = auth_store.get_profile_by_space_id(membership["space_id"]) if auth_store else None
        if chairman is None:
            return
        recipient = chairman.user_id
        prefix = "Новая заявка для председателя" if not specialist else "Специалист пока не вошёл в MAX; проверьте заявку"
    try:
        api.send_message(
            f"{prefix}: {membership['hoa_name']}, пом. {membership['unit']}\n"
            f"Текст собственника:\n{analysis.description}\n"
            "Фотографии доступны в мини-приложении.",
            user_id=recipient,
        )
    except MaxApiError:
        logger.exception("Не удалось отправить уведомление о заявке")


def _create(
    api: MaxApi, store: HoaStore, auth_store: ChairmanAuthorizationRepository | None,
    user_id: int, membership: dict[str, Any], analysis: RequestAnalysis,
    specialist: dict[str, Any] | None,
) -> None:
    result = store.create_request(
        user_id, membership["resident_id"], analysis.title, analysis.description,
        specialist_id=specialist["specialist_id"] if specialist else None,
        specialty=analysis.specialty, source="chat", use_draft_photos=True,
    )
    destination = f"специалисту {specialist['full_name']}" if specialist else "председателю"
    api.send_message(
        f"Заявка №{result['request_id'][:8]} создана и направлена {destination}. "
        "Фото прикреплены. Статус можно посмотреть в мини-приложении через /profile.",
        user_id=user_id,
    )
    _notify(api, auth_store, membership, analysis, specialist)


def handle_quick_callback(
    api: MaxApi, store: HoaStore | None,
    auth_store: ChairmanAuthorizationRepository | None,
    user_id: int, payload: str,
) -> bool:
    if not payload.startswith("request:"):
        return False
    if store is None:
        api.send_message("Заявки временно недоступны.", user_id=user_id)
        return True
    if payload == "request:new":
        start_quick_request(api, store, user_id)
        return True
    draft = store.get_quick_draft(user_id)
    if payload == "request:cancel":
        store.clear_quick_draft(user_id)
        api.send_message("Создание заявки отменено.", user_id=user_id)
        return True
    if draft is None:
        api.send_message("Черновик заявки истёк. Начните новую через кнопку в профиле.", user_id=user_id)
        return True
    if payload.startswith("request:unit:") and draft["stage"] == "awaiting_unit":
        resident_id = payload.removeprefix("request:unit:")
        membership = _membership(store, user_id, resident_id)
        if membership is None:
            api.send_message("Это помещение недоступно вашему аккаунту.", user_id=user_id)
            return True
        store.set_quick_draft(user_id, resident_id, "awaiting_description")
        api.send_message(
            f"Выбрано: {membership['hoa_name']}, помещение {membership['unit']}. "
            "Отправьте описание проблемы текстовым сообщением. Для отмены — /cancel.",
            user_id=user_id,
        )
        return True
    if payload == "request:finish" and draft["stage"] in {"awaiting_photos", "awaiting_photos_chairman"}:
        if store.quick_photo_count(user_id) == 0:
            api.send_message("Пришлите хотя бы одну фотографию проблемы.", user_id=user_id)
            return True
        membership = _membership(store, user_id, draft["resident_id"])
        if membership is None:
            store.clear_quick_draft(user_id)
            api.send_message("Доступ к помещению больше не подтверждён.", user_id=user_id)
            return True
        analysis = RequestAnalysis(draft["title"], draft["description"], draft["specialty"])
        if draft["stage"] == "awaiting_photos_chairman":
            _create(api, store, auth_store, user_id, membership, analysis, None)
            return True
        specialist = store.find_specialist(membership["space_id"], analysis.specialty)
        if specialist is not None:
            _create(api, store, auth_store, user_id, membership, analysis, specialist)
            return True
        store.set_quick_draft(
            user_id, membership["resident_id"], "awaiting_fallback",
            title=analysis.title, description=analysis.description, specialty=analysis.specialty,
        )
        api.send_message(
            f"В вашем ТСЖ не найден подходящий {SPECIALTIES[analysis.specialty]}. "
            "Создать заявку с фотографиями для председателя?",
            user_id=user_id,
            attachments=_buttons([[('Создать для председателя', 'request:confirm')],
                                  [('Отмена', 'request:cancel')]]),
        )
        return True
    if payload == "request:confirm" and draft["stage"] == "awaiting_fallback":
        membership = _membership(store, user_id, draft["resident_id"])
        if membership is None:
            store.clear_quick_draft(user_id)
            api.send_message("Доступ к помещению больше не подтверждён.", user_id=user_id)
            return True
        analysis = RequestAnalysis(draft["title"], draft["description"], draft["specialty"])
        if store.quick_photo_count(user_id) == 0:
            store.set_quick_draft(
                user_id, membership["resident_id"], "awaiting_photos_chairman",
                title=analysis.title, description=analysis.description, specialty=analysis.specialty,
            )
            api.send_message("Перед отправкой председателю пришлите 1–3 фотографии проблемы.", user_id=user_id)
            return True
        _create(api, store, auth_store, user_id, membership, analysis, None)
        return True
    api.send_message("Эта кнопка уже неактуальна. Начните новую заявку из профиля.", user_id=user_id)
    return True


def _image_type(content: bytes) -> str:
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    if content[4:8] == b"ftyp" and content[8:12] in {b"heic", b"heix", b"hevc", b"mif1"}:
        return "image/heic"
    raise ValueError("Пришлите фотографии в формате JPG, PNG, GIF, WebP или HEIC")


def handle_quick_message(
    api: MaxApi, store: HoaStore | None,
    auth_store: ChairmanAuthorizationRepository | None,
    gigachat: GigaChatApi | None, user_id: int, body: dict[str, Any],
) -> bool:
    if store is None:
        return False
    draft = store.get_quick_draft(user_id)
    if draft is None:
        return False
    text = str(body.get("text") or "")
    if text.strip().startswith("/"):
        return False
    if draft["stage"] in {"awaiting_photos", "awaiting_photos_chairman"}:
        attachments = [item for item in body.get("attachments") or []
                       if isinstance(item, dict) and item.get("type") == "image"]
        if not attachments:
            api.send_message("Пришлите 1–3 фотографии проблемы. Для отмены — /cancel.", user_id=user_id)
            return True
        if store.quick_photo_count(user_id) + len(attachments) > 3:
            api.send_message("К заявке можно прикрепить не больше 3 фотографий.", user_id=user_id)
            return True
        try:
            photos = []
            for attachment in attachments:
                payload = attachment.get("payload") or {}
                url = payload.get("url") if isinstance(payload, dict) else None
                if not isinstance(url, str) or not url:
                    raise ValueError("MAX не передал фото боту. Отправьте его ещё раз")
                content = api.download_attachment(url, max_bytes=_MAX_PHOTO_BYTES)
                photos.append((_image_type(content), content))
            count = store.add_quick_photos(user_id, photos)
        except (ValueError, MaxApiError) as error:
            api.send_message(str(error), user_id=user_id)
            return True
        api.send_message(
            f"Фотографии добавлены: {count} из 3. " +
            ("Нажмите «Отправить заявку»." if count == 3 else "Можно прислать ещё фото или отправить заявку."),
            user_id=user_id,
            attachments=_buttons([[('Отправить заявку', 'request:finish')],
                                  [('Отмена', 'request:cancel')]]),
        )
        return True
    if draft["stage"] != "awaiting_description":
        api.send_message("Выберите помещение или подтвердите заявку кнопкой выше. Для отмены — /cancel.", user_id=user_id)
        return True
    membership = _membership(store, user_id, draft["resident_id"])
    if membership is None:
        store.clear_quick_draft(user_id)
        api.send_message("Доступ к помещению больше не подтверждён.", user_id=user_id)
        return True
    if body.get("attachments"):
        api.send_message(
            "Описание проблемы пришлите отдельным текстовым сообщением. Для отмены — /cancel.",
            user_id=user_id,
        )
        return True
    if not text.strip():
        api.send_message(
            "Отправьте описание текстовым сообщением. Для отмены — /cancel.",
            user_id=user_id,
        )
        return True
    if gigachat is None:
        api.send_message("Анализ заявки временно недоступен. Попробуйте позже.", user_id=user_id)
        return True
    api.send_message("Определяю нужного специалиста…", user_id=user_id)
    try:
        analysis = classify_text(gigachat, text)
    except (ValueError, GigaChatApiError) as error:
        logger.warning("Не удалось разобрать быструю заявку user_id=%s", user_id, exc_info=True)
        api.send_message(
            str(error) if isinstance(error, ValueError) else
            "Не удалось обработать сообщение. Попробуйте описать проблему текстом ещё раз.",
            user_id=user_id,
        )
        return True
    store.set_quick_draft(
        user_id, membership["resident_id"], "awaiting_photos",
        title=analysis.title, description=analysis.description, specialty=analysis.specialty,
    )
    api.send_message(
        "Текст заявки сохранён без изменений. Теперь пришлите от 1 до 3 фотографий "
        "проблемы (каждая до 10 МБ). После загрузки нажмите «Отправить заявку».",
        user_id=user_id,
    )
    return True
