# Photobooth VPS

VPS связывает Telegram с фотобудкой через сервисы Яндекса:

- media: файлы в `<event>`, уведомления `session_ready` в
  `photobooth_system/control/to_vps`;
- commands: `photobooth_system/control/to_booth`;
- command responses: общий с сессиями `photobooth_system/control/to_vps`;
- processed messages and logs: `photobooth_system/control/done` и `logs`;
- update artifacts and `status.json`: `photobooth_system/updates` на Диске;
- delivery: Telegram Bot API.

## Event workflow

1. Отправить `/event Название события` Telegram-боту.
2. Дождаться подтверждения будки и VPS.
3. После тестовой сессии убедиться, что медиа появились в event-корне, а
   `session_*.json` был перенесён из `control/to_vps` в `control/done/to_vps`.
4. Отправить `/link` и передать владельцу полученный `public_url`.

VPS раз в 10 секунд листит один стабильный `to_vps`. Ответы команд и готовые
сессии обрабатываются независимыми asyncio workers, поэтому загрузка видео не
блокирует подтверждение команды. VPS не удаляет и не перемещает медиа. При
ошибке скачивания или Telegram сообщение остаётся в `to_vps` и повторяется.
`session_ready` содержит свой `event_folder`, поэтому доставка не зависит от
одновременного переключения event на будке и VPS. `/event` отклоняется будкой,
если ещё идёт сессия или локальная загрузка.

Это protocol v2 с новыми путями. При переходе нужно дождаться пустых старых
`_sessions/inbox`, `commands/inbox` и `responses`, затем обновить будку первой,
а VPS сразу после неё. Старые каталоги после перехода не поллятся.

## Обновления

CI только собирает `photobooth-win.zip` и GitHub Release. Токен Диска в GitHub
Secrets не нужен.

```text
push в main → дождаться успешного GitHub Actions
             → /update
             → VPS перезаписывает artifacts/full.zip на Диске
             → status.json записывается последним
             → администратор отправляет /restart
```

`status.json` хранит метаданные единственного полного артефакта и совместимое
поле `active: "full"`. Будка сравнивает SHA ZIP с локальным `.update_hash`;
отдельной нумерации версий нет. `/update` использует готовый Windows release,
будке доступ к GitHub не нужен.

VPS скачивает release для проверки ZIP, а на Диск файл попадает через
server-side import конечного GitHub asset URL. После проверки size/MD5 временный
ресурс атомарно заменяет `artifacts/full.zip`, и только затем публикуется
`status.json`. Если URL-import недоступен, используется прямой PUT с прогрессом
и 30-минутным timeout.

## Переменные окружения

- `YADISK_TOKEN` — один действующий OAuth-токен Диска на VPS и будке;
- `TG_BOT_TOKEN`, `TG_CHAT_ID`, `TG_ADMIN_ID` — Telegram;
- `GITHUB_RELEASE_URL` — URL `photobooth-win.zip` из release `latest`.
- `POSTGRES_PASSWORD` — обязательный пароль локального PostgreSQL;
- `POSTGRES_DB`, `POSTGRES_USER` — необязательные имя базы и пользователь,
  по умолчанию оба используют `photobooth`.
- `DB_HOST=postgres`, `DB_PORT=5432` — адрес PostgreSQL внутри Compose-сети;
  `DB_NAME`, `DB_USER`, `DB_PASSWORD` можно задать отдельно, иначе приложение
  использует соответствующие `POSTGRES_*`.

`docker-compose.yml` загружает их из локального `.env`. Секреты не хранятся в
репозитории.

## PostgreSQL и миграции

Compose запускает PostgreSQL 18 в сервисе `postgres`; данные лежат в именованном
volume `postgres_data` и переживают пересоздание контейнеров. Порт базы наружу
не публикуется.

После успешного healthcheck PostgreSQL сервис `app` запускается и первым делом,
до Telegram и Яндекс.Диска, применяет новые SQL-файлы из `migrations/`.
Применённые версии, SHA-256 исходного SQL и время хранятся в
`schema_migrations`. Advisory lock защищает от одновременного запуска двух
экземпляров. Уже применённый файл менять нельзя: проверка checksum остановит
запуск; изменение схемы добавляется следующим файлом, например
`0002_events.sql`.

Первая миграция создаёт:

- `bot_users` — общую таблицу пользователей Telegram и MAX с уникальной парой
  `(provider, provider_user_id)`, первым и текущим параметрами `/start`;
- `bot_start_events` — отдельную неизменяемую историю запусков без JSON-массива.

Telegram `/start` уже записывается в эти таблицы. Повторная доставка одного
`update_id` не создаёт повторное событие. Поле `language_code` не хранится.
Схема готова для `provider=max`; когда появится MAX-обработчик, он будет вызывать
тот же слой `database.record_bot_start`.

Официальная документация API:

- https://yandex.ru/dev/disk-api/doc/ru/reference/upload.html
- https://yandex.ru/dev/disk-api/doc/ru/reference/meta.html
- https://yandex.ru/dev/disk-api/doc/ru/reference/move.html
- https://yandex.ru/dev/disk-api/doc/ru/reference/publish.html
