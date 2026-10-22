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
    "/api/v1/posts/for-you?order_by={order}&anomaly=false&limit=36"
    "&query={q}&search_type={stype}&personal=false"
)
# Площадки, которые сейчас ищет Trendsee: instagram (Reels) и tiktok. Shorts и VK Клипы у них «скоро».
PLATFORMS = {"instagram": "Reels", "tiktok": "TikTok", "youtube": "Shorts", "vk": "VK Клипы"}
SKIP_HEADERS = {"host", "content-length", "connection", "accept-encoding", "cookie",
                "origin", "referer", "user-agent"}


class TrendseeAuthError(Exception):
    pass


LOGIN_URL = APP_URL + "/auth/login"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")


async def _login(page) -> None:
    """Вход по почте и паролю на app.trendsee.io/auth/login. При неудаче — TrendseeAuthError с причиной."""
    login = os.environ.get("TRENDSEE_LOGIN", "").strip()
    password = os.environ.get("TRENDSEE_PASSWORD", "").strip()
    if not login or not password:
        raise TrendseeAuthError("не заданы TRENDSEE_LOGIN / TRENDSEE_PASSWORD")

    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    email = page.locator("input[name=email], input[type=email]").first
    try:
        await email.wait_for(state="visible", timeout=30000)
    except Exception:
        if "/auth/" not in page.url:
            return  # уже вошли
        await _dump(page, "login_noform")
        raise TrendseeAuthError("не нашёл форму входа на странице Trendsee")

    pwd = page.locator("input[name=password], input[type=password]").first
    await email.fill(login)
    await pwd.fill(password)
    btn = page.get_by_role("button", name="Дальше")
    if await btn.count():
        await btn.first.click()
    else:
        await pwd.press("Enter")
    # Trendsee входит медленно (30-40 секунд и дольше), ждём до 2 минут
    for sec in range(120):
        await page.wait_for_timeout(1000)
        if "/auth/" not in page.url:
            log.info("Вход в Trendsee: успешно за %d с", sec + 1)
            await page.wait_for_timeout(3000)
            return
        if sec == 60:
            # не дождались — нажимаем «Дальше» ещё раз
            try:
                if await btn.count() and await btn.first.is_enabled():
                    await btn.first.click()
            except Exception:
                pass
    hint = await _dump(page, "login_fail")
    raise TrendseeAuthError(f"сайт не пустил после ввода почты и пароля{': ' + hint if hint else ''}")


async def _dump(page, name: str) -> str:
    """Сохраняет скриншот и текст страницы в /data для разбора, возвращает подсказку с экрана."""
    hint = ""
    try:
        await page.screenshot(path=str(DATA_DIR / f"{name}.png"), full_page=True)
        text = await page.locator("body").inner_text(timeout=5000)
        (DATA_DIR / f"{name}.txt").write_text(f"{page.url}\n\n{text}", encoding="utf-8")
        skip = ("Вход", "Почта*", "Пароль*", "Забыли пароль?", "Дальше", "Или", "Войти с Telegram",
                "Войти с Яндекс ID", "Нет аккаунта?", "Регистрация")
        lines = [l.strip() for l in text.splitlines() if l.strip() and l.strip() not in skip
                 and "cookie" not in l.lower() and "конфиденциальност" not in l.lower()
                 and "соглашени" not in l.lower() and "Сервиса" not in l]
        keys = ("невер", "ошиб", "не найден", "не существ", "подтвер", "код", "капч", "робот",
                "заблок", "попыт", "тариф", "error", "invalid", "wrong")
        important = [l for l in lines if any(k in l.lower() for k in keys)]
        # сообщения об ошибке в элементах формы/уведомлениях
        try:
            alerts = await page.locator("[role=alert], .error, [class*=error], [class*=toast], "
                                        "[class*=notif], [class*=invalid]").all_inner_texts()
            important = [a.strip() for a in alerts if a.strip()] + important
        except Exception:
            pass
        hint = "; ".join(dict.fromkeys(important or lines[-3:]))[:250]
        log.warning("Страница входа (%s): %s", page.url, hint or "без сообщений")
    except Exception as ex:
        log.warning("Не смог сохранить страницу: %s", ex)
    return hint


