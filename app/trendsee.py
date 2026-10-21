"""Доступ к Trendsee: браузер Playwright + запросы к API сайта изнутри страницы.

Открытого API у Trendsee нет. Поэтому открываем app.trendsee.io в headless Chromium
с постоянным профилем (вход сохраняется), ловим первый запрос страницы к /api/v1/
с заголовком authorization и с этими заголовками делаем fetch прямо из страницы.
"""
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

from playwright.async_api import async_playwright

from .storage import BROWSER_DIR, DATA_DIR

log = logging.getLogger("trendsee")

APP_URL = "https://app.trendsee.io"
SEARCH_PATH = (
    "/api/v1/posts/for-you?order_by=viral_coef&anomaly=false&limit=36"
    "&query={q}&search_type=all&personal=true"
)
SKIP_HEADERS = {"host", "content-length", "connection", "accept-encoding", "cookie",
                "origin", "referer", "user-agent"}


class TrendseeAuthError(Exception):
    pass


async def _login(page) -> bool:
    """Ищем форму входа и входим логином/паролем из переменных окружения."""
    login = os.environ.get("TRENDSEE_LOGIN", "")
    password = os.environ.get("TRENDSEE_PASSWORD", "")
    if not login or not password:
        log.error("TRENDSEE_LOGIN / TRENDSEE_PASSWORD не заданы")
        return False

    pwd = page.locator("input[type=password]")
    if await pwd.count() == 0:
        # пробуем открыть страницу входа
        for path in ("/login", "/auth/login", "/sign-in", "/signin"):
            try:
                await page.goto(APP_URL + path, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2500)
            except Exception:
                continue
            if await page.locator("input[type=password]").count():
                break
        pwd = page.locator("input[type=password]")
        if await pwd.count() == 0:
            # иногда форма прячется за кнопкой «Войти»
            for text in ("Войти", "Вход", "Log in", "Login", "Sign in"):
                btn = page.get_by_text(text, exact=False).first
                try:
                    if await btn.count():
                        await btn.click(timeout=3000)
                        await page.wait_for_timeout(2000)
                        if await page.locator("input[type=password]").count():
                            break
                except Exception:
                    pass
            pwd = page.locator("input[type=password]")
            if await pwd.count() == 0:
                log.error("Не нашёл форму входа на Trendsee")
                return False

    user_input = page.locator(
        "input[type=email], input[name*=mail i], input[name*=login i], "
        "input[name*=user i], input[autocomplete=username], input[type=text]"
    ).first
    await user_input.fill(login, timeout=10000)
    await pwd.first.fill(password, timeout=10000)
    await pwd.first.press("Enter")
    await page.wait_for_timeout(6000)
    ok = await page.locator("input[type=password]").count() == 0
    log.info("Вход в Trendsee: %s", "успешно" if ok else "не удалось")
    return ok


async def _capture_auth(page, timeout_s: int = 25) -> dict | None:
    captured: dict = {}

    def on_request(req):
        if captured or "/api/v1/" not in req.url:
            return
        headers = req.headers
        if "authorization" in {k.lower() for k in headers}:
            captured["url"] = req.url
            captured["headers"] = {
                k: v for k, v in headers.items()
                if k.lower() not in SKIP_HEADERS and not k.lower().startswith("sec-")
                and not k.startswith(":")
            }

    page.on("request", on_request)
    try:
        await page.goto(APP_URL, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log.warning("goto: %s", e)
    for _ in range(timeout_s * 2):
        if captured:
            break
        await asyncio.sleep(0.5)
    page.remove_listener("request", on_request)
    return captured or None


async def _api_get(page, url: str, headers: dict) -> tuple[int, object]:
    return await page.evaluate(
        """async ([url, headers]) => {
            const ctrl = new AbortController();
            const t = setTimeout(() => ctrl.abort(), 30000);
            try {
              const r = await fetch(url, {headers, credentials: 'include', signal: ctrl.signal});
              const txt = await r.text();
              let body; try { body = JSON.parse(txt) } catch(e) { body = txt.slice(0, 2000) }
              return [r.status, body];
            } catch (e) { return [0, String(e)] } finally { clearTimeout(t) }
        }""",
        [url, headers],
    )


async def search(phrases: list[str]) -> list[dict]:
    """Возвращает нормализованные ролики по всем фразам (с полем query)."""
    BROWSER_DIR.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            str(BROWSER_DIR),
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
            locale="ru-RU",
            viewport={"width": 1366, "height": 900},
        )
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            page.set_default_timeout(30000)

            auth = await _capture_auth(page)
            if not auth:
                log.info("Нет авторизованных запросов — пробую войти")
                if not await _login(page):
                    raise TrendseeAuthError("не удалось войти в Trendsee")
                auth = await _capture_auth(page)
                if not auth:
                    raise TrendseeAuthError("после входа нет запросов с authorization")

            parts = urlsplit(auth["url"])
            base = f"{parts.scheme}://{parts.netloc}"
            headers = auth["headers"]
            relogged = False
            sample_saved = (DATA_DIR / "sample_response.json").exists()

            for phrase in phrases:
                url = base + SEARCH_PATH.format(q=quote(phrase))
                try:
                    status, body = await _api_get(page, url, headers)
                    if status == 401 and not relogged:
                        relogged = True
                        log.info("401 — вхожу заново")
                        await _login(page)
                        auth = await _capture_auth(page)
                        if not auth:
                            raise TrendseeAuthError("вход слетел и не восстановился")
                        headers = auth["headers"]
                        status, body = await _api_get(page, url, headers)
                    if status == 401:
                        raise TrendseeAuthError("Trendsee отвечает 401")
                    if status != 200:
                        log.warning("«%s»: статус %s %s", phrase, status, str(body)[:300])
                        continue
                    if not sample_saved:
                        (DATA_DIR / "sample_response.json").write_text(
                            json.dumps(body, ensure_ascii=False, indent=1)[:200000], encoding="utf-8")
                        sample_saved = True
                    items = parse_posts(body)
                    log.info("«%s»: %d роликов", phrase, len(items))
                    if not items:
                        log.warning("Непонятный формат ответа: %s", json.dumps(body, ensure_ascii=False)[:800])
                    for it in items:
                        it["query"] = phrase
                    results.extend(items)
                except TrendseeAuthError:
                    raise
                except Exception as e:
                    log.warning("Ошибка запроса «%s»: %s", phrase, e)
                await asyncio.sleep(1.0)
        finally:
            await ctx.close()
    return results


