from __future__ import annotations

import logging
import os
import signal
import time
from datetime import date
from pathlib import Path
from typing import Any

from src.auth_store import (
    ChairmanAuthorizationRepository,
    ChairmanProfile,
    ProtocolAlreadyClaimedError,
    create_chairman_authorization_store,
)
from src.chairman_protocol import (
    ProtocolAnalysisError,
    analyze_chairman_protocol,
    document_sha256,
    make_space_id,
)
from src.gigachat_api import GigaChatApi, GigaChatApiError
from src.hoa_store import CodeError, HoaStore, timezone_label
from src.max_api import MaxApi, MaxApiError
from src.mini_app import MiniAppServer
from src.phone_verification import (
    ContactVerificationError,
    extract_phone_from_vcard,
    find_contact_payload,
    masked_phone,
)
from src.quick_requests import handle_quick_callback, handle_quick_message, start_quick_request
from src.verification_store import (
    PhoneVerificationRepository,
    create_phone_verification_store,
)

WELCOME_TEXT = (
    "Добро пожаловать в проект «Умный город».\n\n"
    "Выберите, как вы хотите войти. Номер подтверждается в чате. "
    "После входа ваше пространство откроется в мини-приложении."
)
HELP_TEXT = (
    "Для входа выберите роль командой /auth. Председателю нужно подтвердить номер и отправить "
    "протокол правления. Затем он загружает реестр в мини-приложении и лично сообщает "
    "собственнику код. Собственник подтверждает номер и вводит код в чате. "
    "Специалист подтверждает номер, который председатель внесла в список.\n\n"
    "После описания заявки собственник прикрепляет от 1 до 3 фотографий и подтверждает отправку.\n\n"
    "Команды: /start, /auth, /phone, /code, /request, /cancel, /status, /profile, /help, /ping"
)
ROLE_KEYBOARD = [
    {
        "type": "inline_keyboard",
        "payload": {
            "buttons": [
                [
                    {
                        "type": "callback",
                        "text": "Я председатель",
                        "payload": "auth:chairman",
                    }
                ],
                [
                    {
                        "type": "callback",
                        "text": "Я собственник",
                        "payload": "auth:owner",
                    }
                ],
                [
                    {
                        "type": "callback",
                        "text": "Я специалист",
                        "payload": "auth:specialist",
                    }
                ],
            ]
        },
    }
]
CONTACT_KEYBOARD = [
    {
        "type": "inline_keyboard",
        "payload": {
            "buttons": [
                [{"type": "request_contact", "text": "Поделиться номером"}],
            ]
        },
    }
]

logger = logging.getLogger(__name__)
running = True


