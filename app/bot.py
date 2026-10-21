"""Трендвотчер: Telegram-бот, расписание, отбор трендов."""
import asyncio
import html
import logging
import os
import random
import secrets
from datetime import datetime, timedelta, timezone, time as dtime
from zoneinfo import ZoneInfo

from telegram import (ForceReply, InlineKeyboardButton, InlineKeyboardMarkup,
                      LinkPreviewOptions, ReplyKeyboardMarkup, Update)
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, Defaults, MessageHandler, filters)

from . import ai, storage, trendsee

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

TZ = ZoneInfo(os.environ.get("TZ", "Europe/Moscow"))
search_lock = asyncio.Lock()
GROUPS = {"основные": "main", "соседние": "near", "английские": "en",
          "main": "main", "near": "near", "en": "en"}

# кнопки меню под полем ввода
BTN_KEYWORDS = "🔎 Поиск трендов по запросам"
BTN_QUERY_NOW = "⚡ Найти тренды по запросу сейчас"
BTN_QUERY_WEEK = "📅 Найти все тренды недели по запросу"
WEEK_LIMIT = 15


def menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[BTN_KEYWORDS], [BTN_QUERY_NOW], [BTN_QUERY_WEEK]],
                               resize_keyboard=True, is_persistent=True)


# ---------------- форматирование ----------------
def fmt_num(x) -> str:
    if x is None:
        return "—"
    return f"{int(x):,}".replace(",", " ")


def fmt_viral(x) -> str:
    if x is None:
        return ""
    return f" · вирусность ×{x:.1f}".replace(".", ",")


def e(s) -> str:
    return html.escape(str(s or ""))


def render(items: list[dict], header: str) -> str:
    return "\n".join(render_parts(items, header))


def render_parts(items: list[dict], header: str, show_niche: bool = True) -> list[str]:
    """Заголовок + по блоку на каждый ролик (для разбивки длинных сообщений)."""
    niche = storage.get_keywords().get("niche", "") if show_niche else ""
    lines = [f"<b>🔥 Тренды · {e(header)}</b>" + (f" · ниша: {e(niche.split(':')[0])}" if niche else "")]
    for i, it in enumerate(items, 1):
        title = it.get("title") or (it.get("caption") or "").split("\n")[0][:70] or it.get("author") or "Ролик"
        stats = f"👁 {fmt_num(it.get('views'))} · ❤ {fmt_num(it.get('likes'))}{fmt_viral(it.get('viral'))}"
        block = [f"\n<b>{i}. «{e(title)}»</b> · {stats}"]
        if it.get("author"):
            block.append(f"Автор: {e(it['author'])} · запрос: {e(it.get('query'))}")
        if it.get("why"):
            block.append(f"Почему залетел: {e(it['why'])}")
        if it.get("idea"):
            block.append(f"Идея для нас: {e(it['idea'])}")
        block.append(e(it["link"]))
        lines.append("\n".join(block))
    return lines


def keyboard(batch_id: str, n: int, more: bool = True) -> InlineKeyboardMarkup:
    rows = []
    if more:
        rows.append([InlineKeyboardButton("🔁 Ещё 5", callback_data=f"m:{batch_id}")])
    rows.append([InlineKeyboardButton("✍ Сценарий по №…", callback_data="noop")])
    nums = [InlineKeyboardButton(f"№{i}", callback_data=f"s:{batch_id}:{i - 1}") for i in range(1, n + 1)]
    rows.extend(nums[i:i + 5] for i in range(0, len(nums), 5))
    return InlineKeyboardMarkup(rows)


def split_messages(parts: list[str], limit: int = 3900) -> list[str]:
    msgs, cur = [], ""
    for p in parts:
        if cur and len(cur) + len(p) + 1 > limit:
            msgs.append(cur)
            cur = p.lstrip("\n")
        else:
            cur = f"{cur}\n{p}" if cur else p
    if cur:
        msgs.append(cur)
    return msgs


def header_now() -> str:
    now = datetime.now(TZ)
    part = "утро" if now.hour < 14 else "вечер"
    return f"{part} {now:%d.%m}"


