"""Cron: cek plastik yang sudah > BUFFER_AGING_HOURS jam di buffer.

Run via:
    python -m packing_router.cron.aging_check

Output: print ke stdout + log event 'alert' untuk tiap aging item.
Channel notifikasi real (email/Slack/dll) ditentukan kemudian.
"""
import json
import sys
from datetime import datetime

from .. import config
from ..db import get_connection, log_event
from ..reports import get_buffer_aging_report


def main(argv=None):
    items = get_buffer_aging_report()
    if not items:
        print(f"[{datetime.now()}] Aging check OK — tidak ada plastik > {config.BUFFER_AGING_HOURS}h.")
        return 0
    print(f"[{datetime.now()}] AGING ALERT — {len(items)} buffer slot:")
    conn = get_connection()
    for it in items:
        print(
            f"  Wadah {it.wadah_nomor} Slot {it.slot_number} | SKU {it.sku} "
            f"| {it.plastik_count} plastik | umur {it.age_hours}h "
            f"| first_at {it.first_plastik_at}"
        )
        log_event(
            "alert",
            "system",
            "buffer_slot",
            it.buffer_slot_id,
            {
                "kind": "aging",
                "sku": it.sku,
                "age_hours": it.age_hours,
                "plastik_count": it.plastik_count,
                "wadah_nomor": it.wadah_nomor,
                "slot_number": it.slot_number,
            },
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
