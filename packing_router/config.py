"""Konfigurasi packing_router. Override via env var PACKING_ROUTER_<KEY>."""
import os
from pathlib import Path


def _env(key: str, default, cast=str):
    val = os.environ.get(f"PACKING_ROUTER_{key}")
    if val is None:
        return default
    if cast is bool:
        return val.lower() in ("1", "true", "yes", "y", "on")
    return cast(val)


REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = _env("DB_PATH", str(REPO_ROOT / "hasil" / "packing_router.db"))
CONFIG_JSON_PATH = _env("CONFIG_JSON_PATH", str(REPO_ROOT / "config.json"))

DEFAULT_SLOT_AKTIF_COUNT = _env("DEFAULT_SLOT_AKTIF_COUNT", 10, int)
RESIS_PER_BATCH = _env("RESIS_PER_BATCH", 300, int)
DEFAULT_WADAH_COUNT = _env("DEFAULT_WADAH_COUNT", 5, int)
SLOTS_PER_WADAH = _env("SLOTS_PER_WADAH", 10, int)
# Backward-compat alias (deprecated): kode lama mungkin masih reference SLOTS_PER_WAVE
SLOTS_PER_WAVE = DEFAULT_SLOT_AKTIF_COUNT
BUFFER_AGING_HOURS = _env("BUFFER_AGING_HOURS", 24, int)
SLOT_KUNING_TIMEOUT_MIN = _env("SLOT_KUNING_TIMEOUT_MIN", 15, int)
ALLOW_BUFFER_OVERFLOW = _env("ALLOW_BUFFER_OVERFLOW", True, bool)
OVERFLOW_TRIGGER_COUNT = _env("OVERFLOW_TRIGGER_COUNT", 10, int)
UNDO_WINDOW_SECONDS = _env("UNDO_WINDOW_SECONDS", 30, int)
WAVE_NEXT_THRESHOLD_PCT = _env("WAVE_NEXT_THRESHOLD_PCT", 90, int)

SQLITE_BUSY_RETRY_COUNT = _env("SQLITE_BUSY_RETRY_COUNT", 3, int)
SQLITE_BUSY_RETRY_BASE_MS = _env("SQLITE_BUSY_RETRY_BASE_MS", 50, int)

LIST_PESANAN_SHEET_NAME = _env("LIST_PESANAN_SHEET", "LIST_PESANAN")
DATA_SALES_SHEET_NAME = _env("DATA_SALES_SHEET", "DATA_SALES")

WEB_HOST = _env("WEB_HOST", "127.0.0.1")
WEB_PORT = _env("WEB_PORT", 5000, int)
WEB_DEBUG = _env("WEB_DEBUG", False, bool)
