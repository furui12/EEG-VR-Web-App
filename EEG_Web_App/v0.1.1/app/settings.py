"""Small JSON settings store (data/settings.json): channel names per EEG stream, etc."""
import json
import threading

from app.database import DATA_DIR

SETTINGS_PATH = DATA_DIR / "settings.json"
_lock = threading.Lock()


def load():
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(data):
    with _lock:
        tmp = SETTINGS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(SETTINGS_PATH)


def get_montage(stream_name):
    return load().get("montages", {}).get(stream_name or "", None)


def set_montage(stream_name, labels, frontal, parietal):
    data = load()
    data.setdefault("montages", {})[stream_name or ""] = {
        "labels": labels, "frontal": frontal, "parietal": parietal,
    }
    save(data)
