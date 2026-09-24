# Реестр использованных username

Сервис не даёт двум людям одновременно взять один и тот же username. Он хранит записи в лёгкой SQLite-базе и подходит для диапазона от нескольких тысяч до примерно 200 000 строк.

Публичный адрес API:

```text
https://lbam.tech/username-registry
```

## Как это работает

У каждой записи есть три рабочих поля:

| Поле | Что хранится |
| --- | --- |
| `username` | Уникальный username, если он есть — запасной способ найти пользователя |
| `user_id` | Постоянный Telegram ID пользователя |
| `access_hash` | Telegram access hash для обращения к пользователю по ID |
| `chat` | Название или описание чата, которое нужно вернуть клиенту |
| `used` | `no` для ещё не выданной записи, `yes` сразу после выдачи клиенту |

При добавлении и проверке username очищается от пробелов, один начальный символ `@` убирается, а регистр приводится к нижнему. Поэтому `@Example`, `Example` и `example` — одна и та же запись. Максимальная длина username — 128 символов; `chat` должен быть непустой строкой.

`user_id` всегда уникален. Username тоже уникален, когда он есть; у части Telegram-пользователей публичного username нет, и такие записи хранятся только по `user_id + access_hash`.

Есть два режима выдачи. Старый `/v1/claim` проверяет конкретный username. Новый `/v1/claim-next` берёт верхнюю ещё не выданную запись либо первые `n` таких записей. Порядок — порядок добавления в базу. При выдаче сервис в одной SQLite-транзакции сначала отмечает все выбранные строки `used = yes`, затем отвечает клиенту. Поэтому два одновременных запроса получат разные записи; пересечений не будет.

Пары `user_id + access_hash` достаточно, чтобы написать пользователю через Telethon. Важно: запись считается выданной, даже если клиент после ответа не смог отправить сообщение. Это защищает от повторной выдачи; ошибки отправки клиент обязан записывать в свой журнал для ручной обработки.

## Ключи доступа

Есть два разных ключа. Они не хранятся в репозитории.

| Ключ | Кому давать | Что разрешает |
| --- | --- | --- |
| `REGISTRY_API_KEY` | Друзьям и обычным клиентам | Только проверить и забрать username через `/v1/claim` |
| `REGISTRY_ADMIN_KEY` | Только администратору | Добавлять записи и импортировать CSV |

На сервере оба ключа лежат в `/etc/username-registry.env`, доступ к файлу есть только у `root`. Не отправляйте admin-ключ другим людям и не добавляйте ключи в Git, `.env` проекта или сообщения в публичных чатах.

## Обычное использование: взять следующую запись

Обычному клиенту не нужно знать username заранее. Взять одну следующую запись:

```bash
cd username-registry
export REGISTRY_URL='https://lbam.tech/username-registry'
export REGISTRY_API_KEY='полученный-клиентский-ключ'
python3 client.py take
```

Взять первые 10 ещё не выданных записей:

```bash
python3 client.py take 10
```

Ответ содержит `items`; у выдачи одной записи дополнительно есть удобное поле `item`:

```json
{"available":true,"requested":1,"claimed":1,"items":[{"username":"some_username","user_id":"123456789","access_hash":"987654321","chat":"Название чата"}],"exhausted":false}
```

Если свободных строк нет, сервис вернёт `available: false`, пустой `items` и `exhausted: true`. Допускается от 1 до 10 000 записей за один запрос.

## Старый режим: проверить конкретный username

Самый простой вариант — командный клиент из этого каталога:

```bash
cd username-registry
export REGISTRY_URL='https://lbam.tech/username-registry'
export REGISTRY_API_KEY='полученный-клиентский-ключ'
python3 client.py claim @some_username
```

Ответ при первой успешной выдаче:

```json
{"available": true, "username": "some_username", "user_id": "123456789", "access_hash": "987654321", "chat": "Название чата"}
```

Ответы, когда запись не может быть выдана:

```json
{"available": false, "reason": "already_used"}
```

```json
{"available": false, "reason": "not_found"}
```

`already_used` означает, что этот username уже был выдан. `not_found` означает, что username отсутствует в загруженной базе. В обоих случаях данные пользователя и `chat` не возвращаются.

Тот же запрос без клиента:

```bash
curl -X POST 'https://lbam.tech/username-registry/v1/claim' \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: $REGISTRY_API_KEY" \
  --data '{"username":"@some_username"}'
```

## Администрирование базы

Перед административными командами установите URL и **admin-ключ**:

```bash
cd username-registry
export REGISTRY_URL='https://lbam.tech/username-registry'
export REGISTRY_ADMIN_KEY='секретный-admin-ключ'
```

### Добавить одну запись

```bash
python3 client.py add some_username 123456789 987654321 'Название чата'
```

Новая запись всегда добавляется с `used = no`. При повторном username или `user_id` команда завершится ошибкой HTTP `409`; существующая строка при этом не изменяется. Для пользователя без публичного username передайте вместо username символ `-`.

