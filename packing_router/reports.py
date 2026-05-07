"""Read-only views: slot aktif status, harvester queue, buffer aging."""
import datetime as _dt
from typing import List

from . import config
from .db import get_connection
from .models import AgingItem, HarvesterTaskRow, SlotStatus


def _parse_iso(s):
    if not s:
        return None
    try:
        return _dt.datetime.fromisoformat(s.replace(" ", "T"))
    except (TypeError, ValueError):
        return None


def get_slot_aktif_status() -> List[SlotStatus]:
    """Return list slot dengan state. Slot tanpa resi → status 'kosong'.

    Enumerate dari tabel ``slot_aktif`` (dynamic count, default 30).
    """
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT r.id AS resi_id, r.nomor_resi, r.slot_aktif_number, r.status,
               r.completed_at
        FROM resi r
        WHERE r.status IN ('active', 'complete')
          AND r.slot_aktif_number IS NOT NULL
        """
    ).fetchall()
    by_slot = {r["slot_aktif_number"]: r for r in rows}

    slot_nums = [
        r["nomor"]
        for r in conn.execute(
            "SELECT nomor FROM slot_aktif WHERE is_active = 1 ORDER BY nomor ASC"
        ).fetchall()
    ]
    now = _dt.datetime.now()
    result: List[SlotStatus] = []
    for slot_no in slot_nums:
        r = by_slot.get(slot_no)
        if r is None:
            result.append(SlotStatus(slot_number=slot_no, resi_id=None, nomor_resi=None, status="kosong"))
            continue
        missing = conn.execute(
            """
            SELECT sku, varian, quantity_ordered, quantity_fulfilled
            FROM resi_item WHERE resi_id = ?
              AND quantity_fulfilled < quantity_ordered
            ORDER BY id ASC
            """,
            (r["resi_id"],),
        ).fetchall()
        missing_list = [
            {
                "sku": m["sku"],
                "varian": m["varian"],
                "ordered": m["quantity_ordered"],
                "fulfilled": m["quantity_fulfilled"],
                "kurang": m["quantity_ordered"] - m["quantity_fulfilled"],
            }
            for m in missing
        ]
        if r["status"] == "active":
            status = "merah"
            minutes_waiting = None
        else:  # complete
            completed = _parse_iso(r["completed_at"])
            if completed is not None:
                minutes_waiting = int((now - completed).total_seconds() // 60)
                status = "kuning" if minutes_waiting >= config.SLOT_KUNING_TIMEOUT_MIN else "hijau"
            else:
                minutes_waiting = None
                status = "hijau"
        result.append(
            SlotStatus(
                slot_number=slot_no,
                resi_id=r["resi_id"],
                nomor_resi=r["nomor_resi"],
                status=status,
                missing_skus=missing_list,
                completed_at=r["completed_at"],
                minutes_waiting=minutes_waiting,
            )
        )
    return result


def get_harvester_queue() -> List[HarvesterTaskRow]:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT ht.id, ht.buffer_slot_id, ht.target_resi_id, ht.sku, ht.status,
               ht.created_at, ht.started_at,
               w.nomor AS wadah_nomor, bs.slot_number,
               r.slot_aktif_number, r.nomor_resi
        FROM harvester_task ht
        JOIN buffer_slot bs ON bs.id = ht.buffer_slot_id
        JOIN wadah w ON w.id = bs.wadah_id
        JOIN resi r ON r.id = ht.target_resi_id
        WHERE ht.status IN ('pending', 'in_progress')
        ORDER BY ht.status DESC, ht.created_at ASC, ht.id ASC
        """
    ).fetchall()
    return [
        HarvesterTaskRow(
            id=r["id"],
            buffer_slot_id=r["buffer_slot_id"],
            buffer_label=f"WADAH {r['wadah_nomor']} SLOT {r['slot_number']}",
            target_resi_id=r["target_resi_id"],
            target_resi_nomor=r["nomor_resi"],
            target_slot_aktif_number=r["slot_aktif_number"],
            sku=r["sku"],
            status=r["status"],
            created_at=r["created_at"],
            started_at=r["started_at"],
        )
        for r in rows
    ]


def get_buffer_match_status() -> dict:
    """Untuk dashboard: list buffer slot + flag apakah SKU-nya match dengan resi
    aktif yang masih butuh.

    Return ``{wadahs: [{nomor, slots: [{slot_number, sku, plastik_count, has_match,
    match_resi_slots: [list slot_aktif_number resi yang butuh sku ini]}]}],
    matched_skus: set of sku}``.
    """
    conn = get_connection()
    wadah_rows = conn.execute(
        """
        SELECT id, nomor, capacity FROM wadah WHERE is_active = 1 ORDER BY nomor ASC
        """
    ).fetchall()
    needed_rows = conn.execute(
        """
        SELECT ri.sku, r.slot_aktif_number
        FROM resi_item ri
        JOIN resi r ON r.id = ri.resi_id
        WHERE r.status = 'active'
          AND r.slot_aktif_number IS NOT NULL
          AND ri.quantity_fulfilled < ri.quantity_ordered
        """
    ).fetchall()
    needed_map: dict = {}
    for nr in needed_rows:
        needed_map.setdefault(nr["sku"], []).append(nr["slot_aktif_number"])

    wadahs = []
    matched_skus = set()
    for w in wadah_rows:
        slots = conn.execute(
            """
            SELECT id, slot_number, sku, plastik_count, is_overflow_of
            FROM buffer_slot WHERE wadah_id = ?
            ORDER BY slot_number ASC
            """,
            (w["id"],),
        ).fetchall()
        slot_list = []
        for s in slots:
            sku = s["sku"]
            has_match = False
            match_slots: list = []
            if sku and (s["plastik_count"] or 0) > 0 and sku in needed_map:
                has_match = True
                matched_skus.add(sku)
                match_slots = sorted(set(needed_map[sku]))
            slot_list.append(
                {
                    "buffer_slot_id": s["id"],
                    "slot_number": s["slot_number"],
                    "sku": sku,
                    "plastik_count": s["plastik_count"] or 0,
                    "is_overflow": s["is_overflow_of"] is not None,
                    "has_match": has_match,
                    "match_resi_slots": match_slots,
                }
            )
        wadahs.append({"wadah_id": w["id"], "nomor": w["nomor"], "slots": slot_list})

    return {"wadahs": wadahs, "matched_skus": matched_skus}


