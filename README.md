# Трендвотчер

Каждый день в 09:00 и 19:00 (по Москве) ищет в Trendsee залетевшие рилсы по ключевым словам и соседним нишам и присылает в Telegram 5 новых: просмотры, лайки, вирусность, «почему залетел», «идея для нас», ссылка. Кнопки «Ещё 5» и «Сценарий по №N».

Команды бота: `/now`, `/keywords`, `/time 09:00 19:00`, `/status`, `/addchat`.

## Переменные окружения

| Переменная | Что туда |
|---|---|
| `TELEGRAM_BOT_TOKEN` | токен от @BotFather |
| `TRENDSEE_LOGIN` | почта Trendsee |
| `TRENDSEE_PASSWORD` | пароль Trendsee |
| `CLAUDE_CODE_OAUTH_TOKEN` | выполнить `claude setup-token` на своём компьютере (нужна подписка Claude Pro/Max) |
| `TZ` | `Europe/Moscow` |

**Не добавляйте `ANTHROPIC_API_KEY`** — иначе Claude пойдёт по платному API.
Без `CLAUDE_CODE_OAUTH_TOKEN` бот работает, но присылает тренды без разбора и не пишет сценарии.

## Railway

1. New Project → Deploy from GitHub repo → этот репозиторий (собирается по `Dockerfile`).
2. Volume на 1 ГБ с путём `/data` (там вход в Trendsee, отправленные ролики, настройки).
3. Variables — из таблицы выше.
4. Написать боту `/start` (первый написавший становится владельцем), затем `/now`.

Чтобы тренды приходили в группу: добавьте бота в группу и напишите там `/start` от владельца.

## Локально

```bash
pip install -r requirements.txt && playwright install chromium
npm i -g @anthropic-ai/claude-code
export DATA_DIR=./data TELEGRAM_BOT_TOKEN=... TRENDSEE_LOGIN=... TRENDSEE_PASSWORD=... CLAUDE_CODE_OAUTH_TOKEN=...
python -m app.bot
```

## Где что

- `app/trendsee.py` — браузер, вход, запросы к API Trendsee, разбор ответа (первый ответ сохраняется в `/data/sample_response.json`).
- `app/ai.py` — отбор 5 лучших и сценарии через `claude -p --model sonnet`.
- `app/bot.py` — Telegram, расписание, команды.
- `keywords.default.json` — стартовые ключевые слова (дальше меняются командой `/keywords`).
- `STYLE.md` — голос бренда для идей и сценариев.