# ---------------- отбор ----------------
def pick_phrases(kw: dict) -> list[str]:
    def take(group, n):
        words = [w for w in kw.get(group, []) if w.strip()]
        return random.sample(words, min(n, len(words)))
    return take("main", 3) + take("near", 2) + take("en", 1)


def filter_candidates(posts: list[dict], days: int = 30, skip_seen: bool = True,
                      limit: int = 15) -> list[dict]:
    seen = storage.get_seen() if skip_seen else set()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    uniq: dict[str, dict] = {}
    for p in posts:
        link = p["link"]
        if link in seen or link in uniq:
            continue
        if p.get("date"):
            try:
                if datetime.fromisoformat(p["date"]) < cutoff:
                    continue
            except Exception:
                pass
        uniq[link] = p

    def score(p):
        q = (p.get("query") or "").lower().split()
        cap = (p.get("caption") or "").lower()
        match = all(w in cap for w in q) if q else False
        return (match, p.get("viral") or 0, p.get("views") or 0)

    return sorted(uniq.values(), key=score, reverse=True)[:limit]


async def make_batch(pool: list[dict] | None = None) -> tuple[str, list[dict]]:
    """Возвращает (batch_id, 5 трендов). pool — кандидаты для «Ещё 5»."""
    kw = storage.get_keywords()
    niche = kw.get("niche", "")
    async with search_lock:
        storage.update_state(last_run=datetime.now(TZ).isoformat(timespec="minutes"))
        if not pool or len(pool) < 3:
            phrases = pick_phrases(kw)
            log.info("Ищу по фразам: %s", phrases)
            posts = await asyncio.wait_for(trendsee.search(phrases), timeout=600)
            pool = filter_candidates(posts)
            log.info("Кандидатов после фильтра: %d", len(pool))
        items = await ai.pick_five(pool, niche)
    shown = {i["link"] for i in items}
    rest = [c for c in pool if c["link"] not in shown]
    batch_id = secrets.token_hex(4)
    storage.save_batch(batch_id, {"items": items, "pool": rest})
    storage.add_seen(list(shown))
    storage.update_state(last_ok=datetime.now(TZ).isoformat(timespec="minutes"), last_error=None)
    return batch_id, items


# ---------------- доступ ----------------
def is_owner(update: Update) -> bool:
    st = storage.get_state()
    return bool(update.effective_user and st["owner"] == update.effective_user.id)


def is_allowed(update: Update) -> bool:
    st = storage.get_state()
    return update.effective_chat.id in st["chats"] or is_owner(update)


async def notify_owner(app: Application, text: str) -> None:
    owner = storage.get_state()["owner"]
    if owner:
        try:
            await app.bot.send_message(owner, text)
        except Exception as ex:
            log.warning("Не смог написать владельцу: %s", ex)


# ---------------- рассылка ----------------
async def send_trends(app: Application, chat_ids: list[int], pool=None, header=None) -> None:
    try:
        batch_id, items = await make_batch(pool)
    except trendsee.TrendseeAuthError as ex:
        storage.update_state(last_error=f"доступ к Trendsee: {ex}")
        await notify_owner(app, f"⚠️ Не получилось войти в Trendsee ({ex}). "
                                "Проверьте логин/пароль и активный тариф.")
        return
    except Exception as ex:
        log.exception("Ошибка поиска")
        storage.update_state(last_error=str(ex)[:300])
        await notify_owner(app, f"⚠️ Поиск трендов упал: {str(ex)[:300]}")
        return
    if not items:
        for cid in chat_ids:
            await app.bot.send_message(cid, "Новых трендов не нашлось. Попробуйте /now позже или смените слова в /keywords.")
        return
    text = render(items, header or header_now())
    for cid in chat_ids:
        try:
            await app.bot.send_message(cid, text, reply_markup=keyboard(batch_id, len(items)))
        except Exception as ex:
            log.warning("Не отправил в %s: %s", cid, ex)


