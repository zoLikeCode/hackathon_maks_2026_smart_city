from __future__ import annotations

import logging
import os
import signal
import time
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
from src.max_api import MaxApi, MaxApiError
from src.mini_app import MiniAppServer
from src.phone_verification import (
    ContactVerificationError,
    extract_phone_from_vcard,
    find_contact_payload,
    masked_phone,
)
from src.verification_store import (
    PhoneVerificationRepository,
    create_phone_verification_store,
)

WELCOME_TEXT = (
    "Добро пожаловать в проект «Умный город».\n\n"
    "Выберите, как вы хотите войти. Авторизация председателя проходит здесь, в чате. "
    "После подтверждения профиль будет доступен в мини-приложении."
)
HELP_TEXT = (
    "Для входа выберите роль командой /auth. Председателю нужно подтвердить привязанный к MAX "
    "номер и отправить протокол правления ТСЖ в PDF, DOC или DOCX.\n\n"
    "Команды: /start, /auth, /phone, /status, /profile, /help, /ping"
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


def handle_callback(
    api: MaxApi,
    update: dict[str, Any],
    phone_store: PhoneVerificationRepository | None,
    auth_store: ChairmanAuthorizationRepository | None,
    bot_username: str | None,
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
            notification = "Раздел собственника появится позже" if payload == "auth:owner" else None
            api.answer_callback(callback_id, notification=notification)
        except MaxApiError:
            logger.warning("Не удалось подтвердить callback MAX", exc_info=True)
    if payload == "auth:chairman":
        start_chairman_authorization(api, user_id, phone_store, auth_store, bot_username)
    elif payload == "auth:owner":
        api.send_message(
            "Авторизация собственника пока в разработке. Сейчас доступен сценарий председателя.",
            user_id=user_id,
        )
    return True


def handle_update(
    api: MaxApi,
    update: dict[str, Any],
    store: PhoneVerificationRepository | None = None,
    auth_store: ChairmanAuthorizationRepository | None = None,
    gigachat: GigaChatApi | None = None,
    bot_username: str | None = None,
) -> None:
    update_type = update.get("update_type")

    if update_type == "bot_started":
        user_id = (update.get("user") or {}).get("user_id")
        if user_id is not None:
            user_id = int(user_id)
            profile = auth_store.get_profile_by_user_id(user_id) if auth_store is not None else None
            if profile is not None:
                send_authorized_profile(api, profile, bot_username)
            else:
                send_role_selection(api, user_id)
        return

    if handle_callback(api, update, store, auth_store, bot_username):
        return
    if update_type != "message_created":
        return

    target = reply_target(update)
    message = update.get("message") or {}
    body = message.get("body") or {}
    text = (body.get("text") or "").strip()
    if target is None:
        logger.warning("У события message_created нет адресата для ответа")
        return

    if handle_contact(api, update, target, store, auth_store, bot_username):
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
    sender_id = (message.get("sender") or {}).get("user_id")
    if command in {"/start", "/auth"}:
        if sender_id is not None:
            profile = (
                auth_store.get_profile_by_user_id(int(sender_id))
                if auth_store is not None
                else None
            )
            if profile is not None:
                send_authorized_profile(api, profile, bot_username)
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
        verification = (
            store.get(int(sender_id)) if store is not None and sender_id is not None else None
        )
        if verification is None:
            response = "Авторизация не завершена. Выберите роль командой /auth."
        else:
            response = (
                f"Номер {masked_phone(verification.phone)} подтверждён. "
                "Для профиля председателя выберите роль командой /auth."
            )
    elif command == "/help":
        response = HELP_TEXT
    elif command == "/ping":
        response = "pong"
    elif text:
        response = "Выберите способ авторизации командой /auth."
    else:
        response = "Отправьте команду /auth, чтобы начать авторизацию."

    api.send_message(response, **target)


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
