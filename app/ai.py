"""Вызовы Claude через Claude Code CLI по подписке (CLAUDE_CODE_OAUTH_TOKEN)."""
import asyncio
import json
import logging
import os
import re

from .storage import ROOT

log = logging.getLogger("ai")
_claude_lock = asyncio.Lock()


def style_text() -> str:
    p = ROOT / "STYLE.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def check_env() -> None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        log.warning("ВНИМАНИЕ: задан ANTHROPIC_API_KEY — Claude пойдёт по платному API, а не по подписке. "
                    "Удалите эту переменную в Railway.")
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        log.warning("CLAUDE_CODE_OAUTH_TOKEN не задан — разбор «почему залетел» и сценарии работать не будут, "
                    "агент пришлёт тренды без разбора.")


async def ask_claude(prompt: str, timeout: int = 240) -> str:
    async with _claude_lock:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", "--model", "sonnet", "--output-format", "text",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd="/tmp",
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("Claude не ответил вовремя")
        if proc.returncode != 0:
            raise RuntimeError(f"Claude: {(err or out).decode('utf-8', 'ignore')[:400]}")
        return out.decode("utf-8", "ignore").strip()


def _fmt_candidate(i, c):
    return (f"#{i} | запрос: {c.get('query')} | автор: {c.get('author') or '-'} | "
            f"просмотры: {c.get('views')} | лайки: {c.get('likes')} | вирусность: {c.get('viral')} | "
            f"дата: {(c.get('date') or '-')[:10]}\nописание: {(c.get('caption') or '-')[:500]}")


async def pick_five(candidates: list[dict], niche: str) -> list[dict]:
    """Claude выбирает 5 лучших и пишет «почему» и «идею». При ошибке — топ-5 без разбора."""
    if not candidates:
        return []
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return [dict(c) for c in candidates[:5]]
    listing = "\n\n".join(_fmt_candidate(i, c) for i, c in enumerate(candidates))
    prompt = f"""Ты трендвотчер контент-команды. Наша ниша: {niche}.
Голос бренда (для идей):
{style_text()[:2500]}

Вот кандидаты — залетевшие короткие ролики из Trendsee:

{listing}

Выбери до 5 лучших. Берём ТОЛЬКО ролики, где речь или кадр про мебель и всё, что с ней связано:
диваны, шкафы, кухни, столы, кровати, хранение, сборка мебели, было/стало, семья за столом
(уроки, застолье), юмор про мебель, интерьер комнаты. Ролики про недвижимость, психологию,
бьюти, отношения и прочее без мебели — пропускай, даже если они очень вирусные.
Если подходящих меньше 5, верни меньше.
Для каждого напиши:
- title: короткое название ролика по сути (до 7 слов, по описанию, не выдумывай)
- why: почему залетел — механика (хук, формат, триггер), одна фраза
- idea: идея, как снять похожий ролик у нас, одна фраза
Не выдумывай цифры. Ответь ТОЛЬКО JSON-массивом без пояснений:
[{{"n": номер_кандидата, "title": "...", "why": "...", "idea": "..."}}]"""
    try:
        text = await ask_claude(prompt)
        m = re.search(r"\[.*\]", text, re.S)
        data = json.loads(m.group(0)) if m else []
        out = []
        for row in data:
            n = int(row.get("n"))
            if 0 <= n < len(candidates) and candidates[n]["link"] not in {o["link"] for o in out}:
                c = dict(candidates[n])
                c.update(title=row.get("title", ""), why=row.get("why", ""), idea=row.get("idea", ""))
                out.append(c)
        if out:
            return out[:5]
        log.warning("Claude вернул непонятный ответ: %s", text[:500])
    except Exception as e:
        log.warning("Отбор через Claude не удался: %s", e)
    return [dict(c) for c in candidates[:5]]


async def write_script(item: dict, niche: str) -> str:
    prompt = f"""Напиши сценарий рилса на 25-40 секунд для нашего аккаунта. Ниша: {niche}.
Возьми механику залетевшего ролика и перенеси её к нам.

Залетевший ролик:
{_fmt_candidate(0, item)}
Почему залетел: {item.get('why', '-')}
Идея для нас: {item.get('idea', '-')}

Голос и правила бренда:
{style_text()}

Формат ответа (обычный текст, без markdown-разметки):
ХУК (0-3 сек): что в кадре и что говорим
Далее по секундам: кадр — текст за кадром / на экране
ПРИЗЫВ в конце по правилам из STYLE.md
Подпись к посту: 2-3 предложения."""
    return await ask_claude(prompt, timeout=300)
