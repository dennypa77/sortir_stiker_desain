"""Test cron/aging_check + reports yang sebelumnya 0/39% coverage."""
import datetime
import sqlite3

import pytest

from packing_router import config as pr_config
from packing_router.cron import aging_check
from packing_router.db import get_connection
from packing_router.reports import (
    get_buffer_aging_report,
    get_buffer_match_status,
    get_harvester_queue,
    get_slot_aktif_match_status,
    get_slot_aktif_status,
)
from packing_router.resi_setup import handle_setup_resi_aktif
from packing_router.scan_handler import handle_scan_plastik


def _seed_resi(nomor="RESI-R", sku="1446", varian=10, ordered=1, slot_number=1):
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO wave (bigseller_batch_id, wave_number, status) VALUES ('B', 1, 'active')"
    )
    cur = conn.execute(
        "INSERT INTO resi (wave_id, nomor_resi, status) VALUES (?, ?, 'pending')",
        (cur.lastrowid, nomor),
    )
    rid = cur.lastrowid
    conn.execute(
        "INSERT INTO resi_item (resi_id, sku, varian, quantity_ordered) VALUES (?, ?, ?, ?)",
        (rid, sku, varian, ordered),
    )
    if slot_number is not None:
        handle_setup_resi_aktif(rid, slot_number=slot_number)
    return rid


class TestAgingReport:
    def test_no_aging_when_buffer_empty(self, buffer_seeded):
        assert get_buffer_aging_report() == []

    def test_no_aging_when_below_threshold(self, buffer_seeded):
        handle_scan_plastik("1446", operator_id="op1")
        assert get_buffer_aging_report() == []

    def test_aging_returned_when_first_plastik_at_old(self, buffer_seeded, monkeypatch):
        handle_scan_plastik("1446", operator_id="op1")
        # Backdate first_plastik_at jadi 100 jam lalu
        conn = get_connection()
        old = (datetime.datetime.now() - datetime.timedelta(hours=100)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE buffer_slot SET first_plastik_at = ? WHERE sku = ?",
            (old, "1446"),
        )
        monkeypatch.setattr(pr_config, "BUFFER_AGING_HOURS", 48)
        rows = get_buffer_aging_report()
        assert len(rows) == 1
        assert rows[0].sku == "1446"
        assert rows[0].age_hours >= 48


class TestAgingCron:
    def test_cron_returns_0_when_clean(self, buffer_seeded, capsys):
        assert aging_check.main() == 0
        out = capsys.readouterr().out
        assert "OK" in out

    def test_cron_returns_1_when_aging(self, buffer_seeded, monkeypatch, capsys):
        handle_scan_plastik("1446", operator_id="op1")
        conn = get_connection()
        old = (datetime.datetime.now() - datetime.timedelta(hours=100)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE buffer_slot SET first_plastik_at = ? WHERE sku = ?",
            (old, "1446"),
        )
        monkeypatch.setattr(pr_config, "BUFFER_AGING_HOURS", 48)
        assert aging_check.main() == 1
        # Cek event_log punya 'alert'
        cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM event_log WHERE event_type = 'alert'"
        ).fetchone()["c"]
        assert cnt == 1


class TestSlotStatusReports:
    def test_kosong_slot_for_empty_db(self, buffer_seeded):
        slots = get_slot_aktif_status()
        assert len(slots) > 0
        assert all(s.status == "kosong" for s in slots)

    def test_active_resi_becomes_merah(self, buffer_seeded):
        _seed_resi(slot_number=1)
        slots = get_slot_aktif_status()
        first = next(s for s in slots if s.slot_number == 1)
        assert first.status == "merah"

    def test_complete_resi_becomes_hijau(self, buffer_seeded):
        _seed_resi(slot_number=2, ordered=1)
        handle_scan_plastik("1446", operator_id="op1")
        slots = get_slot_aktif_status()
        s = next(s for s in slots if s.slot_number == 2)
        assert s.status == "hijau"


class TestBufferMatchStatus:
    def test_empty_buffer(self, buffer_seeded):
        status = get_buffer_match_status()
        assert "wadahs" in status
        assert all(
            not slot["has_match"]
            for w in status["wadahs"]
            for slot in w["slots"]
        )

    def test_buffer_with_match(self, buffer_seeded):
        _seed_resi(sku="1446", slot_number=1)
        handle_scan_plastik("1446", operator_id="op1")
        # Hmm — scan match resi langsung → tidak masuk buffer. Test scan SKU yang
        # tidak match resi instead.
        handle_scan_plastik("9999", operator_id="op1")
        status = get_buffer_match_status()
        any_match = any(
            slot["has_match"] for w in status["wadahs"] for slot in w["slots"]
        )
        # 1446 sudah masuk slot aktif, 9999 di buffer tapi no resi need
        # Jadi has_match=False adalah expected.
        assert any_match is False

    def test_buffer_match_flag_true(self, buffer_seeded):
        # Sequence yang benar: scan ke buffer DULU, BARU setup resi
        handle_scan_plastik("1446", operator_id="op1")
        _seed_resi(sku="1446", slot_number=1, ordered=2)
        status = get_buffer_match_status()
        any_match = any(
            slot["has_match"] for w in status["wadahs"] for slot in w["slots"]
        )
        assert any_match is True
        assert "1446" in status["matched_skus"]


class TestSlotAktifMatchStatus:
    def test_includes_buffer_locations(self, buffer_seeded):
        handle_scan_plastik("1446", operator_id="op1")
        _seed_resi(sku="1446", slot_number=1, ordered=2)
        out = get_slot_aktif_match_status()
        slot1 = next(s for s in out if s["slot_number"] == 1)
        miss = slot1["missing"][0]
        assert miss["in_buffer"] is True
        assert miss["buffer_locations"], "buffer_locations harus diisi"
        assert miss["buffer_locations"][0]["wadah_nomor"] == 1


class TestHarvesterQueue:
    def test_empty_queue(self, buffer_seeded):
        assert get_harvester_queue() == []

    def test_queue_after_setup(self, buffer_seeded):
        handle_scan_plastik("1446", operator_id="op1")
        _seed_resi(sku="1446", slot_number=1, ordered=1)
        q = get_harvester_queue()
        assert len(q) >= 1
        assert q[0].sku == "1446"