# ---------------- разбор ответа ----------------
LINK_RE = re.compile(r"https?://(www\.)?(instagram\.com|tiktok\.com|youtube\.com|youtu\.be|vk\.com|vkvideo\.ru)/\S+", re.I)


def _flatten(d, prefix="", depth=0, out=None):
    out = {} if out is None else out
    if depth > 3:
        return out
    if isinstance(d, dict):
        for k, v in d.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)) and not (isinstance(v, list) and v and not isinstance(v[0], (dict, list))):
                if isinstance(v, dict):
                    _flatten(v, key, depth + 1, out)
            else:
                out[key.lower()] = v
    return out


def _find_list(obj, depth=0):
    """Ищем самый «похожий на ролики» список словарей."""
    best = None
    if depth > 4:
        return None
    if isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj[:5]):
        best = obj
    if isinstance(obj, dict):
        for v in obj.values():
            cand = _find_list(v, depth + 1)
            if cand and (best is None or len(cand) > len(best)):
                best = cand
    return best


def _num(flat, *needles, exclude=()):
    for k, v in flat.items():
        last = k.split(".")[-1]
        if any(n in last for n in needles) and not any(e in last for e in exclude):
            try:
                if v is None or isinstance(v, bool):
                    continue
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _str(flat, *needles):
    for k, v in flat.items():
        last = k.split(".")[-1]
        if any(n == last or n in last for n in needles) and isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _date(flat):
    for k, v in flat.items():
        last = k.split(".")[-1]
        if any(n in last for n in ("taken_at", "published", "posted", "created", "date", "timestamp")):
            try:
                if isinstance(v, (int, float)) and v > 1e9:
                    return datetime.fromtimestamp(v / 1000 if v > 1e12 else v, tz=timezone.utc)
                if isinstance(v, str) and len(v) >= 10:
                    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
                    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
    return None


def _link(flat):
    for k in ("url", "link", "post_url", "permalink", "source_url", "original_url", "video_url"):
        for fk, v in flat.items():
            if fk.split(".")[-1] == k and isinstance(v, str) and LINK_RE.match(v):
                return v
    for v in flat.values():
        if isinstance(v, str):
            m = LINK_RE.search(v)
            if m:
                return m.group(0)
    code = _str(flat, "shortcode", "code")
    if code and re.fullmatch(r"[\w-]{6,20}", code):
        return f"https://www.instagram.com/reel/{code}/"
    return ""


def parse_posts(body) -> list[dict]:
    lst = _find_list(body) or []
    out = []
    for raw in lst:
        flat = _flatten(raw)
        link = _link(flat)
        if not link:
            continue
        out.append({
            "link": link.split("?")[0],
            "author": _str(flat, "username", "author", "owner", "nickname", "login"),
            "views": _num(flat, "view", "play"),
            "likes": _num(flat, "like", exclude=("dislike",)),
            "comments": _num(flat, "comment"),
            "viral": _num(flat, "viral", "anomal", "coef", "ratio"),
            "date": (_date(flat) or None) and _date(flat).isoformat(),
            "caption": (_str(flat, "caption", "description", "text", "title") or "")[:700],
        })
    return out
