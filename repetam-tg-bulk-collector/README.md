# Repetam bulk Telegram lead collector

Цель: массово собрать публичные Telegram usernames активных репетиторов и представителей онлайн-школ из новых источников и убрать уже собранные аккаунты.

## Что делает
- проходит по списку Telegram-групп;
- собирает публичные usernames авторов сообщений;
- пытается добавить видимых участников, если Telegram разрешает;
- для смешанных групп оставляет кандидатов по текстовым признакам;
- дедуплицирует;
- умеет исключить уже имеющуюся базу;
- не отправляет никаких сообщений.

## Установка
```bash
python -m venv .venv
source .venv/bin/activate
pip install -U telethon
```

Создай Telegram API credentials на https://my.telegram.org и задай:

```bash
export TG_API_ID=123456
export TG_API_HASH='...'
```

Если у тебя уже есть список собранных Telegram usernames, положи его в:
`existing_usernames.txt`
по одному username на строку. Можно с `@`.

Запуск:
```bash
python repetam_bulk_tg_collector.py
```

Результат:
`repetam_public_tg_leads.csv`

Для более глубокого прохода:
```bash
MAX_MESSAGES_PER_CHAT=50000 python repetam_bulk_tg_collector.py
```

Если хочешь сохранить и низкоуверенных кандидатов:
```bash
INCLUDE_LOW_CONFIDENCE=1 python repetam_bulk_tg_collector.py
```

Важно: Telegram ограничивает выдачу полного списка участников некоторых больших групп. Поэтому скрипт также собирает авторов сообщений — это часто даже качественнее, потому что получаются активные пользователи.
