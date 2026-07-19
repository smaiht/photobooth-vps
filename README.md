# Photobooth VPS

VPS связывает Telegram с фотобудкой через сервисы Яндекса:

- media: Яндекс Диск, `<event>/_sessions/inbox/*.json`;
- commands, responses and logs: `photobooth_system/control` на Диске;
- update artifacts and `status.json`: `photobooth_system/updates` на Диске;
- delivery: Telegram Bot API.

## Event workflow

1. Отправить `/event Название события` Telegram-боту.
2. Дождаться подтверждения будки и VPS.
3. После тестовой сессии убедиться, что медиа появились в корне, а манифест
   был перенесён из `_sessions/inbox` в `_sessions/done`.
4. Отправить `/link` и передать владельцу полученный `public_url`.

VPS не удаляет и не перемещает медиа. При ошибке скачивания или Telegram
манифест остаётся в inbox и повторяется. `/event` переключает одну активную
папку и отклоняется будкой, если ещё идёт сессия или локальная загрузка.

## Обновления

CI только собирает `photobooth-win.zip` и GitHub Release. Токен Диска в GitHub
Secrets не нужен.

```text
push в main → дождаться успешного GitHub Actions
             → /update или /update_small
             → VPS перезаписывает full.zip или small.zip на Диске
             → status.json записывается последним
             → администратор отправляет /restart
```

`status.json` хранит SHA-256 обоих артефактов и поле `active`. Будка сравнивает
SHA активного ZIP с локальным `.update_hash`; отдельной нумерации версий нет.

`/update_small` перепаковывает ZIP исходников без runtime. `/update` использует
готовый Windows release. Будке GitHub не нужен.

## Переменные окружения

- `YADISK_TOKEN` — один действующий OAuth-токен Диска на VPS и будке;
- `TG_BOT_TOKEN`, `TG_CHAT_ID`, `TG_ADMIN_ID` — Telegram;
- `GITHUB_RELEASE_URL` — URL `photobooth-win.zip` из release `latest`;
- `GITHUB_REPO_ZIP_URL` — URL ZIP ветки `main` для small update.

`docker-compose.yml` загружает их из локального `.env`. Секреты не хранятся в
репозитории.

Официальная документация API:

- https://yandex.ru/dev/disk-api/doc/ru/reference/upload.html
- https://yandex.ru/dev/disk-api/doc/ru/reference/meta.html
- https://yandex.ru/dev/disk-api/doc/ru/reference/move.html
- https://yandex.ru/dev/disk-api/doc/ru/reference/publish.html