HTTP-вариант:

```bash
curl -X POST 'https://lbam.tech/username-registry/v1/records' \
  -H 'Content-Type: application/json' \
  -H "X-Admin-Key: $REGISTRY_ADMIN_KEY" \
  --data '{"username":"some_username","user_id":"123456789","access_hash":"987654321","chat":"Название чата"}'
```

### Загрузить стартовую базу из CSV

Создайте UTF-8 CSV с обязательными заголовками `username,user_id,access_hash`. Столбец `chat` можно добавить в файл либо передать одним параметром для всех строк:

```csv
username,user_id,access_hash,chat
first_username,123456789,987654321,Чат предпринимателей
second_username,123456790,-987654322,Рабочий чат
```

Импортируйте его:

```bash
python3 client.py import-csv users.csv
```

Выгрузка [сборщика участников чата](../users-from-chat/README.md) уже содержит `username`, `user_id` и `access_hash`, но не содержит название чата. Пользователи без username тоже импортируются: их можно использовать для отправки по `user_id + access_hash`, но нельзя найти запросом по username. Загрузить выгрузку можно так:

```bash
python3 client.py import-csv ../users-from-chat/telegram_chat_users.csv \
  --chat 'Название исходного чата'
```

Импорт атомарный: если в самом CSV есть дубли username/user_id или хотя бы одна такая запись уже имеется в БД, не добавится ни одна строка из этого файла. Для повторной выгрузки из другого чата передайте `--ignore-existing`: существующие записи останутся без изменений, а новые будут добавлены. За один импорт допускается до 200 000 записей; тело HTTP-запроса ограничено 64 МБ.

## API для разработки клиента

Все тела запросов и ответы — JSON в UTF-8. Все пути ниже добавляются к `https://lbam.tech/username-registry`.

| Метод и путь | Заголовок | Тело | Успешный ответ |
| --- | --- | --- | --- |
| `POST /v1/claim` | `X-API-Key` | `{"username":"..."}` | `200`, `available` и при успехе данные Telegram + `chat` |
| `POST /v1/claim-next` | `X-API-Key` | `{"n":1}` или `{}` | `200`, первые `n` невыданных записей, сразу отмеченные `used=yes` |
| `POST /v1/records` | `X-Admin-Key` | `{"username":"...","user_id":"...","access_hash":"...","chat":"..."}` | `201`, `{"ok":true,"used":"no"}` |
| `POST /v1/import` | `X-Admin-Key` | `{"records":[...],"ignore_existing":false}` | `201`, число добавленных и пропущенных записей |
| `GET /healthz` | не нужен | — | `200`, `{"ok":true}` |

Основные коды ошибок:

| Код | Значение |
| --- | --- |
| `400` | Некорректный JSON, username или chat |
| `401` | Нет ключа или ключ неверный |
| `404` | Неизвестный путь API |
| `409` | Повторный username или `user_id` при добавлении или импорте |
| `413` | Слишком большой запрос |
| `503` | База временно недоступна — повторите запрос позже |

Для `/v1/claim` отсутствие username или уже использованная запись — это нормальный ответ `200` с `available: false`, а не HTTP-ошибка. Для `/v1/claim-next` пустая очередь — тоже нормальный ответ `200`; некорректное `n` — `400`.

## Развёртывание и обслуживание сервера

На сервере сервис называется `username-registry.service` и работает от отдельного системного пользователя. Он слушает только `127.0.0.1:8711`; Nginx принимает внешний HTTPS-трафик на `lbam.tech` и проксирует только путь `/username-registry/` к сервису.

Проверить состояние и последние записи журнала:

```bash
systemctl status username-registry.service
journalctl -u username-registry.service -n 100 --no-pager
```

Перезапустить после обновления `server.py`:

```bash
systemctl restart username-registry.service
curl --fail https://lbam.tech/username-registry/healthz
```

Файлы на сервере:

| Путь | Назначение |
| --- | --- |
| `/opt/username-registry/server.py` | Код API |
| `/opt/username-registry/client.py` | CLI-клиент |
| `/var/lib/username-registry/registry.sqlite3` | SQLite-база |
| `/etc/username-registry.env` | Адрес БД, порт и оба секретных ключа |
| `/etc/systemd/system/username-registry.service` | Настройка systemd |

Для резервной копии SQLite используйте встроенную команду, а не копирование файла во время работы сервиса:

```bash
sqlite3 /var/lib/username-registry/registry.sqlite3 \
  ".backup '/root/registry-$(date +%F).sqlite3'"
```

В API намеренно нет удаления, редактирования или сброса `used` — это защищает историю выдачи. Если такие операции понадобятся, их стоит добавить отдельными admin-методами с журналом действий, а не менять БД вручную.

## Локальная проверка кода

Сервис использует только стандартную библиотеку Python. Перед развёртыванием можно запустить тесты:

```bash
cd username-registry
python3 -m unittest -v
```
