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

`docker-compose.yml` загружает их из локального `.env`. Секреты не хранятся в
репозитории.

Официальная документация API:

- https://yandex.ru/dev/disk-api/doc/ru/reference/upload.html
- https://yandex.ru/dev/disk-api/doc/ru/reference/meta.html
- https://yandex.ru/dev/disk-api/doc/ru/reference/move.html
- https://yandex.ru/dev/disk-api/doc/ru/reference/publish.html