async def search_query(query: str) -> list[dict]:
    async with search_lock:
        storage.update_state(last_run=datetime.now(TZ).isoformat(timespec="minutes"))
        log.info("Поиск по запросу пользователя: %s", query)
        posts = await asyncio.wait_for(trendsee.search([query]), timeout=300)
    storage.update_state(last_ok=datetime.now(TZ).isoformat(timespec="minutes"), last_error=None)
    return posts


async def query_trends(app: Application, chat_id: int, query: str, mode: str) -> None:
    """mode='now' — 5 лучших с разбором; mode='week' — все тренды за 7 дней."""
    try:
        posts = await search_query(query)
    except trendsee.TrendseeAuthError as ex:
        storage.update_state(last_error=f"доступ к Trendsee: {ex}")
        await app.bot.send_message(chat_id, "⚠️ Не получилось войти в Trendsee. Проверьте логин, пароль и тариф.")
        return
    except Exception as ex:
        log.exception("Ошибка поиска по запросу")
        storage.update_state(last_error=str(ex)[:300])
        await app.bot.send_message(chat_id, f"⚠️ Поиск не удался: {e(str(ex)[:200])}")
        return

    if mode == "week":
        items = filter_candidates(posts, days=7, skip_seen=False, limit=WEEK_LIMIT)
        if not any(p.get("date") for p in posts):
            note = "\n<i>Даты публикаций Trendsee не отдал, показываю самые вирусные без отсева по дате.</i>"
        else:
            items = [p for p in items if p.get("date")]
            note = ""
        if not items:
            await app.bot.send_message(chat_id, f"За последние 7 дней по запросу «{e(query)}» трендов не нашлось.",
                                       reply_markup=menu_kb())
            return
        batch_id = secrets.token_hex(4)
        storage.save_batch(batch_id, {"items": items, "pool": [], "query": query})
        msgs = split_messages(render_parts(items, f"все тренды недели · «{query}»", show_niche=False))
        for i, text in enumerate(msgs):
            last = i == len(msgs) - 1
            await app.bot.send_message(chat_id, text + (note if last else ""),
                                       reply_markup=keyboard(batch_id, len(items), more=False) if last else None)
        return

    pool = filter_candidates(posts, days=30, skip_seen=False, limit=15)
    if not pool:
        await app.bot.send_message(chat_id, f"По запросу «{e(query)}» свежих трендов не нашлось. "
                                            "Попробуйте сформулировать иначе.", reply_markup=menu_kb())
        return
    async with search_lock:
        items = await ai.pick_five(pool, storage.get_keywords().get("niche", ""))
    shown = {i["link"] for i in items}
    rest = [c for c in pool if c["link"] not in shown]
    batch_id = secrets.token_hex(4)
    storage.save_batch(batch_id, {"items": items, "pool": rest, "query": query})
    storage.add_seen(list(shown))
    await app.bot.send_message(chat_id, render(items, f"по запросу «{query}»"),
                               reply_markup=keyboard(batch_id, len(items), more=bool(rest)))


