"""Простое хранение состояния в JSON-файлах на Volume /data."""
import json
import os
import threading
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
BROWSER_DIR = DATA_DIR / "browser"
ROOT = Path(__file__).resolve().parent.parent

_lock = threading.Lock()


def _path(name: str) -> Path:
    return DATA_DIR / name


def load(name: str, default):
    p = _path(name)
    with _lock:
        if not p.exists():
            return default
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default


def save(name: str, data) -> None:
    p = _path(name)
    tmp = p.with_suffix(".tmp")
    with _lock:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)


# ---------- ключевые слова ----------
def get_keywords() -> dict:
    kw = load("keywords.json", None)
    if not kw:
        kw = json.loads((ROOT / "keywords.default.json").read_text(encoding="utf-8"))
        save("keywords.json", kw)
    return kw


def set_keywords(group: str, words: list[str]) -> dict:
    kw = get_keywords()
    kw[group] = words
    save("keywords.json", kw)
    return kw


# ---------- состояние бота ----------
DEFAULT_STATE = {
    "owner": None,
    "chats": [],
    "times": ["09:00", "19:00"],
    "last_run": None,
    "last_ok": None,
    "last_error": None,
}


def get_state() -> dict:
    st = load("state.json", {})
    out = dict(DEFAULT_STATE)
    out.update(st)
    return out


def update_state(**kw) -> dict:
    st = get_state()
    st.update(kw)
    save("state.json", st)
    return st


# ---------- уже отправленные ролики ----------
def get_seen() -> set[str]:
    return set(load("seen.json", []))


def add_seen(links: list[str]) -> None:
    seen = load("seen.json", [])
    seen.extend(l for l in links if l not in seen)
    save("seen.json", seen[-5000:])


# ---------- подборки (для кнопок «Ещё 5» и «Сценарий») ----------
def save_batch(batch_id: str, data: dict) -> None:
    batches = load("batches.json", {})
    batches[batch_id] = data
    if len(batches) > 60:
        for k in list(batches)[: len(batches) - 60]:
            batches.pop(k, None)
    save("batches.json", batches)


def get_batch(batch_id: str) -> dict | None:
    return load("batches.json", {}).get(batch_id)
