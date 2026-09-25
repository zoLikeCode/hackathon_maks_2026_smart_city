# Бот для MAX — «Умный город»

Python-бот для MAX с авторизацией председателя ТСЖ, проверкой протокола через GigaChat Ultra,
PostgreSQL и mini app с профилем. Основной способ запуска — Docker Compose.

## Запуск через Docker

Понадобится Docker Desktop с поддержкой `docker compose`.

1. Создайте `.env` из примера:

   ```powershell
   Copy-Item .env.example .env
   ```

2. Заполните в `.env` как минимум:

   ```text
   MAX_BOT_TOKEN=токен_бота_MAX
   POSTGRES_PASSWORD=длинный_случайный_пароль
   ```

   Для GigaChat также задайте `GIGACHAT_CREDENTIALS`.

3. Запустите весь проект одной командой:

   ```powershell
   .\start.ps1
   ```

   Скрипт соберёт и запустит PostgreSQL и бота. Локальный mini app будет доступен по адресу
   `http://localhost:8080`.

4. Проверьте состояние и логи:

   ```powershell
   docker compose ps
   docker compose logs -f bot
   ```

Локальный Compose запускает два сервиса:

- `bot` — MAX-бот с Long Polling и HTTP-сервер mini app на порту `8080`;
- `postgres` — PostgreSQL 16 с проверкой готовности перед запуском бота.

Таблицы подтверждённых номеров, состояний авторизации, пространств ТСЖ, профилей председателей и
проверок протоколов создаются автоматически. Данные PostgreSQL находятся в Docker volume
`postgres_data` и сохраняются при пересборке или обычной остановке контейнеров.

Остановить сервисы:

```powershell
docker compose down
```

Посмотреть записи напрямую:

```powershell
docker compose exec postgres psql -U smart_city -d smart_city `
  -c "SELECT user_id, phone, verified_at FROM phone_verifications;"
```

Команда `docker compose down -v` удаляет volume вместе со всеми данными базы.

## Авторизация председателя

Сценарий полностью проходит в личном чате:

1. Пользователь выбирает роль «Я председатель». Кнопка собственника пока возвращает заглушку.
2. Бот запрашивает номер через кнопку `request_contact` и проверяет HMAC-подпись MAX.
3. Бот запрашивает протокол правления ТСЖ в PDF, DOC или DOCX.
4. GigaChat проверяет тип документа, кворум, вопрос об избрании председателя, результаты
   голосования, решение, ФИО и наличие подписи.
5. При успешной проверке создаются пространство ТСЖ и профиль председателя. Хеш документа
   защищает протокол от повторного присвоения другому номеру.
6. При следующем входе подтверждённый номер автоматически получает свой профиль без повторной
   загрузки протокола.

Обычный контакт из телефонной книги подтверждённым не считается.

Начать можно командой `/auth`, проверить состояние — `/status`, открыть профиль — `/profile`.

## Mini app

Встроенный сервер отдаёт страницу профиля и endpoint `/api/profile`. Запрос профиля принимает
только подписанный `window.WebApp.initData`, проверяет его HMAC токеном бота и срок жизни в один
час. Данные из `initDataUnsafe` для авторизации не используются.

Для подключения mini app к MAX нужен публичный HTTPS URL:

1. Публичный mini app развёрнут по постоянному адресу `https://213-171-9-160.sslip.io`.
2. На платформе MAX для партнёров откройте настройки бота.
3. В поле mini app вставьте этот HTTPS URL и сохраните кнопку открытия.
4. Отправьте боту `/profile`, чтобы получить новую кнопку.

На сервере Caddy автоматически получает и обновляет TLS-сертификат. Для запуска или обновления
проекта выполните в каталоге приложения:

```sh
./server-start.sh
```

Серверный Compose запускает `bot`, `postgres` и `caddy`. Проверка публичной доступности:

```sh
curl https://213-171-9-160.sslip.io/health
```

Локальная проверка доступности:

```powershell
Invoke-RestMethod http://localhost:8080/health
```

## Локальный запуск без Docker

Если `DATABASE_URL` не задан, бот использует SQLite-файл из `PHONE_VERIFICATION_DB`:

```powershell
python -m pip install .
python -m src.bot
```

Для прямого подключения к отдельному PostgreSQL задайте стандартный URL:

```text
DATABASE_URL=postgresql://user:password@host:5432/database
```

## Проверка

```powershell
python -m unittest discover -s tests -v
```

API MAX использует цепочку сертификатов Минцифры. Официальный CA-bundle находится в
`certs/russiantrustedca.pem` и подключается только к HTTP-клиенту проекта; системное хранилище
сертификатов не изменяется.

## GigaChat 3 Ultra

REST-клиент находится в `src/gigachat_api.py`. Он получает и кеширует access token, отправляет
текстовые запросы, загружает изображения и документы для анализа, а после ответа удаляет их из
хранилища GigaChat.

Настройки в `.env`:

```text
GIGACHAT_CREDENTIALS=ваш_Authorization_Key
GIGACHAT_SCOPE=GIGACHAT_API_PERS
GIGACHAT_MODEL=GigaChat-3-Ultra
```

Пример отдельного вызова:

```python
from src.bot import load_env
from src.gigachat_api import GigaChatApi

load_env()
client = GigaChatApi.from_env()
answer = client.generate_text("Кратко объясни, что такое ТСЖ")
print(answer)
```

Ключ авторизации и access token не должны попадать в логи.