def get_slot_aktif_match_status() -> List[dict]:
    """Untuk dashboard: list slot aktif + SKU yang dibutuhkan + flag match dengan buffer."""
    conn = get_connection()
    slot_nums = [
        r["nomor"]
        for r in conn.execute(
            "SELECT nomor FROM slot_aktif WHERE is_active = 1 ORDER BY nomor ASC"
        ).fetchall()
    ]
    resi_rows = conn.execute(
        """
        SELECT r.id, r.nomor_resi, r.slot_aktif_number, r.status, r.completed_at
        FROM resi r
        WHERE r.status IN ('active', 'complete')
          AND r.slot_aktif_number IS NOT NULL
        """
    ).fetchall()
    by_slot = {r["slot_aktif_number"]: r for r in resi_rows}

    buffer_skus = {
        r["sku"]
        for r in conn.execute(
            "SELECT DISTINCT sku FROM buffer_slot "
            "WHERE sku IS NOT NULL AND plastik_count > 0"
        ).fetchall()
    }

    now = _dt.datetime.now()
    out = []
    for n in slot_nums:
        r = by_slot.get(n)
        if r is None:
            out.append(
                {
                    "slot_number": n,
                    "resi_id": None,
                    "nomor_resi": None,
                    "status": "kosong",
                    "missing": [],
                    "match_skus": [],
                    "minutes_waiting": None,
                }
            )
            continue
        missing = conn.execute(
            """
            SELECT sku, varian, quantity_ordered, quantity_fulfilled
            FROM resi_item WHERE resi_id = ?
              AND quantity_fulfilled < quantity_ordered
            ORDER BY id ASC
            """,
            (r["id"],),
        ).fetchall()
        missing_list = [
            {
                "sku": m["sku"],
                "varian": m["varian"],
                "ordered": m["quantity_ordered"],
                "fulfilled": m["quantity_fulfilled"],
                "kurang": m["quantity_ordered"] - m["quantity_fulfilled"],
                "ordered_pcs": (m["quantity_ordered"] or 0) * 10,
                "fulfilled_pcs": (m["quantity_fulfilled"] or 0) * 10,
                "kurang_pcs": ((m["quantity_ordered"] or 0) - (m["quantity_fulfilled"] or 0)) * 10,
                "in_buffer": m["sku"] in buffer_skus,
                "untouched": (m["quantity_fulfilled"] or 0) == 0,
            }
            for m in missing
        ]
        match_skus = [m["sku"] for m in missing_list if m["in_buffer"]]
        if r["status"] == "active":
            untouched_count = sum(1 for m in missing_list if m["untouched"])
            if untouched_count > 0:
                status = "merah"  # ada SKU belum di-scan sama sekali
            else:
                status = "kuning"  # semua SKU sudah tersentuh, qty kurang
            minutes_waiting = None
        else:  # complete
            completed = _parse_iso(r["completed_at"])
            if completed is not None:
                minutes_waiting = int((now - completed).total_seconds() // 60)
                # biru = complete + overdue (sebelumnya 'kuning')
                status = "biru" if minutes_waiting >= config.SLOT_KUNING_TIMEOUT_MIN else "hijau"
            else:
                minutes_waiting = None
                status = "hijau"
        out.append(
            {
                "slot_number": n,
                "resi_id": r["id"],
                "nomor_resi": r["nomor_resi"],
                "status": status,
                "missing": missing_list,
                "match_skus": match_skus,
                "minutes_waiting": minutes_waiting,
                "completed_at": r["completed_at"],
            }
        )
    return out


def get_buffer_aging_report() -> List[AgingItem]:
    conn = get_connection()
    threshold_hours = config.BUFFER_AGING_HOURS
    rows = conn.execute(
        """
        SELECT bs.id, w.nomor AS wadah_nomor, bs.slot_number, bs.sku, bs.plastik_count,
               bs.first_plastik_at
        FROM buffer_slot bs
        JOIN wadah w ON w.id = bs.wadah_id
        WHERE bs.first_plastik_at IS NOT NULL AND bs.plastik_count > 0
        ORDER BY bs.first_plastik_at ASC
        """
    ).fetchall()
    now = _dt.datetime.now()
    out: List[AgingItem] = []
    for r in rows:
        first = _parse_iso(r["first_plastik_at"])
        if first is None:
            continue
        age_hours = (now - first).total_seconds() / 3600.0
        if age_hours < threshold_hours:
            continue
        out.append(
            AgingItem(
                buffer_slot_id=r["id"],
                wadah_nomor=r["wadah_nomor"],
                slot_number=r["slot_number"],
                sku=r["sku"],
                plastik_count=r["plastik_count"],
                first_plastik_at=r["first_plastik_at"],
                age_hours=round(age_hours, 2),
            )
        )
    return out