def load_env(path: Path = Path(".env")) -> None:
    """Загружает простой KEY=VALUE файл без внешних зависимостей."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def reply_target(update: dict[str, Any]) -> dict[str, int] | None:
    message = update.get("message") or {}
    recipient = message.get("recipient") or {}
    sender = message.get("sender") or {}

    if recipient.get("chat_type") == "dialog":
        user_id = sender.get("user_id") or recipient.get("user_id")
        return {"user_id": int(user_id)} if user_id is not None else None

    chat_id = recipient.get("chat_id") or update.get("chat_id")
    return {"chat_id": int(chat_id)} if chat_id is not None else None


def profile_keyboard(bot_username: str | None) -> list[dict[str, Any]] | None:
    if not bot_username:
        return None
    return [
        {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [
                    [
                        {
                            "type": "open_app",
                            "text": "Открыть профиль",
                            "web_app": bot_username.lstrip("@"),
                            "payload": "profile",
                        }
                    ]
                ]
            },
        }
    ]


def owner_profile_keyboard(bot_username: str | None) -> list[dict[str, Any]]:
    buttons: list[list[dict[str, Any]]] = []
    if bot_username:
        buttons.append([{
            "type": "open_app", "text": "Открыть профиль",
            "web_app": bot_username.lstrip("@"), "payload": "profile",
        }])
    buttons.append([{
        "type": "callback", "text": "Быстро создать заявку", "payload": "request:new",
    }])
    return [{"type": "inline_keyboard", "payload": {"buttons": buttons}}]


def send_role_selection(api: MaxApi, user_id: int) -> None:
    api.send_message(WELCOME_TEXT, user_id=user_id, attachments=ROLE_KEYBOARD)


def send_phone_request(api: MaxApi, **target: int) -> None:
    api.send_message(
        "Сначала подтвердите номер, привязанный к вашему аккаунту MAX.",
        attachments=CONTACT_KEYBOARD,
        **target,
    )


def send_protocol_request(api: MaxApi, user_id: int) -> None:
    api.send_message(
        "Номер подтверждён. Теперь отправьте одним файлом протокол правления ТСЖ, "
        "в котором зафиксировано избрание председателя. Поддерживаются PDF, DOC и DOCX.",
        user_id=user_id,
    )


def send_authorized_profile(
    api: MaxApi,
    profile: ChairmanProfile,
    bot_username: str | None,
) -> None:
    api.send_message(
        "Авторизация председателя подтверждена.\n\n"
        f"Председатель: {profile.full_name}\n"
        f"ТСЖ: {profile.hoa_name}\n"
        f"Адрес: {profile.address}\n"
        f"Телефон: {masked_phone(profile.phone)}",
        user_id=profile.user_id,
        attachments=profile_keyboard(bot_username),
    )


def send_owner_profile(api: MaxApi, user_id: int, memberships: list[dict[str, Any]], bot_username: str | None) -> None:
    places = ", ".join(f"{item['hoa_name']}, пом. {item['unit']}" for item in memberships[:3])
    api.send_message(
        f"Вход собственника подтверждён. Ваше пространство: {places}. "
        "Заявки и их статусы доступны в мини-приложении.",
        user_id=user_id,
        attachments=owner_profile_keyboard(bot_username),
    )


def send_owner_code_request(api: MaxApi, **target: int) -> None:
    api.send_message(
        "Номер подтверждён. Получите личный код у председателя и отправьте его "
        "сюда одним сообщением или командой /code КОД. Код обновляется каждые два часа.",
        **target,
    )


def send_specialist_profile(
    api: MaxApi, user_id: int, memberships: list[dict[str, Any]], bot_username: str | None,
) -> None:
    specialty_labels = {"plumber": "сантехник", "electrician": "электрик"}
    spaces = ", ".join(item["hoa_name"] for item in memberships[:3])
    api.send_message(
        f"Вход специалиста подтверждён. Вы — {specialty_labels.get(memberships[0]['specialty'], 'специалист')}. "
        f"Доступно пространство ТСЖ: {spaces}. Откройте мини-приложение для просмотра профиля.",
        user_id=user_id,
        attachments=profile_keyboard(bot_username),
    )


def start_specialist_authorization(
    api: MaxApi,
    user_id: int,
    phone_store: PhoneVerificationRepository | None,
    auth_store: ChairmanAuthorizationRepository | None,
    hoa_store: HoaStore | None,
    bot_username: str | None,
) -> None:
    if auth_store is None or hoa_store is None:
        api.send_message("Хранилище авторизации пока недоступно.", user_id=user_id)
        return
    verification = phone_store.get(user_id) if phone_store else None
    if verification is None:
        auth_store.set_state(user_id, "specialist", "awaiting_phone")
        send_phone_request(api, user_id=user_id)
        return
    memberships = hoa_store.claim_specialist_phone(user_id, verification.phone)
    if memberships:
        auth_store.clear_state(user_id)
        send_specialist_profile(api, user_id, memberships, bot_username)
    else:
        auth_store.set_state(user_id, "specialist", "awaiting_invite")
        api.send_message(
            "Для этого номера пока нет приглашения специалиста. Попросите председателя внести "
            "ваши ФИО, специальность и номер телефона, затем снова выберите «Я специалист».",
            user_id=user_id,
        )


def start_owner_authorization(
    api: MaxApi,
    user_id: int,
    phone_store: PhoneVerificationRepository | None,
    auth_store: ChairmanAuthorizationRepository | None,
    hoa_store: HoaStore | None,
    bot_username: str | None,
) -> None:
    if auth_store is None or hoa_store is None:
        api.send_message("Хранилище авторизации пока недоступно.", user_id=user_id)
        return
    memberships = hoa_store.memberships(user_id)
    if memberships:
        auth_store.clear_state(user_id)
        send_owner_profile(api, user_id, memberships, bot_username)
        return
    auth_store.set_state(user_id, "owner", "awaiting_phone")
    send_phone_request(api, user_id=user_id)


def start_chairman_authorization(
    api: MaxApi,
    user_id: int,
    phone_store: PhoneVerificationRepository | None,
    auth_store: ChairmanAuthorizationRepository | None,
    bot_username: str | None,
) -> None:
    if auth_store is None:
        api.send_message("Хранилище авторизации пока недоступно.", user_id=user_id)
        return
    profile = auth_store.get_profile_by_user_id(user_id)
    if profile is not None:
        auth_store.clear_state(user_id)
        send_authorized_profile(api, profile, bot_username)
        return

    verification = phone_store.get(user_id) if phone_store is not None else None
    if verification is None:
        auth_store.set_state(user_id, "chairman", "awaiting_phone")
        send_phone_request(api, user_id=user_id)
        return

    profile = auth_store.link_user_to_phone(user_id, verification.phone)
    if profile is not None:
        auth_store.clear_state(user_id)
        send_authorized_profile(api, profile, bot_username)
        return

    auth_store.set_state(user_id, "chairman", "awaiting_protocol")
    send_protocol_request(api, user_id)


def handle_contact(
    api: MaxApi,
    update: dict[str, Any],
    target: dict[str, int],
    phone_store: PhoneVerificationRepository | None,
    auth_store: ChairmanAuthorizationRepository | None = None,
    bot_username: str | None = None,
    hoa_store: HoaStore | None = None,
) -> bool:
    message = update.get("message") or {}
    body = message.get("body") or {}
    payload = find_contact_payload(body)
    if payload is None:
        return False

    sender_id = (message.get("sender") or {}).get("user_id")
    recipient = message.get("recipient") or {}
    if sender_id is None or recipient.get("chat_type") != "dialog":
        api.send_message(
            "Подтверждение номера доступно только в личном диалоге с ботом.",
            **target,
        )
        return True

    vcf_info = payload.get("vcf_info")
    signature = payload.get("hash")
    if not isinstance(vcf_info, str) or not isinstance(signature, str):
        api.send_message(
            "Этот контакт не подтверждён MAX. Используйте кнопку из команды /phone.",
            **target,
        )
        return True

    if not api.verify_contact(vcf_info, signature):
        logger.warning("Отклонена некорректная подпись контакта от user_id=%s", sender_id)
        api.send_message(
            "Не удалось проверить подпись номера. Запросите новую кнопку командой /phone.",
            **target,
        )
        return True

    try:
        phone = extract_phone_from_vcard(vcf_info)
    except ContactVerificationError:
        logger.warning("Отклонён подписанный контакт без корректного телефона", exc_info=True)
        api.send_message("MAX передал контакт без корректного номера телефона.", **target)
        return True

    user_id = int(sender_id)
    if phone_store is not None:
        phone_store.save(user_id, phone)
    logger.info("Подтверждён номер для user_id=%s", sender_id)

    state = auth_store.get_state(user_id) if auth_store is not None else None
    if auth_store is not None and state is not None and state.role == "chairman":
        profile = auth_store.link_user_to_phone(user_id, phone)
        if profile is not None:
            auth_store.clear_state(user_id)
            send_authorized_profile(api, profile, bot_username)
        else:
            auth_store.set_state(user_id, "chairman", "awaiting_protocol")
            send_protocol_request(api, user_id)
        return True

    if auth_store is not None and state is not None and state.role == "owner":
        memberships = hoa_store.memberships(user_id) if hoa_store else []
        if memberships:
            auth_store.clear_state(user_id)
            send_owner_profile(api, user_id, memberships, bot_username)
        else:
            auth_store.set_state(user_id, "owner", "awaiting_code")
            send_owner_code_request(api, **target)
        return True

    if auth_store is not None and state is not None and state.role == "specialist":
        memberships = hoa_store.claim_specialist_phone(user_id, phone) if hoa_store else []
        if memberships:
            auth_store.clear_state(user_id)
            send_specialist_profile(api, user_id, memberships, bot_username)
        else:
            auth_store.set_state(user_id, "specialist", "awaiting_invite")
            api.send_message(
                "Номер подтверждён, но приглашение специалиста для него пока не найдено. "
                "Попросите председателя добавить этот номер.",
                **target,
            )
        return True

    api.send_message(
        f"Номер {masked_phone(phone)} подтверждён и привязан к вашему аккаунту MAX.",
        **target,
    )
    return True


def _file_attachment(body: dict[str, Any]) -> tuple[str, str] | None:
    attachments = body.get("attachments")
    if not isinstance(attachments, list):
        return None
    for attachment in attachments:
        if not isinstance(attachment, dict) or attachment.get("type") != "file":
            continue
        payload = attachment.get("payload")
        if not isinstance(payload, dict):
            continue
        url = payload.get("url")
        filename = attachment.get("filename")
        if isinstance(url, str) and url and isinstance(filename, str) and filename:
            return Path(filename).name, url
    return None


def handle_protocol_document(
    api: MaxApi,
    update: dict[str, Any],
    target: dict[str, int],
    phone_store: PhoneVerificationRepository | None,
    auth_store: ChairmanAuthorizationRepository | None,
    gigachat: GigaChatApi | None,
    bot_username: str | None,
) -> bool:
    message = update.get("message") or {}
    body = message.get("body") or {}
    attachment = _file_attachment(body)
    if attachment is None:
        return False
    sender_id = (message.get("sender") or {}).get("user_id")
    recipient = message.get("recipient") or {}
    if sender_id is None or recipient.get("chat_type") != "dialog":
        api.send_message("Протокол можно отправить только в личном диалоге.", **target)
        return True
    user_id = int(sender_id)
    state = auth_store.get_state(user_id) if auth_store is not None else None
    if state is None or state.role != "chairman" or state.step != "awaiting_protocol":
        api.send_message("Сначала выберите авторизацию председателя командой /auth.", **target)
        return True
    verification = phone_store.get(user_id) if phone_store is not None else None
    if verification is None:
        auth_store.set_state(user_id, "chairman", "awaiting_phone")
        send_phone_request(api, user_id=user_id)
        return True
    if gigachat is None:
        api.send_message("Проверка документов временно недоступна.", **target)
        return True

    filename, url = attachment
    if Path(filename).suffix.lower() not in {".pdf", ".doc", ".docx"}:
        api.send_message("Отправьте протокол в формате PDF, DOC или DOCX.", **target)
        return True
    api.send_message("Протокол получен. Проверяю кворум, голосование и решение…", **target)
    try:
        content = api.download_attachment(url)
        digest = document_sha256(content)
        claimed = auth_store.get_profile_by_protocol(digest)
        if claimed is not None and claimed.phone != verification.phone:
            raise ProtocolAlreadyClaimedError
        if claimed is not None:
            profile = auth_store.link_user_to_phone(user_id, verification.phone)
            auth_store.clear_state(user_id)
            if profile is not None:
                send_authorized_profile(api, profile, bot_username)
                return True

        analysis = analyze_chairman_protocol(gigachat, content, filename)
        if not analysis.approved:
            auth_store.record_review(
                document_sha256=digest,
                user_id=user_id,
                phone=verification.phone,
                filename=filename,
                status="rejected",
                analysis=analysis.as_dict(),
            )
            reason = analysis.rejection_reason or (
                "не удалось однозначно подтвердить кворум, голосование, решение и подпись"
            )
            api.send_message(
                f"Протокол не прошёл проверку: {reason}. Отправьте более полный или читаемый файл.",
                **target,
            )
            return True

        profile = auth_store.approve_chairman(
            user_id=user_id,
            phone=verification.phone,
            full_name=analysis.chairman_name,
            space_id=make_space_id(analysis.hoa_name, analysis.address),
            hoa_name=analysis.hoa_name,
            address=analysis.address,
            protocol_sha256=digest,
            protocol_filename=filename,
        )
        auth_store.record_review(
            document_sha256=digest,
            user_id=user_id,
            phone=verification.phone,
            filename=filename,
            status="approved",
            analysis=analysis.as_dict(),
        )
        auth_store.clear_state(user_id)
        send_authorized_profile(api, profile, bot_username)
    except ProtocolAlreadyClaimedError:
        api.send_message(
            "Этот протокол уже использован для другого подтверждённого номера. "
            "Для продолжения нужна проверка администратором.",
            **target,
        )
    except (MaxApiError, GigaChatApiError, ProtocolAnalysisError, ValueError):
        logger.exception("Не удалось проверить протокол user_id=%s", user_id)
        api.send_message(
            "Не удалось обработать протокол. Попробуйте отправить читаемый PDF ещё раз.",
            **target,
        )
    return True


def send_pending_visit_approvals(api: MaxApi, hoa_store: HoaStore) -> None:
    for notice in hoa_store.pending_visit_approval_notices():
        day = notice["date"]
        hour = notice["hour"]
        when = (f"{day[8:10]}.{day[5:7]}.{day[:4]}, "
                f"{hour:02d}:00–{hour + 1:02d}:00 "
                f"(местное время ТСЖ: {timezone_label(notice['timezone'])})")
        try:
            api.send_message(
                f"Собственник разрешил вам войти в квартиру по заявке №{notice['request_id'][:8]} "
                f"«{notice['title']}».\n{notice['address']}, квартира {notice['unit']}.\n"
                f"Согласованное время: {when}.",
                user_id=int(notice["specialist_user_id"]),
            )
        except MaxApiError:
            logger.exception("Не удалось уведомить специалиста о разрешении на вход %s; повторим позже",
                             notice["request_id"])
            continue
        hoa_store.mark_visit_approval_notified(notice["request_id"])


def handle_callback(
    api: MaxApi,
    update: dict[str, Any],
    phone_store: PhoneVerificationRepository | None,
    auth_store: ChairmanAuthorizationRepository | None,
    bot_username: str | None,
    hoa_store: HoaStore | None = None,
) -> bool:
    if update.get("update_type") != "message_callback":
        return False
    callback = update.get("callback") or {}
    user_id = (callback.get("user") or {}).get("user_id")
    callback_id = callback.get("callback_id")
    payload = callback.get("payload")
    if user_id is None:
        return True
    user_id = int(user_id)
    if isinstance(callback_id, str):
        try:
            api.answer_callback(callback_id)
        except MaxApiError:
            logger.warning("Не удалось подтвердить callback MAX", exc_info=True)
    if isinstance(payload, str) and payload.startswith("visit:"):
        parts = payload.split(":")
        if hoa_store is None or len(parts) < 4:
            return True
        action, request_id, token = parts[1:4]
        keyboard = lambda rows: [{"type": "inline_keyboard", "payload": {"buttons": [
            [{"type": "callback", "text": label, "payload": value} for label, value in row]
            for row in rows
        ]}}]
        try:
            if action == "days" and len(parts) == 5:
                page = int(parts[4])
                options = hoa_store.owner_visit_days(user_id, request_id, token, page)
                if options is None:
                    raise ValueError("Запрос доступа больше неактуален. Попросите специалиста отправить новый")
                weekdays = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
                rows = [[(f"{weekdays[date.fromisoformat(day).weekday()]} {day[8:10]}.{day[5:7]}",
                          f"visit:hours:{request_id}:{token}:{day}")] for day in options["dates"]]
                navigation = []
                if page > 0:
                    navigation.append(("← Раньше", f"visit:days:{request_id}:{token}:{page - 1}"))
                if options["has_next"]:
                    navigation.append(("Позже →", f"visit:days:{request_id}:{token}:{page + 1}"))
                if navigation:
                    rows.append(navigation)
                rows.append([("Отказаться от визита", f"visit:decline:{request_id}:{token}")])
                api.send_message(
                    f"Выберите день посещения по заявке №{request_id[:8]} из ближайших 10 дней "
                    f"(местное время ТСЖ: {timezone_label(options['timezone'])})."
                    + (" На этой неделе свободных часов нет." if not options["dates"] else ""),
                    user_id=user_id, attachments=keyboard(rows),
                )
            elif action == "hours" and len(parts) == 5:
                day = parts[4]
                options = hoa_store.owner_visit_hours(user_id, request_id, token, day)
                if options is None:
                    raise ValueError("Запрос доступа больше неактуален")
                rows = [[(f"{hour:02d}:00–{hour + 1:02d}:00",
                          f"visit:pick:{request_id}:{token}:{day}:{hour}")]
                        for hour in options["hours"]]
                rows.append([("← К датам", f"visit:days:{request_id}:{token}:0")])
                api.send_message(
                    f"Выберите свободный час на {day[8:10]}.{day[5:7]}.{day[:4]} "
                    f"({timezone_label(options['timezone'])})."
                    + (" Свободных часов не осталось." if not options["hours"] else ""),
                    user_id=user_id, attachments=keyboard(rows),
                )
            elif action == "pick" and len(parts) == 6:
                day, hour = parts[4], int(parts[5])
                options = hoa_store.owner_visit_hours(user_id, request_id, token, day)
                if options is None:
                    raise ValueError("Запрос доступа больше неактуален")
                if hour not in options["hours"]:
                    raise ValueError("Этот час уже занят. Выберите другой")
                api.send_message(
                    f"Подтвердить доступ специалиста по заявке №{request_id[:8]} "
                    f"{day[8:10]}.{day[5:7]}.{day[:4]} в {hour:02d}:00–{hour + 1:02d}:00 "
                    f"({timezone_label(options['timezone'])})?",
                    user_id=user_id, attachments=keyboard([
                        [("Подтвердить визит", f"visit:confirm:{request_id}:{token}:{day}:{hour}")],
                        [("Выбрать другой час", f"visit:hours:{request_id}:{token}:{day}")],
                    ]),
                )
            elif action == "confirm" and len(parts) == 6:
                result = hoa_store.confirm_owner_visit(user_id, request_id, token, parts[4], int(parts[5]))
                if result is None:
                    raise ValueError("Запрос доступа больше неактуален")
                send_pending_visit_approvals(api, hoa_store)
                date_label = f"{result['date'][8:10]}.{result['date'][5:7]}.{result['date'][:4]}, {result['hour']:02d}:00–{result['hour'] + 1:02d}:00 ({timezone_label(result['timezone'])})"
                api.send_message(f"Визит по заявке №{request_id[:8]} подтверждён: {date_label}.", user_id=user_id)
            elif action == "decline" and len(parts) == 4:
                result = hoa_store.decline_owner_access(user_id, request_id, token)
                if result is None:
                    raise ValueError("Запрос доступа больше неактуален")
                api.send_message(f"Вы отказались от визита по заявке №{request_id[:8]}.", user_id=user_id)
                if result["specialist_user_id"] is not None:
                    try:
                        api.send_message(f"Собственник отказался от визита по заявке №{request_id[:8]} «{result['title']}».",
                                         user_id=int(result["specialist_user_id"]))
                    except MaxApiError:
                        logger.exception("Не удалось уведомить специалиста об отказе %s", request_id)
            elif action in {"yes", "no"} and len(parts) == 4:
                result = hoa_store.respond_visit(user_id, request_id, token, action == "yes")
                if result is None:
                    raise ValueError("Предложение времени больше неактуально")
                if result["accepted"]:
                    send_pending_visit_approvals(api, hoa_store)
                decision = "подтверждено" if result["accepted"] else "отклонено"
                api.send_message(f"Предложение по заявке №{request_id[:8]} {decision}.", user_id=user_id)
                if not result["accepted"] and result["specialist_user_id"] is not None:
                    api.send_message(f"Собственник отклонил время визита по заявке №{request_id[:8]}.",
                                     user_id=int(result["specialist_user_id"]))
        except (ValueError, OverflowError) as error:
            api.send_message(str(error), user_id=user_id)
        return True
    if isinstance(payload, str) and handle_quick_callback(api, hoa_store, auth_store, user_id, payload):
        return True
    if payload == "auth:chairman":
        start_chairman_authorization(api, user_id, phone_store, auth_store, bot_username)
    elif payload == "auth:owner":
        start_owner_authorization(api, user_id, phone_store, auth_store, hoa_store, bot_username)
    elif payload == "auth:specialist":
        start_specialist_authorization(api, user_id, phone_store, auth_store, hoa_store, bot_username)
    return True


def handle_update(
    api: MaxApi,
    update: dict[str, Any],
    store: PhoneVerificationRepository | None = None,
    auth_store: ChairmanAuthorizationRepository | None = None,
    gigachat: GigaChatApi | None = None,
    bot_username: str | None = None,
    hoa_store: HoaStore | None = None,
) -> None:
    update_type = update.get("update_type")

    if update_type in {"bot_added", "bot_admin_permissions_changed"}:
        chat_id = update.get("chat_id")
        if hoa_store is not None and chat_id is not None:
            hoa_store.queue_group_chat_check(int(chat_id))
        return
    if update_type == "bot_removed":
        chat_id = update.get("chat_id")
        if hoa_store is not None and chat_id is not None:
            hoa_store.forget_group_chat(int(chat_id))
        return

    if update_type == "bot_started":
        user_id = (update.get("user") or {}).get("user_id")
        if user_id is not None:
            user_id = int(user_id)
            profile = auth_store.get_profile_by_user_id(user_id) if auth_store is not None else None
            if profile is not None:
                send_authorized_profile(api, profile, bot_username)
            elif hoa_store is not None and (memberships := hoa_store.memberships(user_id)):
                send_owner_profile(api, user_id, memberships, bot_username)
            elif hoa_store is not None and store is not None and (verification := store.get(user_id)) is not None and (
                specialist_spaces := hoa_store.specialist_memberships(user_id, verification.phone)
            ):
                send_specialist_profile(api, user_id, specialist_spaces, bot_username)
            elif auth_store is not None and (
                state := auth_store.get_state(user_id)
            ) is not None and state.role == "owner":
                if state.step == "awaiting_code" and store is not None and store.get(user_id):
                    send_owner_code_request(api, user_id=user_id)
                else:
                    send_phone_request(api, user_id=user_id)
            else:
                send_role_selection(api, user_id)
        return

    if handle_callback(api, update, store, auth_store, bot_username, hoa_store):
        return
    if update_type != "message_created":
        return

    if not isinstance(update.get("message"), dict):
        logger.warning(
            "MAX прислал message_created без message; поля события: %s",
            ", ".join(sorted(update)),
        )
        return
    target = reply_target(update)
    message = update["message"]
    recipient = message.get("recipient") or {}
    if recipient.get("chat_type") != "dialog":
        chat_id = recipient.get("chat_id") or update.get("chat_id")
        if chat_id is None or hoa_store is None:
            return
        if not hoa_store.is_group_chat_authorized(int(chat_id)):
            hoa_store.queue_group_chat_check(int(chat_id))
            return
    body = message.get("body") or {}
    text = (body.get("text") or "").strip()
    if recipient.get("chat_type") != "dialog" and not text.startswith("/"):
        return
    if not text and not body.get("attachments"):
        logger.warning(
            "MAX прислал сообщение без текста и вложений; поля body: %s",
            ", ".join(sorted(body)),
        )
    if target is None:
        logger.warning("У события message_created нет адресата для ответа")
        return

    if handle_contact(api, update, target, store, auth_store, bot_username, hoa_store):
        return
    sender_id = (message.get("sender") or {}).get("user_id")
    if sender_id is not None and (message.get("recipient") or {}).get("chat_type") == "dialog":
        if handle_quick_message(api, hoa_store, auth_store, gigachat, int(sender_id), body):
            return
    if handle_protocol_document(
        api,
        update,
        target,
        store,
        auth_store,
        gigachat,
        bot_username,
    ):
        return

    command = text.split(maxsplit=1)[0].lower() if text else ""
    command = command.split("@", 1)[0]
    if command == "/request":
        if sender_id is not None and (message.get("recipient") or {}).get("chat_type") == "dialog" and hoa_store:
            start_quick_request(api, hoa_store, int(sender_id))
        else:
            api.send_message("Создать заявку можно только в личном диалоге с ботом.", **target)
        return
    if command == "/cancel":
        if sender_id is not None and hoa_store:
            hoa_store.clear_quick_draft(int(sender_id))
        api.send_message("Создание заявки отменено.", **target)
        return
    if command == "/code" or (
        auth_store is not None and sender_id is not None
        and (state := auth_store.get_state(int(sender_id))) is not None
        and state.role == "owner" and state.step == "awaiting_code" and text
        and not text.startswith("/")
    ):
        if sender_id is None or (message.get("recipient") or {}).get("chat_type") != "dialog":
            response = "Код можно вводить только в личном диалоге с ботом."
        elif hoa_store is None or auth_store is None:
            response = "Авторизация пока недоступна."
        else:
            verification = store.get(int(sender_id)) if store else None
            if verification is None:
                auth_store.set_state(int(sender_id), "owner", "awaiting_phone")
                send_phone_request(api, **target)
                return
            code = text.split(maxsplit=1)[1] if command == "/code" and len(text.split(maxsplit=1)) > 1 else (text if command != "/code" else "")
            try:
                hoa_store.claim_code(int(sender_id), verification.phone, code)
            except CodeError as error:
                response = str(error)
            else:
                auth_store.clear_state(int(sender_id))
                send_owner_profile(api, int(sender_id), hoa_store.memberships(int(sender_id)), bot_username)
                return
        api.send_message(response, **target)
        return
    if command in {"/start", "/auth"}:
        if sender_id is not None:
            profile = (
                auth_store.get_profile_by_user_id(int(sender_id))
                if auth_store is not None
                else None
            )
            if profile is not None:
                send_authorized_profile(api, profile, bot_username)
            elif hoa_store is not None and (memberships := hoa_store.memberships(int(sender_id))):
                send_owner_profile(api, int(sender_id), memberships, bot_username)
            elif hoa_store is not None and store is not None and (
                verification := store.get(int(sender_id))
            ) is not None and (
                specialist_spaces := hoa_store.specialist_memberships(int(sender_id), verification.phone)
            ):
                send_specialist_profile(api, int(sender_id), specialist_spaces, bot_username)
            elif auth_store is not None and (
                state := auth_store.get_state(int(sender_id))
            ) is not None and state.role == "owner":
                if state.step == "awaiting_code" and store is not None and store.get(int(sender_id)):
                    send_owner_code_request(api, **target)
                else:
                    send_phone_request(api, **target)
            else:
                send_role_selection(api, int(sender_id))
        return
    if command == "/phone":
        if (message.get("recipient") or {}).get("chat_type") != "dialog":
            response = "Подтвердить номер можно только в личном диалоге с ботом."
        else:
            send_phone_request(api, **target)
            return
    elif command in {"/status", "/profile"}:
        profile = (
            auth_store.get_profile_by_user_id(int(sender_id))
            if auth_store is not None and sender_id is not None
            else None
        )
        if profile is not None:
            send_authorized_profile(api, profile, bot_username)
            return
        if hoa_store is not None and sender_id is not None and (memberships := hoa_store.memberships(int(sender_id))):
            send_owner_profile(api, int(sender_id), memberships, bot_username)
            return
        if hoa_store is not None and store is not None and sender_id is not None and (
            verification := store.get(int(sender_id))
        ) is not None and (
            specialist_spaces := hoa_store.specialist_memberships(int(sender_id), verification.phone)
        ):
            send_specialist_profile(api, int(sender_id), specialist_spaces, bot_username)
            return
        if auth_store is not None and sender_id is not None and (
            state := auth_store.get_state(int(sender_id))
        ) is not None and state.role == "owner":
            if state.step == "awaiting_code" and store is not None and store.get(int(sender_id)):
                send_owner_code_request(api, **target)
            else:
                send_phone_request(api, **target)
            return
        verification = (
            store.get(int(sender_id)) if store is not None and sender_id is not None else None
        )
        if verification is None:
            response = "Авторизация не завершена. Выберите роль командой /auth."
        else:
            response = (
                f"Номер {masked_phone(verification.phone)} подтверждён. "
                "Для входа выберите роль командой /auth."
            )
    elif command == "/help":
        response = HELP_TEXT
    elif command == "/ping":
        response = "pong"
    elif text and sender_id is not None and auth_store is not None and (
        state := auth_store.get_state(int(sender_id))
    ) is not None and state.role == "owner" and state.step == "awaiting_phone":
        send_phone_request(api, **target)
        return
    elif text:
        response = "Выберите способ авторизации командой /auth."
    else:
        response = "Отправьте команду /auth, чтобы начать авторизацию."

    api.send_message(response, **target)


def check_pending_group_chats(api: MaxApi, hoa_store: HoaStore) -> None:
    for chat_id in hoa_store.pending_group_chat_checks():
        try:
            chat = api.get_chat(chat_id)
            if chat.get("status") in {"left", "removed", "closed"}:
                hoa_store.forget_group_chat(chat_id)
                continue
            if chat.get("type") == "chat" and (
                hoa_store.is_group_chat_authorized(chat_id)
                or hoa_store.bind_group_chat(chat_id, str(chat.get("link") or ""), chat.get("title"))
            ):
                hoa_store.complete_group_chat_check(chat_id)
                logger.info("Чат MAX %s подтверждён для ТСЖ", chat_id)
                continue
            api.leave_chat(chat_id)
            hoa_store.forget_group_chat(chat_id)
            logger.info("Бот покинул незарегистрированный чат MAX %s", chat_id)
        except MaxApiError:
            hoa_store.defer_group_chat_check(chat_id)
            logger.exception("Не удалось проверить чат MAX %s; повторим позже", chat_id)


def request_stop(_signum: int, _frame: object) -> None:
    global running
    running = False


def main() -> None:
    load_env()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    token = os.getenv("MAX_BOT_TOKEN", "")
    database_url = os.getenv("DATABASE_URL")
    sqlite_path = Path(os.getenv("PHONE_VERIFICATION_DB", ".data/phone_verifications.sqlite3"))
    api = MaxApi(token)
    phone_store = create_phone_verification_store(database_url, sqlite_path)
    auth_store = create_chairman_authorization_store(database_url, sqlite_path)
    hoa_store = HoaStore(database_url, sqlite_path)
    gigachat = GigaChatApi.from_env()
    profile = api.get_me()
    bot_username = str(profile.get("username") or "") or None
    logger.info(
        "Подключён бот %s (@%s)",
        profile.get("name", "без имени"),
        profile.get("username", "без username"),
    )

    mini_app = MiniAppServer(
        auth_store,
        token,
        hoa_store=hoa_store,
        phone_store=phone_store,
        gigachat=gigachat,
        max_api=api,
        port=int(os.getenv("MINI_APP_PORT", "8080")),
    )
    mini_app.start()
    try:
        api.set_commands(
            [
                {"name": "start", "description": "Запустить бота"},
                {"name": "auth", "description": "Выбрать способ авторизации"},
                {"name": "phone", "description": "Подтвердить номер телефона"},
                {"name": "status", "description": "Проверить статус"},
                {"name": "profile", "description": "Открыть профиль"},
                {"name": "code", "description": "Ввести код собственника"},
                {"name": "request", "description": "Быстро создать заявку"},
                {"name": "cancel", "description": "Отменить создание заявки"},
                {"name": "help", "description": "Показать справку"},
                {"name": "ping", "description": "Проверить соединение"},
            ]
        )
    except MaxApiError:
        logger.warning("Не удалось обновить меню команд", exc_info=True)

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    marker: int | None = None
    try:
        while running:
            try:
                send_pending_visit_approvals(api, hoa_store)
                check_pending_group_chats(api, hoa_store)
                result = api.get_updates(marker=marker)
                marker = result.get("marker", marker)
                for update in result.get("updates", []):
                    try:
                        handle_update(
                            api,
                            update,
                            phone_store,
                            auth_store,
                            gigachat,
                            bot_username,
                            hoa_store,
                        )
                    except (MaxApiError, GigaChatApiError):
                        logger.exception("Не удалось обработать событие MAX")
            except MaxApiError:
                logger.exception("Ошибка Long Polling; повтор через 3 секунды")
                time.sleep(3)
    finally:
        mini_app.stop()


if __name__ == "__main__":
    main()
