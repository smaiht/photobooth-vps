# Photobooth VPS

VPS связывает Telegram с фотобудкой через сервисы Яндекса:

- media: Яндекс Диск, `<event>/_sessions/inbox/*.json`;
- commands, logs and updates: Яндекс Заметки;
- delivery: Telegram Bot API.

## Event workflow

1. Создать отдельную папку ивента на Яндекс Диске.
2. Указать одинаковый `yadisk_folder` в конфигурации будки и VPS.
3. Запустить VPS и проверить лог `watching /<event>/_sessions/inbox`.
4. После тестовой сессии убедиться, что медиа появились в корне, а манифест
   был перенесён из `_sessions/inbox` в `_sessions/done`.
5. Опубликовать корневую папку средствами Яндекс Диска и передать владельцу
   полученный `public_url`.

VPS не удаляет и не перемещает медиа. При ошибке скачивания или Telegram
манифест остаётся в inbox и повторяется. `media_transport: "notes"` включает
старый ZIP transport на случай аварийного отката.

Официальная документация API:

- https://yandex.ru/dev/disk-api/doc/ru/reference/upload.html
- https://yandex.ru/dev/disk-api/doc/ru/reference/meta.html
- https://yandex.ru/dev/disk-api/doc/ru/reference/move.html
- https://yandex.ru/dev/disk-api/doc/ru/reference/publish.html
