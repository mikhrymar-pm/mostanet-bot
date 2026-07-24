# Mostanet Bot — мониторинг билетов Сахалин → Курилы

## Структура

```
mostanet_bot/
├── bot.py           — Telegram бот (запускать)
├── monitor.py       — логика проверки билетов
├── discover_api.py  — утилита поиска API (запустить ОДИН РАЗ)
├── config.py        — настройки
├── .env             — токены (создать из .env.example)
└── requirements.txt
```

## Установка

```bash
pip install -r requirements.txt
playwright install chromium
```

## Первый запуск: найти API

```bash
python discover_api.py
```

Откроется браузер. Поищи любой рейс на сайте руками.
Скрипт перехватит все API-запросы и сохранит в `captured_api.json`.

Посмотри файл — найди endpoint с расписанием/билетами.
Заполни в `monitor.py`:
- `API_SEARCH_URL`
- `build_search_payload()`
- `parse_tickets()`

## Настройка

1. Скопируй `.env.example` → `.env`
2. Заполни токен бота и chat_id
3. Заполни `ROUTES` в `config.py`

## Запуск бота

```bash
python bot.py
```

## Команды бота

| Команда | Описание |
|---------|----------|
| `/start` | Приветствие |
| `/check` | Проверить прямо сейчас |
| `/dates 2024-07-20` | Добавить дату |
| `/dates +30` | Следить за ближайшими 30 днями |
| `/cleardates` | Очистить даты |
| `/status` | Текущие настройки |