async def _capture_auth(page, timeout_s: int = 45) -> dict | None:
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
            const t = setTimeout(() => ctrl.abort(), 45000);
            try {
              const r = await fetch(url, {headers, credentials: 'include', signal: ctrl.signal});
              const txt = await r.text();
              let body; try { body = JSON.parse(txt) } catch(e) { body = txt.slice(0, 2000) }
              return [r.status, body];
            } catch (e) { return [0, String(e)] } finally { clearTimeout(t) }
        }""",
        [url, headers],
    )


def _job(j) -> dict:
    """Задание поиска: query, order (viral_coef|taken_at), social (None|instagram|tiktok),
    max_age_days (для листания свежих), pages."""
    if isinstance(j, str):
        j = {"query": j}
    return {"query": j["query"], "order": j.get("order", "viral_coef"), "social": j.get("social"),
            "stype": j.get("stype", "description"),
            "max_age_days": j.get("max_age_days"), "pages": j.get("pages", 1)}


async def search(jobs: list) -> list[dict]:
    """Выполняет задания поиска, возвращает нормализованные ролики (с полем query)."""
    jobs = [_job(j) for j in jobs]
    BROWSER_DIR.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            str(BROWSER_DIR),
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
            locale="ru-RU",
            user_agent=UA,
            viewport={"width": 1366, "height": 900},
        )
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            page.set_default_timeout(30000)

            auth = await _capture_auth(page)
            if not auth:
                log.info("Нет авторизованных запросов — вхожу")
                await _login(page)
                auth = await _capture_auth(page)
                if not auth:
                    raise TrendseeAuthError("после входа нет запросов с authorization")

            parts = urlsplit(auth["url"])
            base = f"{parts.scheme}://{parts.netloc}"
            state = {"headers": auth["headers"], "relogged": False}
            sample_saved = (DATA_DIR / "sample_response.json").exists()

            async def fetch(url):
                await asyncio.sleep(1.2)  # у Trendsee лимит ~1 запрос в секунду
                status, body = await _api_get(page, url, state["headers"])
                if status == 429:
                    await asyncio.sleep(3)
                    status, body = await _api_get(page, url, state["headers"])
                if status == 401 and not state["relogged"]:
                    state["relogged"] = True
                    log.info("401 — вхожу заново")
                    await _login(page)
                    a = await _capture_auth(page)
                    if not a:
                        raise TrendseeAuthError("вход слетел и не восстановился")
                    state["headers"] = a["headers"]
                    status, body = await _api_get(page, url, state["headers"])
                if status == 401:
                    raise TrendseeAuthError("Trendsee отвечает 401")
                return status, body

            for job in jobs:
                url = base + SEARCH_PATH.format(q=quote(job["query"]), order=job["order"], stype=job["stype"])
                if job["social"]:
                    url += f"&social_type={job['social']}"
                cursor, got = None, 0
                for page_no in range(max(1, job["pages"])):
                    try:
                        status, body = await fetch(url + (f"&cursor={quote(str(cursor))}" if cursor else ""))
                    except TrendseeAuthError:
                        raise
                    except Exception as e:
                        log.warning("Ошибка запроса «%s»: %s", job["query"], e)
                        break
                    if status != 200:
                        log.warning("«%s»: статус %s %s", job["query"], status, str(body)[:300])
                        break
                    if not sample_saved:
                        (DATA_DIR / "sample_response.json").write_text(
                            json.dumps(body, ensure_ascii=False, indent=1)[:200000], encoding="utf-8")
                        sample_saved = True
                    items = parse_posts(body)
                    for it in items:
                        it["query"] = job["query"]
                    results.extend(items)
                    got += len(items)
                    cursor = body.get("next_cursor") if isinstance(body, dict) else None
                    if not cursor or not items:
                        break
                    if job["order"] == "taken_at" and job["max_age_days"]:
                        oldest = min((it["date"] for it in items if it.get("date")), default=None)
                        limit = datetime.now(timezone.utc).timestamp() - job["max_age_days"] * 86400
                        if oldest and datetime.fromisoformat(oldest).timestamp() < limit:
                            break
                    await asyncio.sleep(0.5)
                log.info("«%s» [%s, %s%s]: %d роликов", job["query"], job["stype"], job["order"],
                         f", {job['social']}" if job["social"] else "", got)
                await asyncio.sleep(0.5)
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


POST_MARKERS = ("/reel/", "/reels/", "/p/", "/tv/", "/video/", "/shorts/", "watch?v=", "youtu.be/", "/clip")


def _link(flat):
    """Ссылка именно на ролик, а не на профиль автора."""
    urls = []
    for k, v in flat.items():
        if isinstance(v, str):
            for m in LINK_RE.finditer(v):
                urls.append((k, m.group(0)))
    for _, u in urls:
        if any(mk in u for mk in POST_MARKERS):
            return u
    # короткий код поста → ссылка на рилс
    for k, v in flat.items():
        last = k.split(".")[-1]
        if last in ("shortcode", "short_code", "code", "post_code", "shortcode_id") and isinstance(v, str) \
                and re.fullmatch(r"[\w-]{6,20}", v):
            return f"https://www.instagram.com/reel/{v}/"
    for k, v in flat.items():
        last = k.split(".")[-1]
        if last in ("video_id", "aweme_id") and v and "tiktok" in " ".join(u for _, u in urls):
            author = next((u for _, u in urls if "tiktok" in u), "").rstrip("/")
            return f"{author}/video/{v}"
    return urls[0][1] if urls else ""


def _post_id(flat) -> str:
    for k, v in flat.items():
        last = k.split(".")[-1]
        if last in ("id", "post_id", "pk", "media_id", "external_id", "uuid") and v not in (None, ""):
            return str(v)
    return ""


_keys_logged = False


def _from_trendsee(raw: dict) -> dict | None:
    """Формат Trendsee: social_url — ссылка на ролик, blogger_social_url — на автора."""
    link = raw.get("social_url")
    if not link:
        return None
    author_url = raw.get("blogger_social_url") or ""
    author = author_url.rstrip("/").rsplit("/", 1)[-1].lstrip("@") if author_url else ""
    taken = raw.get("taken_at")
    num = lambda k: float(raw[k]) if isinstance(raw.get(k), (int, float)) else None
    return {
        "link": link if "watch?v=" in link else link.split("?")[0],
        "platform": raw.get("social_type") or "",
        "author": author,
        "views": num("play_count"),
        "likes": num("like_count"),
        "comments": num("comment_count"),
        "viral": num("viral_coef"),
        "subscribers": num("subscribers_count"),
        "date": taken.replace("Z", "+00:00") if isinstance(taken, str) else None,
        "caption": (raw.get("caption") or "")[:700],
        "speech": _text(raw.get("transcription"))[:1500] or _text(raw.get("subtitles"))[:1500],
    }


def _text(v) -> str:
    if not v:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return " ".join(_text(x) for x in v.values())
    if isinstance(v, list):
        return " ".join(_text(x) for x in v)
    return ""


def parse_posts(body) -> list[dict]:
    if isinstance(body, dict) and isinstance(body.get("items"), list):
        exact = [x for x in (_from_trendsee(r) for r in body["items"] if isinstance(r, dict)) if x]
        if exact:
            return exact
    lst = _find_list(body) or []
    out = []
    global _keys_logged
    for raw in lst:
        flat = _flatten(raw)
        if not _keys_logged:
            _keys_logged = True
            log.info("Поля ролика в ответе Trendsee: %s", ", ".join(list(flat)[:60]))
        link = _link(flat)
        if not link:
            continue
        link = link if "watch?v=" in link else link.split("?")[0]
        pid = _post_id(flat)
        if not any(mk in link for mk in POST_MARKERS) and pid:
            link = f"{link.rstrip('/')}#post-{pid}"  # профиль: делаем ссылку уникальной для каждого ролика
        out.append({
            "link": link,
            "author": _str(flat, "username", "author", "owner", "nickname", "login"),
            "views": _num(flat, "view", "play"),
            "likes": _num(flat, "like", exclude=("dislike",)),
            "comments": _num(flat, "comment"),
            "viral": _num(flat, "viral", "anomal", "coef", "ratio"),
            "date": (_date(flat) or None) and _date(flat).isoformat(),
            "caption": (_str(flat, "caption", "description", "text", "title") or "")[:700],
        })
    return out