async def scheduled(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chats = storage.get_state()["chats"]
    if chats:
        await send_trends(ctx.application, chats)


def schedule_jobs(app: Application) -> None:
    for job in app.job_queue.get_jobs_by_name("trends"):
        job.schedule_removal()
    for t in storage.get_state()["times"]:
        h, m = map(int, t.split(":"))
        app.job_queue.run_daily(scheduled, time=dtime(h, m, tzinfo=TZ), name="trends")
    log.info("Расписание: %s (%s)", storage.get_state()["times"], TZ)


# ---------------- команды ----------------
HELP = ("Кнопки внизу: поиск по ключевым словам, по своему запросу и тренды недели.\n\n"
        "Команды:\n/menu — показать кнопки\n/now — тренды сейчас\n/keywords — ключевые слова\n"
        "/time 09:00 19:00 — время рассылки\n/status — состояние\n/addchat — добавить этот чат (владелец)")


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    st = storage.get_state()
    chat_id = update.effective_chat.id
    if st["owner"] is None:
        storage.update_state(owner=update.effective_user.id, chats=[chat_id])
        await update.message.reply_text("Готово, вы владелец бота, этот чат подключён к рассылке.\n\n" + HELP, reply_markup=menu_kb())
        return
    if is_owner(update) and chat_id not in st["chats"]:
        storage.update_state(chats=st["chats"] + [chat_id])
        await update.message.reply_text("Этот чат подключён к рассылке трендов.\n\n" + HELP, reply_markup=menu_kb())
        return
    if is_allowed(update):
        await update.message.reply_text("Я на месте. " + HELP, reply_markup=menu_kb())
    else:
        await update.message.reply_text("Доступ закрыт. Попросите владельца бота добавить этот чат.")


async def cmd_addchat(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    st = storage.get_state()
    cid = int(ctx.args[0]) if ctx.args else update.effective_chat.id
    if cid not in st["chats"]:
        storage.update_state(chats=st["chats"] + [cid])
    await update.message.reply_text(f"Чат {cid} добавлен. Всего чатов: {len(storage.get_state()['chats'])}")


async def cmd_now(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    if search_lock.locked():
        await update.message.reply_text("Поиск уже идёт, пришлю, как будет готово.")
    else:
        await update.message.reply_text("Ищу тренды, это 1-3 минуты…")
    asyncio.create_task(send_trends(ctx.application, [update.effective_chat.id]))


async def cmd_keywords(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    text = update.message.text.split(maxsplit=1)
    if len(text) > 1 and ":" in text[1]:
        if not is_owner(update):
            await update.message.reply_text("Менять слова может только владелец.")
            return
        group_name, words = text[1].split(":", 1)
        group = GROUPS.get(group_name.strip().lower())
        if not group:
            await update.message.reply_text("Группа должна быть: основные, соседние или английские.")
            return
        storage.set_keywords(group, [w.strip() for w in words.split(",") if w.strip()])
    kw = storage.get_keywords()
    await update.message.reply_text(
        f"<b>Ниша:</b> {e(kw.get('niche'))}\n\n"
        f"<b>Основные:</b> {e(', '.join(kw.get('main', [])))}\n\n"
        f"<b>Соседние:</b> {e(', '.join(kw.get('near', [])))}\n\n"
        f"<b>Английские:</b> {e(', '.join(kw.get('en', [])))}\n\n"
        "Поменять группу целиком:\n<code>/keywords основные: слово 1, слово 2, …</code>")


async def cmd_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    if ctx.args:
        if not is_owner(update):
            await update.message.reply_text("Время меняет только владелец.")
            return
        try:
            times = []
            for a in ctx.args:
                h, m = map(int, a.split(":"))
                assert 0 <= h < 24 and 0 <= m < 60
                times.append(f"{h:02d}:{m:02d}")
        except Exception:
            await update.message.reply_text("Формат: /time 09:00 19:00")
            return
        storage.update_state(times=times)
        schedule_jobs(ctx.application)
    await update.message.reply_text(f"Рассылка в: {', '.join(storage.get_state()['times'])} ({TZ.key})")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    st = storage.get_state()
    claude = "подключён" if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") else "НЕ подключён (нет CLAUDE_CODE_OAUTH_TOKEN)"
    await update.message.reply_text(
        f"Последний поиск: {st['last_run'] or '—'}\n"
        f"Последний успешный: {st['last_ok'] or '—'}\n"
        f"Ошибка: {e(st['last_error']) or 'нет'}\n"
        f"Поиск идёт сейчас: {'да' if search_lock.locked() else 'нет'}\n"
        f"Рассылка: {', '.join(st['times'])} ({TZ.key}), чатов: {len(st['chats'])}\n"
        f"Claude: {claude}\n"
        f"Уже отправлено роликов: {len(storage.get_seen())}")


# ---------------- кнопки ----------------
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_allowed(update):
        await q.answer("Нет доступа")
        return
    if q.data == "noop":
        await q.answer("Нажмите номер ролика ниже")
        return
    parts = q.data.split(":")
    batch = storage.get_batch(parts[1])
    if not batch:
        await q.answer("Подборка устарела, нажмите /now")
        return
    chat_id = update.effective_chat.id
    if parts[0] == "m" and batch.get("query"):
        pool = batch.get("pool") or []
        if not pool:
            await q.answer("Больше трендов по этому запросу нет")
            return
        await q.answer("Отбираю ещё 5…")

        async def more():
            async with search_lock:
                items = await ai.pick_five(pool, storage.get_keywords().get("niche", ""))
            shown = {i["link"] for i in items}
            rest = [c for c in pool if c["link"] not in shown]
            bid = secrets.token_hex(4)
            storage.save_batch(bid, {"items": items, "pool": rest, "query": batch["query"]})
            storage.add_seen(list(shown))
            await ctx.bot.send_message(chat_id, render(items, f"по запросу «{batch['query']}» · ещё"),
                                       reply_markup=keyboard(bid, len(items), more=bool(rest)))
        asyncio.create_task(more())
    elif parts[0] == "m":
        await q.answer("Ищу ещё 5…")
        asyncio.create_task(send_trends(ctx.application, [chat_id], pool=batch["pool"],
                                        header=header_now() + " · ещё"))
    elif parts[0] == "s":
        n = int(parts[2])
        item = batch["items"][n]
        await q.answer("Пишу сценарий…")

        async def job():
            try:
                text = await ai.write_script(item, storage.get_keywords().get("niche", ""))
                await ctx.bot.send_message(chat_id, f"<b>✍ Сценарий по №{n + 1}</b>\n{e(item['link'])}\n\n{e(text)}"[:4090])
            except Exception as ex:
                await ctx.bot.send_message(chat_id, f"Не получилось написать сценарий: {e(str(ex)[:200])}")
        asyncio.create_task(job())


# ---------------- кнопки меню ----------------
async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if is_allowed(update):
        await update.message.reply_text("Выберите, что найти 👇", reply_markup=menu_kb())


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_allowed(update):
        return
    text = (update.message.text or "").strip()
    if text == BTN_KEYWORDS:
        ctx.chat_data.pop("await", None)
        await cmd_now(update, ctx)
        return
    if text in (BTN_QUERY_NOW, BTN_QUERY_WEEK):
        ctx.chat_data["await"] = "now" if text == BTN_QUERY_NOW else "week"
        what = "5 лучших трендов" if text == BTN_QUERY_NOW else "все тренды за последние 7 дней"
        await update.message.reply_text(
            f"Напишите запрос — пришлю {what}.\nНапример: <i>шкаф купе</i>, <i>кухня в хрущёвке</i>, <i>home decor</i>",
            reply_markup=ForceReply(selective=True, input_field_placeholder="ваш запрос"))
        return
    mode = ctx.chat_data.pop("await", None)
    if not mode:
        return
    query = text[:80]
    note = "Ищу 5 лучших" if mode == "now" else "Собираю все тренды недели"
    busy = " (дождусь окончания текущего поиска)" if search_lock.locked() else ""
    await update.message.reply_text(f"{note} по запросу «{e(query)}», это 1-2 минуты…{busy}",
                                    reply_markup=menu_kb())
    asyncio.create_task(query_trends(ctx.application, update.effective_chat.id, query, mode))


async def post_init(app: Application) -> None:
    schedule_jobs(app)
    await app.bot.set_my_commands([
        ("menu", "Кнопки поиска"), ("now", "Тренды сейчас"), ("keywords", "Ключевые слова"),
        ("time", "Время рассылки"), ("status", "Состояние"), ("addchat", "Добавить чат"),
    ])


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Нет TELEGRAM_BOT_TOKEN")
    ai.check_env()
    storage.get_keywords()
    defaults = Defaults(parse_mode=ParseMode.HTML, tzinfo=TZ,
                        link_preview_options=LinkPreviewOptions(is_disabled=True))
    app = Application.builder().token(token).defaults(defaults).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("addchat", cmd_addchat))
    app.add_handler(CommandHandler("now", cmd_now))
    app.add_handler(CommandHandler("keywords", cmd_keywords))
    app.add_handler(CommandHandler("time", cmd_time))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Трендвотчер запущен")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
