"""Pytest fixtures untuk packing_router. Pakai temp DB per-test (no shared state)."""
import os
import sys
import tempfile

import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(THIS_DIR, "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from packing_router import config as pr_config  # noqa: E402
from packing_router import db as pr_db  # noqa: E402
from packing_router.buffer import add_wadah  # noqa: E402
from packing_router.slot_aktif import init_default_slot_aktif  # noqa: E402


@pytest.fixture
def tmp_db(monkeypatch):
    """Pakai DB sementara per-test. Reset connection biar fresh tiap test."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="pr_test_")
    os.close(fd)
    try:
        os.remove(path)
    except OSError:
        pass
    monkeypatch.setattr(pr_config, "DB_PATH", path)
    pr_db.reset_connection()
    pr_db.get_connection(path)
    yield path
    pr_db.reset_connection()
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(path + ext)
        except OSError:
            pass


@pytest.fixture
def buffer_seeded(tmp_db, monkeypatch):
    """DB + 2 wadah default (10 slot each) + 30 slot aktif."""
    monkeypatch.setattr(pr_config, "DEFAULT_WADAH_COUNT", 2)
    monkeypatch.setattr(pr_config, "SLOTS_PER_WADAH", 10)
    monkeypatch.setattr(pr_config, "DEFAULT_SLOT_AKTIF_COUNT", 30)
    pr_db.init_default_wadah()
    init_default_slot_aktif()
    return tmp_db


@pytest.fixture
def small_buffer(tmp_db, monkeypatch):
    """1 wadah, 3 slot saja (untuk test buffer-full skenario) + 30 slot aktif."""
    monkeypatch.setattr(pr_config, "DEFAULT_WADAH_COUNT", 1)
    monkeypatch.setattr(pr_config, "SLOTS_PER_WADAH", 3)
    monkeypatch.setattr(pr_config, "DEFAULT_SLOT_AKTIF_COUNT", 30)
    pr_db.init_default_wadah()
    init_default_slot_aktif()
    return tmp_db


@pytest.fixture
def tiny_slot_aktif(tmp_db, monkeypatch):
    """2 wadah default + hanya 2 slot aktif (untuk test slot-full skenario)."""
    monkeypatch.setattr(pr_config, "DEFAULT_WADAH_COUNT", 2)
    monkeypatch.setattr(pr_config, "SLOTS_PER_WADAH", 10)
    monkeypatch.setattr(pr_config, "DEFAULT_SLOT_AKTIF_COUNT", 2)
    pr_db.init_default_wadah()
    init_default_slot_aktif()
    return tmp_db
