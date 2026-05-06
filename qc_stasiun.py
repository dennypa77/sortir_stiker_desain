"""
qc_stasiun.py
Stasiun QC Stiker untuk PT Heavy Object Group (HOG).

Quality gate antara packing dan shipping. Setiap polymailer di-scan barcode resi-nya,
lalu setiap pack di dalamnya di-scan untuk verifikasi match dengan isi resi sebelum
boleh di-seal.

Sumber data pesanan: Sheet `LIST_PESANAN` di Google Spreadsheet (di-populate oleh
Apps Script v7.0 saat tim gudang upload BigSeller export).

State QC + audit trail: SQLite lokal di hasil/qc_data.db.
"""

import os
import re
import json
import shutil
import sqlite3
import hashlib
import threading
import winsound
from datetime import datetime
from collections import defaultdict

import customtkinter as ctk
from tkinter import messagebox

# ============================================================
# KONSTANTA
# ============================================================
DB_FILE = os.path.join("hasil", "qc_data.db")
SHEET_NAME = "LIST_PESANAN"

STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_QC_APPROVED = "qc_approved"
STATUS_QC_REJECTED = "qc_rejected"

REJECT_REASONS = [
    "SKU salah",
    "SKU kurang",
    "SKU lebih",
    "Pack rusak",
    "Item non-stiker tidak ada",
    "Lainnya",
]

MARKETPLACE_PREFIXES = {
    "SPXID": "Shopee Express",
    "SPX": "Shopee Express",
    "SHPE": "Shopee",
    "SHP": "Shopee",
    "JNT": "J&T Express",
    "JT": "J&T Express",
    "JNE": "JNE",
    "TKP": "Tokopedia",
    "IDE": "ID Express",
    "SAP": "SAP Express",
}

# Color tokens — match palette existing app.py
COLOR_HIJAU = "#28a745"
COLOR_KUNING = "#ffc107"
COLOR_MERAH = "#dc3545"
COLOR_CYAN = "#17a2b8"
COLOR_INFO = "#adb5bd"
COLOR_DARK = "#1f2937"
COLOR_BORDER = "#3b82f6"
COLOR_GREY_BTN = "#6c757d"
COLOR_GREY_BTN_HOVER = "#5a6268"


# ============================================================
# PARSER
# ============================================================
def parse_sku(sku):
    """Reuse logic dari app.py extract_numeric_id_and_pcs.
    Return (numeric_id_or_None, pcs_per_paket).
    Contoh: '431-RETRO-10PCS' -> ('431', 10), '1446-20pcs\\n' -> ('1446', 20),
            'BANNER-A3' -> (None, 1), '' -> (None, 1).
    """
    if not sku:
        return None, 1
    s = str(sku).strip()
    if not s:
        return None, 1
    id_match = re.match(r"^\d+", s)
    numeric_id = id_match.group(0) if id_match else None
    pcs_match = re.search(r"(\d+)pcs", s, re.IGNORECASE)
    pcs = int(pcs_match.group(1)) if pcs_match else 1
    return numeric_id, pcs


def calculate_packs_needed(pcs_per_paket, jumlah_paket):
    """Total pcs / 10 (1 pack = 10 pcs). Min 1 pack walau di bawah 10."""
    total_pcs = pcs_per_paket * jumlah_paket
    return max(1, total_pcs // 10) if total_pcs >= 10 else (1 if total_pcs > 0 else 0)


def detect_marketplace(resi):
    r = (resi or "").strip().upper()
    if not r:
        return "Unknown"
    for prefix in sorted(MARKETPLACE_PREFIXES, key=len, reverse=True):
        if r.startswith(prefix):
            return MARKETPLACE_PREFIXES[prefix]
    return "Unknown"


def hash_pin(pin):
    return hashlib.sha256(str(pin).strip().encode("utf-8")).hexdigest()


# ============================================================
# DB LAYER
# ============================================================
def _ensure_db_dir():
    db_dir = os.path.dirname(DB_FILE)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)


def get_db():
    _ensure_db_dir()
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _maybe_migrate_legacy_schema():
    """Auto-migrate qc_sessions in-place kalau punya operator_id NOT NULL (skema lama).
    Pakai rebuild table via SQL (tidak hapus file) supaya robust di Windows.
    Tetap buat file backup di .bak_<timestamp> sebagai safety.
    """
    if not os.path.exists(DB_FILE):
        return
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cols = conn.execute("PRAGMA table_info(qc_sessions)").fetchall()
        # PRAGMA table_info: (cid, name, type, notnull, dflt_value, pk)
        needs_migrate = any(
            c[1] == "operator_id" and c[3] == 1 for c in cols
        )
        if not needs_migrate:
            return

        # Buat backup file dulu (safety net)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = f"{DB_FILE}.bak_{ts}"
        try:
            bak_conn = sqlite3.connect(backup)
            conn.backup(bak_conn)
            bak_conn.close()
        except Exception as e:
            print(f"[QC DB] Backup gagal ({e}), tetap lanjut migrasi.")

        # In-place rebuild qc_sessions tanpa NOT NULL di operator_id
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            """
            CREATE TABLE qc_sessions_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resi_code TEXT NOT NULL,
                operator_id INTEGER,
                batch_id TEXT,
                marketplace TEXT,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                status TEXT DEFAULT 'in_progress',
                reject_reason TEXT,
                reject_notes TEXT,
                FOREIGN KEY (operator_id) REFERENCES qc_operators(id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO qc_sessions_new
              SELECT id, resi_code, operator_id, batch_id, marketplace,
                     started_at, completed_at, status, reject_reason, reject_notes
              FROM qc_sessions
            """
        )
        conn.execute("DROP TABLE qc_sessions")
        conn.execute("ALTER TABLE qc_sessions_new RENAME TO qc_sessions")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_qc_sessions_resi ON qc_sessions(resi_code)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_qc_sessions_status ON qc_sessions(status)"
        )
        conn.execute("PRAGMA foreign_keys = ON")
        conn.commit()
        print(
            f"[QC DB] Schema lama (operator_id NOT NULL) ter-migrate.\n"
            f"        Backup: {backup}"
        )
    except Exception as e:
        print(f"[QC DB] Migration error: {e}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def init_db():
    """Idempotent — buat tabel kalau belum ada. Auto-migrate kalau schema lama."""
    _ensure_db_dir()
    _maybe_migrate_legacy_schema()
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS qc_operators (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                is_supervisor INTEGER DEFAULT 0,
                pin_hash TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS qc_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resi_code TEXT NOT NULL,
                operator_id INTEGER,
                batch_id TEXT,
                marketplace TEXT,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                status TEXT DEFAULT 'in_progress',
                reject_reason TEXT,
                reject_notes TEXT,
                FOREIGN KEY (operator_id) REFERENCES qc_operators(id)
            );

            CREATE TABLE IF NOT EXISTS qc_session_progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                design_sku TEXT NOT NULL,
                bigseller_sku TEXT,
                target_packs INTEGER NOT NULL,
                scanned_packs INTEGER DEFAULT 0,
                is_non_stiker INTEGER DEFAULT 0,
                is_visual_confirmed INTEGER DEFAULT 0,
                last_scan_at TIMESTAMP,
                FOREIGN KEY (session_id) REFERENCES qc_sessions(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS qc_activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER,
                operator_id INTEGER,
                event_type TEXT NOT NULL,
                event_data TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_qc_sessions_resi ON qc_sessions(resi_code);
            CREATE INDEX IF NOT EXISTS idx_qc_sessions_status ON qc_sessions(status);
            CREATE INDEX IF NOT EXISTS idx_qc_log_session ON qc_activity_log(session_id);
            CREATE INDEX IF NOT EXISTS idx_qc_log_ts ON qc_activity_log(timestamp);
            """
        )


# Operator
def list_operators(active_only=True):
    with get_db() as conn:
        sql = "SELECT * FROM qc_operators"
        if active_only:
            sql += " WHERE is_active=1"
        sql += " ORDER BY name"
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def add_operator(name, is_supervisor=False, pin=None):
    name = (name or "").strip()
    if not name:
        raise ValueError("Nama operator tidak boleh kosong")
    pin_hash_value = hash_pin(pin) if pin else None
    with get_db() as conn:
        conn.execute(
            "INSERT INTO qc_operators(name, is_supervisor, pin_hash) VALUES(?,?,?)",
            (name, 1 if is_supervisor else 0, pin_hash_value),
        )


def deactivate_operator(name):
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE qc_operators SET is_active=0 WHERE name=?", (name,)
        )
    return cur.rowcount > 0


def verify_supervisor_pin(pin):
    """Return supervisor name kalau PIN match, else None."""
    if not pin:
        return None
    pin_h = hash_pin(pin)
    with get_db() as conn:
        row = conn.execute(
            "SELECT name FROM qc_operators "
            "WHERE is_supervisor=1 AND is_active=1 AND pin_hash=?",
            (pin_h,),
        ).fetchone()
    return row["name"] if row else None


# Session & progress
def find_active_session(resi_code):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM qc_sessions WHERE resi_code=? AND status='in_progress' "
            "ORDER BY started_at DESC LIMIT 1",
            (resi_code,),
        ).fetchone()
    return dict(row) if row else None


def find_completed_session(resi_code):
    """Cek apakah resi sudah pernah approved/rejected."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM qc_sessions WHERE resi_code=? "
            "AND status IN ('qc_approved','qc_rejected') "
            "ORDER BY completed_at DESC LIMIT 1",
            (resi_code,),
        ).fetchone()
    return dict(row) if row else None


def create_session(resi_code, operator_id, batch_id, marketplace, line_items):
    """Create session + populate qc_session_progress.
    line_items: list of dict {bigseller_sku, design_sku, target_packs, is_non_stiker}
    """
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO qc_sessions(resi_code, operator_id, batch_id, marketplace) "
            "VALUES(?,?,?,?)",
            (resi_code, operator_id, batch_id, marketplace),
        )
        sid = cur.lastrowid
        for li in line_items:
            conn.execute(
                "INSERT INTO qc_session_progress"
                "(session_id, design_sku, bigseller_sku, target_packs, is_non_stiker) "
                "VALUES(?,?,?,?,?)",
                (
                    sid,
                    li["design_sku"] or "",
                    li["bigseller_sku"],
                    li["target_packs"],
                    1 if li["is_non_stiker"] else 0,
                ),
            )
    return sid


def get_session_progress(session_id):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM qc_session_progress WHERE session_id=? ORDER BY id",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def increment_scan(progress_id):
    now = datetime.now().isoformat(timespec="seconds")
    with get_db() as conn:
        conn.execute(
            "UPDATE qc_session_progress "
            "SET scanned_packs=scanned_packs+1, last_scan_at=? WHERE id=?",
            (now, progress_id),
        )


def set_visual_confirm(progress_id, value=True):
    now = datetime.now().isoformat(timespec="seconds")
    with get_db() as conn:
        conn.execute(
            "UPDATE qc_session_progress SET is_visual_confirmed=?, last_scan_at=? WHERE id=?",
            (1 if value else 0, now, progress_id),
        )


def is_session_complete(session_id):
    progress = get_session_progress(session_id)
    if not progress:
        return False
    for p in progress:
        if p["is_non_stiker"]:
            if not p["is_visual_confirmed"]:
                return False
        else:
            if p["scanned_packs"] < p["target_packs"]:
                return False
    return True


def close_session(session_id, status, reject_reason=None, reject_notes=None):
    now = datetime.now().isoformat(timespec="seconds")
    with get_db() as conn:
        conn.execute(
            "UPDATE qc_sessions SET status=?, completed_at=?, reject_reason=?, reject_notes=? "
            "WHERE id=?",
            (status, now, reject_reason, reject_notes, session_id),
        )


def log_event(session_id, operator_id, event_type, event_data=None):
    data_json = json.dumps(event_data, ensure_ascii=False) if event_data else None
    with get_db() as conn:
        conn.execute(
            "INSERT INTO qc_activity_log"
            "(session_id, operator_id, event_type, event_data) VALUES(?,?,?,?)",
            (session_id, operator_id, event_type, data_json),
        )


def stats_today():
    """Stats QC hari ini: processed, approved, rejected, pass_rate."""
    today_start = datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()
    with get_db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) c FROM qc_sessions "
            "WHERE completed_at >= ? AND status IN ('qc_approved','qc_rejected')",
            (today_start,),
        ).fetchone()["c"]
        approved = conn.execute(
            "SELECT COUNT(*) c FROM qc_sessions "
            "WHERE completed_at >= ? AND status='qc_approved'",
            (today_start,),
        ).fetchone()["c"]
        rejected = conn.execute(
            "SELECT COUNT(*) c FROM qc_sessions "
            "WHERE completed_at >= ? AND status='qc_rejected'",
            (today_start,),
        ).fetchone()["c"]
    pass_rate = (approved / total * 100.0) if total > 0 else 0.0
    return {
        "processed": total,
        "approved": approved,
        "rejected": rejected,
        "pass_rate": pass_rate,
    }


# ============================================================
# SHEET ADAPTER
# ============================================================
class SheetAdapter:
    """Read/write sheet LIST_PESANAN.

    Cache resi data in-memory setelah refresh; auto-refresh kalau lebih dari 5 menit.
    Semua write operation update cache lokal juga.
    """

    HEADER_EXPECTED = [
        "Batch_ID", "Uploaded_At", "Nomor_Resi", "SKU", "Jumlah",
        "Marketplace", "Status", "QC_Operator", "QC_Completed_At", "QC_Notes",
    ]
    NUM_COLS = 10

    def __init__(self, spreadsheet):
        self.spreadsheet = spreadsheet
        self._sheet = None
        self._cache = None  # {resi: {batch_id, marketplace, rows: [...]}}
        self._cache_at = None
        self._lock = threading.Lock()

    def _get_sheet(self):
        if self._sheet is None:
            self._sheet = self.spreadsheet.worksheet(SHEET_NAME)
        return self._sheet

    def refresh(self):
        """Fetch full sheet, rebuild cache."""
        with self._lock:
            sheet = self._get_sheet()
            all_values = sheet.get_all_values()
            cache = defaultdict(
                lambda: {"batch_id": None, "marketplace": "", "rows": []}
            )
            if len(all_values) >= 2:
                for idx, row in enumerate(all_values[1:], start=2):
                    if len(row) < self.NUM_COLS:
                        row = list(row) + [""] * (self.NUM_COLS - len(row))
                    batch_id = (row[0] or "").strip()
                    resi = (row[2] or "").strip()
                    sku_raw = (row[3] or "").strip()
                    jumlah_str = (row[4] or "").strip()
                    marketplace = (row[5] or "").strip()
                    status = (row[6] or "").strip().lower()

                    if not resi or not sku_raw:
                        continue

                    try:
                        jumlah = int(float(jumlah_str)) if jumlah_str else 1
                    except (TypeError, ValueError):
                        jumlah = 1

                    cache[resi]["batch_id"] = batch_id or cache[resi]["batch_id"]
                    cache[resi]["marketplace"] = (
                        marketplace or cache[resi]["marketplace"]
                    )
                    cache[resi]["rows"].append(
                        {
                            "sheet_row": idx,
                            "bigseller_sku": sku_raw,
                            "jumlah": jumlah,
                            "status": status or STATUS_PENDING,
                            "qc_operator": (row[7] or "").strip(),
                            "qc_completed_at": (row[8] or "").strip(),
                            "qc_notes": (row[9] or "").strip(),
                        }
                    )
            self._cache = dict(cache)
            self._cache_at = datetime.now()

    def _ensure_cache(self, max_age_seconds=300):
        if (
            self._cache is None
            or self._cache_at is None
            or (datetime.now() - self._cache_at).total_seconds() > max_age_seconds
        ):
            self.refresh()

    def find_resi(self, resi_code):
        """Return resi data {batch_id, marketplace, rows} atau None."""
        self._ensure_cache()
        return self._cache.get((resi_code or "").strip())

    def get_pending_resi_count(self):
        self._ensure_cache()
        count = 0
        for r in self._cache.values():
            for row in r["rows"]:
                if row["status"] in (STATUS_PENDING, STATUS_IN_PROGRESS, ""):
                    count += 1
                    break  # one per resi
        return count

    def update_resi_qc_status(self, resi_code, status, operator_name, notes=""):
        """Tulis kolom G-J untuk SEMUA row resi tsb. Return True kalau ada yg di-update."""
        resi_data = self.find_resi(resi_code)
        if not resi_data:
            return False
        sheet = self._get_sheet()
        completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        batch_updates = []
        for row in resi_data["rows"]:
            r = row["sheet_row"]
            batch_updates.append(
                {
                    "range": f"G{r}:J{r}",
                    "values": [[status, operator_name, completed_at, notes]],
                }
            )
        if not batch_updates:
            return False

        with self._lock:
            sheet.batch_update(batch_updates)
            for row in resi_data["rows"]:
                row["status"] = status
                row["qc_operator"] = operator_name
                row["qc_completed_at"] = completed_at
                row["qc_notes"] = notes
        return True


# ============================================================
# UI WINDOW
# ============================================================
class QcStasiunWindow(ctk.CTkToplevel):
    """Stasiun QC — window terpisah dari aplikasi utama."""

    def __init__(self, parent, spreadsheet):
        super().__init__(parent)
        self.parent = parent
        self.spreadsheet = spreadsheet
        self.adapter = SheetAdapter(spreadsheet)

        # Stub operator: pakai nama workstation supaya audit trail tetap ada
        # tanpa minta login. Ganti via os env atau hardcode kalau perlu.
        qc_name = (
            os.environ.get("COMPUTERNAME")
            or os.environ.get("USERNAME")
            or "QC"
        )
        self.current_operator = {"id": None, "name": qc_name}
        self.current_session_id = None
        self.current_resi_code = None
        self.current_resi_data = None  # cached data dari adapter
        self.current_progress = []  # list of dict from get_session_progress

        # Pastikan DB siap
        try:
            init_db()
        except Exception as e:
            messagebox.showerror(
                "DB Error",
                f"Gagal inisialisasi database QC: {e}\n\nPath: {DB_FILE}",
            )
            self.after(100, self.destroy)
            return

        self.title("Stasiun QC HOG")
        self.geometry("1080x760")
        self.minsize(960, 680)

        try:
            self.transient(parent)
        except Exception:
            pass

        self._build_layout()
        # Pre-fetch sheet data lalu langsung tampilkan idle (no login)
        self._refresh_sheet_data(silent=True, then=self._show_idle)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- helpers shared ----
    def speak(self, text):
        try:
            self.parent.speak(text)
        except Exception:
            pass

    def beep_match(self):
        try:
            winsound.Beep(1500, 150)
        except Exception:
            pass

    def beep_mismatch(self):
        try:
            winsound.Beep(400, 400)
        except Exception:
            pass

    def beep_complete(self):
        try:
            winsound.Beep(2000, 120)
            winsound.Beep(2400, 180)
        except Exception:
            pass

    def _run_async(self, fn, on_done=None, on_error=None):
        """Run blocking task di thread, callback di main thread via .after()."""
        def worker():
            try:
                result = fn()
                if on_done:
                    self.after(0, lambda: on_done(result))
            except Exception as e:
                err = e
                if on_error:
                    self.after(0, lambda: on_error(err))
                else:
                    self.after(
                        0,
                        lambda: messagebox.showerror("Error", str(err)),
                    )

        threading.Thread(target=worker, daemon=True).start()

    # ---- layout skeleton ----
    def _build_layout(self):
        # Header
        self.header = ctk.CTkFrame(self, fg_color=COLOR_DARK, corner_radius=0, height=64)
        self.header.pack(fill="x")
        self.header.pack_propagate(False)

        self.lbl_title = ctk.CTkLabel(
            self.header,
            text="STASIUN QC HOG",
            font=("Segoe UI", 20, "bold"),
            text_color="#f8fafc",
        )
        self.lbl_title.pack(side="left", padx=20)

        self.header_right = ctk.CTkFrame(self.header, fg_color="transparent")
        self.header_right.pack(side="right", padx=20)

        self.btn_refresh = ctk.CTkButton(
            self.header_right,
            text="Refresh Data",
            width=120,
            height=32,
            fg_color=COLOR_CYAN,
            hover_color="#138a99",
            command=self._refresh_sheet_data,
        )
        self.btn_refresh.pack(side="right", padx=5)

        # Body
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=20, pady=10)

        # Footer stats
        self.footer = ctk.CTkFrame(self, fg_color=COLOR_DARK, corner_radius=0, height=40)
        self.footer.pack(fill="x", side="bottom")
        self.footer.pack_propagate(False)
        self.lbl_stats = ctk.CTkLabel(
            self.footer,
            text="Stats hari ini: -",
            font=("Segoe UI", 11),
            text_color=COLOR_INFO,
        )
        self.lbl_stats.pack(side="left", padx=20, pady=8)

        self.lbl_pending = ctk.CTkLabel(
            self.footer,
            text="",
            font=("Segoe UI", 11),
            text_color=COLOR_INFO,
        )
        self.lbl_pending.pack(side="right", padx=20, pady=8)

        self._refresh_stats_label()

    def _clear_body(self):
        for w in self.body.winfo_children():
            w.destroy()

    def _refresh_stats_label(self):
        try:
            s = stats_today()
            self.lbl_stats.configure(
                text=(
                    f"Hari ini: {s['processed']} resi diproses | "
                    f"{s['approved']} approved | "
                    f"{s['rejected']} rejected | "
                    f"Pass rate {s['pass_rate']:.1f}%"
                )
            )
        except Exception as e:
            self.lbl_stats.configure(text=f"Stats error: {e}")

    # ---- IDLE STATE ----
    def _show_idle(self):
        self.current_session_id = None
        self.current_resi_code = None
        self.current_resi_data = None
        self.current_progress = []
        # current_operator (stub workstation name) tetap dipertahankan

        self._clear_body()

        wrap = ctk.CTkFrame(
            self.body,
            fg_color="#0f172a",
            border_width=2,
            border_color=COLOR_BORDER,
            corner_radius=12,
        )
        wrap.pack(fill="both", expand=True, padx=80, pady=40)

        ctk.CTkLabel(
            wrap,
            text="SCAN BARCODE RESI POLYMAILER",
            font=("Segoe UI", 24, "bold"),
            text_color="#f8fafc",
        ).pack(pady=(50, 8))

        ctk.CTkLabel(
            wrap,
            text=(
                "Polymailer di tangan, belum di-seal. Arahkan scanner ke barcode resi.\n"
                "Setelah resi terbaca, scan tiap pack stiker untuk verifikasi isi sebelum seal."
            ),
            font=("Segoe UI", 12),
            text_color=COLOR_INFO,
            justify="center",
        ).pack(pady=(0, 30))

        self.entry_resi = ctk.CTkEntry(
            wrap,
            placeholder_text="Scan barcode resi disini...",
            height=72,
            font=("Segoe UI", 24, "bold"),
            justify="center",
        )
        self.entry_resi.pack(fill="x", padx=80, pady=10)
        self.entry_resi.bind("<Return>", self._on_resi_scan)
        self.after(100, self.entry_resi.focus_set)

        self.lbl_idle_status = ctk.CTkLabel(
            wrap, text="", font=("Segoe UI", 13), text_color=COLOR_INFO
        )
        self.lbl_idle_status.pack(pady=(20, 30))

    def _on_resi_scan(self, _event=None):
        if not hasattr(self, "entry_resi"):
            return
        resi = self.entry_resi.get().strip()
        self.entry_resi.delete(0, "end")
        if not resi:
            return
        self.beep_match()
        self.lbl_idle_status.configure(
            text=f"Mencari resi {resi}...", text_color=COLOR_INFO
        )

        def fetch():
            data = self.adapter.find_resi(resi)
            return resi, data

        def done(result):
            resi_code, data = result
            if not data:
                self._idle_show_error(
                    f"Resi '{resi_code}' tidak ditemukan di LIST_PESANAN.\n"
                    "Pastikan tim gudang sudah upload export BigSeller."
                )
                self.speak("Resi tidak ditemukan")
                self.beep_mismatch()
                return

            # Cek apakah sudah pernah selesai
            done_session = find_completed_session(resi_code)
            if done_session and done_session["status"] == STATUS_QC_APPROVED:
                self._idle_show_warning(
                    f"Resi {resi_code} SUDAH PERNAH approved oleh "
                    f"{done_session.get('completed_at', '-')}. "
                    f"Tidak perlu di-QC ulang."
                )
                self.speak("Resi sudah pernah disetujui")
                return

            # Resume session aktif kalau ada
            active = find_active_session(resi_code)
            if active:
                self._load_session(resi_code, data, active["id"])
                self.lbl_idle_status.configure(text="Resume sesi sebelumnya...")
                return

            # Build line items dari sheet rows
            line_items = self._sheet_rows_to_line_items(data["rows"])
            if not line_items:
                self._idle_show_error(
                    f"Resi {resi_code} tidak punya line item valid."
                )
                return

            sid = create_session(
                resi_code,
                self.current_operator["id"],
                data.get("batch_id"),
                data.get("marketplace"),
                line_items,
            )
            log_event(
                sid,
                self.current_operator["id"],
                "session_start",
                {"resi": resi_code, "batch": data.get("batch_id")},
            )
            self._load_session(resi_code, data, sid)

        def err(e):
            self._idle_show_error(f"Error fetch sheet: {e}")
            self.beep_mismatch()

        self._run_async(fetch, on_done=done, on_error=err)

    def _idle_show_error(self, msg):
        self.lbl_idle_status.configure(text=msg, text_color=COLOR_MERAH)
        self.after(150, self.entry_resi.focus_set)

    def _idle_show_warning(self, msg):
        self.lbl_idle_status.configure(text=msg, text_color=COLOR_KUNING)
        self.after(150, self.entry_resi.focus_set)

    def _sheet_rows_to_line_items(self, sheet_rows):
        """Convert rows dari sheet ke line items untuk session.
        Aggregate by design_sku — kalau ada 2 row dengan SKU sama (misal 431-RETRO-10PCS qty=2),
        target_packs di-sum.
        """
        agg = {}  # design_sku -> {bigseller_sku, target_packs, is_non_stiker}
        for row in sheet_rows:
            sku_raw = row["bigseller_sku"]
            jumlah = row["jumlah"]
            design_sku, pcs_per_paket = parse_sku(sku_raw)

            if design_sku is None:
                # Non-stiker (banner, stamp, pin, dll)
                key = sku_raw  # gunakan SKU asli sebagai key supaya tidak nabrak design lain
                if key in agg:
                    continue  # 1 row visual confirm cukup
                agg[key] = {
                    "design_sku": "",
                    "bigseller_sku": sku_raw,
                    "target_packs": 0,
                    "is_non_stiker": True,
                }
            else:
                target = calculate_packs_needed(pcs_per_paket, jumlah)
                if design_sku in agg:
                    agg[design_sku]["target_packs"] += target
                else:
                    agg[design_sku] = {
                        "design_sku": design_sku,
                        "bigseller_sku": sku_raw,
                        "target_packs": target,
                        "is_non_stiker": False,
                    }
        return list(agg.values())

    # ---- RESI LOADED STATE ----
    def _load_session(self, resi_code, resi_data, session_id):
        self.current_resi_code = resi_code
        self.current_resi_data = resi_data
        self.current_session_id = session_id
        self.current_progress = get_session_progress(session_id)
        self._show_resi_loaded()

    def _show_resi_loaded(self):
        self._clear_body()

        # Top: resi info
        info = ctk.CTkFrame(
            self.body,
            fg_color="#0f172a",
            border_width=1,
            border_color=COLOR_BORDER,
            corner_radius=10,
        )
        info.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(
            info,
            text=f"Resi: {self.current_resi_code}",
            font=("Segoe UI", 18, "bold"),
            text_color="#f8fafc",
        ).pack(side="left", padx=20, pady=12)

        meta_text = (
            f"Marketplace: {self.current_resi_data.get('marketplace') or 'Unknown'}  •  "
            f"Batch: {self.current_resi_data.get('batch_id') or '-'}"
        )
        ctk.CTkLabel(
            info, text=meta_text, font=("Segoe UI", 12), text_color=COLOR_INFO
        ).pack(side="right", padx=20)

        # Middle: split (left checklist, right scan + buttons)
        middle = ctk.CTkFrame(self.body, fg_color="transparent")
        middle.pack(fill="both", expand=True)

        # Left — checklist
        left = ctk.CTkFrame(middle, fg_color="transparent")
        left.pack(side="left", fill="both", expand=True, padx=(0, 10))

        ctk.CTkLabel(
            left, text="Checklist Item Pesanan", font=("Segoe UI", 14, "bold")
        ).pack(anchor="w", pady=(0, 5))

        self.checklist_frame = ctk.CTkScrollableFrame(left, fg_color="#0f172a")
        self.checklist_frame.pack(fill="both", expand=True)
        self.progress_widgets = {}  # progress_id -> {row_frame, status_lbl, count_lbl}
        self._render_checklist()

        # Right — scan & buttons
        right = ctk.CTkFrame(middle, fg_color="transparent", width=380)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)

        ctk.CTkLabel(
            right, text="Scan Pack Stiker", font=("Segoe UI", 14, "bold")
        ).pack(anchor="w", pady=(0, 5))

        self.entry_pack = ctk.CTkEntry(
            right,
            placeholder_text="Scan barcode pack...",
            height=56,
            font=("Segoe UI", 18, "bold"),
            justify="center",
        )
        self.entry_pack.pack(fill="x", pady=5)
        self.entry_pack.bind("<Return>", self._on_pack_scan)
        self.after(150, self.entry_pack.focus_set)

        self.lbl_last_scan = ctk.CTkLabel(
            right,
            text="Belum ada scan.",
            font=("Segoe UI", 12),
            text_color=COLOR_INFO,
        )
        self.lbl_last_scan.pack(anchor="w", pady=(8, 12))

        # Buttons
        self.btn_approve = ctk.CTkButton(
            right,
            text="✓ APPROVE & SEAL",
            height=52,
            font=("Segoe UI", 15, "bold"),
            fg_color=COLOR_HIJAU,
            hover_color="#1e7e34",
            state="disabled",
            command=self._on_approve,
        )
        self.btn_approve.pack(fill="x", pady=4)

        self.btn_reject = ctk.CTkButton(
            right,
            text="✗ REJECT",
            height=44,
            font=("Segoe UI", 13, "bold"),
            fg_color=COLOR_MERAH,
            hover_color="#a82835",
            command=self._on_reject,
        )
        self.btn_reject.pack(fill="x", pady=4)

        self.btn_manual = ctk.CTkButton(
            right,
            text="Manual Entry (PIN Supervisor)",
            height=36,
            font=("Segoe UI", 11),
            fg_color=COLOR_KUNING,
            text_color="#000",
            hover_color="#dba90c",
            command=self._on_manual_entry,
        )
        self.btn_manual.pack(fill="x", pady=4)

        self.btn_cancel_session = ctk.CTkButton(
            right,
            text="← Batalkan, Kembali",
            height=32,
            font=("Segoe UI", 11),
            fg_color=COLOR_GREY_BTN,
            hover_color=COLOR_GREY_BTN_HOVER,
            command=self._on_cancel_session,
        )
        self.btn_cancel_session.pack(fill="x", pady=(20, 4))

        self._update_approve_button()

    def _render_checklist(self):
        for w in self.checklist_frame.winfo_children():
            w.destroy()
        self.progress_widgets = {}

        for p in self.current_progress:
            row = ctk.CTkFrame(self.checklist_frame, fg_color="#1e293b", corner_radius=6)
            row.pack(fill="x", pady=3, padx=2)

            # Status icon kolom kiri
            status_lbl = ctk.CTkLabel(
                row, text="◯", font=("Segoe UI", 18, "bold"), width=30
            )
            status_lbl.pack(side="left", padx=(8, 4), pady=8)

            # Info middle (SKU + counter)
            mid = ctk.CTkFrame(row, fg_color="transparent")
            mid.pack(side="left", fill="both", expand=True, padx=4, pady=8)

            sku_label = (
                p["bigseller_sku"]
                if p["is_non_stiker"]
                else f"{p['bigseller_sku']}  (ID: {p['design_sku']})"
            )
            ctk.CTkLabel(
                mid,
                text=sku_label,
                font=("Segoe UI", 13, "bold"),
                anchor="w",
            ).pack(fill="x")

            count_lbl = ctk.CTkLabel(
                mid, text="", font=("Segoe UI", 11), anchor="w", text_color=COLOR_INFO
            )
            count_lbl.pack(fill="x")

            # Right side: action button (visual confirm) for non-stiker
            action_frame = ctk.CTkFrame(row, fg_color="transparent")
            action_frame.pack(side="right", padx=8, pady=8)

            confirm_btn = None
            if p["is_non_stiker"]:
                confirm_btn = ctk.CTkButton(
                    action_frame,
                    text="Visual Confirm",
                    width=130,
                    height=32,
                    font=("Segoe UI", 11),
                    fg_color=COLOR_KUNING,
                    text_color="#000",
                    hover_color="#dba90c",
                    command=lambda pid=p["id"]: self._on_visual_confirm(pid),
                )
                confirm_btn.pack()

            self.progress_widgets[p["id"]] = {
                "row": row,
                "status_lbl": status_lbl,
                "count_lbl": count_lbl,
                "confirm_btn": confirm_btn,
            }
            self._refresh_progress_row(p)

    def _refresh_progress_row(self, p):
        widgets = self.progress_widgets.get(p["id"])
        if not widgets:
            return
        if p["is_non_stiker"]:
            if p["is_visual_confirmed"]:
                widgets["status_lbl"].configure(text="✔", text_color=COLOR_HIJAU)
                widgets["count_lbl"].configure(
                    text="Non-stiker — sudah dikonfirmasi visual",
                    text_color=COLOR_HIJAU,
                )
                if widgets["confirm_btn"]:
                    widgets["confirm_btn"].configure(state="disabled")
            else:
                widgets["status_lbl"].configure(text="□", text_color=COLOR_KUNING)
                widgets["count_lbl"].configure(
                    text="Non-stiker — perlu Visual Confirm", text_color=COLOR_KUNING
                )
        else:
            scanned = p["scanned_packs"]
            target = p["target_packs"]
            bar_full = "▣" * min(scanned, target)
            bar_empty = "▢" * max(0, target - scanned)
            count_text = f"{bar_full}{bar_empty}  {scanned}/{target} pack"
            if scanned >= target:
                widgets["status_lbl"].configure(text="✔", text_color=COLOR_HIJAU)
                widgets["count_lbl"].configure(text=count_text, text_color=COLOR_HIJAU)
            elif scanned > 0:
                widgets["status_lbl"].configure(text="●", text_color=COLOR_CYAN)
                widgets["count_lbl"].configure(text=count_text, text_color=COLOR_CYAN)
            else:
                widgets["status_lbl"].configure(text="◯", text_color=COLOR_INFO)
                widgets["count_lbl"].configure(text=count_text, text_color=COLOR_INFO)

    def _update_approve_button(self):
        if is_session_complete(self.current_session_id):
            self.btn_approve.configure(state="normal")
        else:
            self.btn_approve.configure(state="disabled")

    # ---- SCAN PACK ----
    def _on_pack_scan(self, _event=None):
        scanned = self.entry_pack.get().strip()
        self.entry_pack.delete(0, "end")
        if not scanned:
            return
        self._process_scan(scanned, source="scan")

    def _process_scan(self, scanned_value, source="scan"):
        # Extract numeric ID dari yang di-scan
        numeric_id, _ = parse_sku(scanned_value)
        target_id = numeric_id or scanned_value.strip()

        # Cari progress row yang match
        match_p = None
        for p in self.current_progress:
            if p["is_non_stiker"]:
                continue
            if p["scanned_packs"] >= p["target_packs"]:
                continue
            if p["design_sku"] == target_id:
                match_p = p
                break

        if match_p:
            increment_scan(match_p["id"])
            match_p["scanned_packs"] += 1
            self.beep_match()
            self.lbl_last_scan.configure(
                text=f"✔ MATCH: {target_id}  ({match_p['scanned_packs']}/{match_p['target_packs']})",
                text_color=COLOR_HIJAU,
            )
            log_event(
                self.current_session_id,
                self.current_operator["id"],
                "scan_match" if source == "scan" else f"{source}_match",
                {"scanned": scanned_value, "design_sku": target_id},
            )
            self._refresh_progress_row(match_p)
            self._update_approve_button()
            if is_session_complete(self.current_session_id):
                self._on_session_complete()
        else:
            # Cek mismatch reason: completed atau tidak ada di resi
            already_full = any(
                p["design_sku"] == target_id
                and not p["is_non_stiker"]
                and p["scanned_packs"] >= p["target_packs"]
                for p in self.current_progress
            )
            reason = (
                "sudah penuh, tidak perlu lagi"
                if already_full
                else "tidak terdaftar di resi ini"
            )
            self.beep_mismatch()
            self.lbl_last_scan.configure(
                text=f"✗ MISMATCH: {target_id} — {reason}", text_color=COLOR_MERAH
            )
            self.speak("Mismatch")
            log_event(
                self.current_session_id,
                self.current_operator["id"],
                "scan_mismatch",
                {
                    "scanned": scanned_value,
                    "design_sku": target_id,
                    "reason": "already_full" if already_full else "not_in_resi",
                    "source": source,
                },
            )
        self.after(100, self.entry_pack.focus_set)

    def _on_visual_confirm(self, progress_id):
        for p in self.current_progress:
            if p["id"] == progress_id:
                set_visual_confirm(progress_id, True)
                p["is_visual_confirmed"] = 1
                log_event(
                    self.current_session_id,
                    self.current_operator["id"],
                    "visual_confirm",
                    {"progress_id": progress_id, "sku": p["bigseller_sku"]},
                )
                self.beep_match()
                self._refresh_progress_row(p)
                self._update_approve_button()
                if is_session_complete(self.current_session_id):
                    self._on_session_complete()
                break

    def _on_session_complete(self):
        self.beep_complete()
        self.speak("Resi selesai, silakan seal")
        self.lbl_last_scan.configure(
            text="✔ SEMUA SKU SUDAH TERVERIFIKASI — Klik APPROVE untuk seal",
            text_color=COLOR_HIJAU,
        )

    # ---- MANUAL ENTRY ----
    def _on_manual_entry(self):
        ManualEntryDialog(self, self._handle_manual_entry)

    def _handle_manual_entry(self, sku_value):
        log_event(
            self.current_session_id,
            self.current_operator["id"],
            "manual_entry",
            {"sku": sku_value},
        )
        self._process_scan(sku_value, source="manual_entry")

    # ---- APPROVE / REJECT ----
    def _on_approve(self):
        if not is_session_complete(self.current_session_id):
            return
        if not messagebox.askyesno(
            "Konfirmasi Approve",
            f"Seal polymailer untuk resi {self.current_resi_code}?",
            parent=self,
        ):
            return
        operator_name = self.current_operator["name"]
        resi_code = self.current_resi_code
        sid = self.current_session_id

        def do_update():
            self.adapter.update_resi_qc_status(
                resi_code, STATUS_QC_APPROVED, operator_name, ""
            )
            close_session(sid, STATUS_QC_APPROVED)
            log_event(sid, self.current_operator["id"], "approve", {"resi": resi_code})
            return True

        def done(_):
            self._refresh_stats_label()
            self.beep_complete()
            self.speak("Approved")
            self._show_idle()
            self.lbl_idle_status.configure(
                text=f"✔ Resi {resi_code} approved. Silakan seal & lanjut resi berikutnya.",
                text_color=COLOR_HIJAU,
            )

        def err(e):
            messagebox.showerror(
                "Gagal Update Sheet",
                f"Status approve sudah disimpan lokal, tapi gagal update sheet:\n{e}\n\n"
                "Klik 'Refresh Data' lalu coba scan resi ini lagi untuk re-sync.",
                parent=self,
            )
            close_session(sid, STATUS_QC_APPROVED)
            log_event(
                sid,
                self.current_operator["id"],
                "approve_sheet_fail",
                {"error": str(e)},
            )

        self.btn_approve.configure(state="disabled", text="Menyimpan...")
        self._run_async(do_update, on_done=done, on_error=err)

    def _on_reject(self):
        RejectDialog(self, self._handle_reject_submit)

    def _handle_reject_submit(self, reason, notes):
        operator_name = self.current_operator["name"]
        resi_code = self.current_resi_code
        sid = self.current_session_id
        full_notes = f"{reason}: {notes}".strip(": ").strip()

        def do_update():
            self.adapter.update_resi_qc_status(
                resi_code, STATUS_QC_REJECTED, operator_name, full_notes
            )
            close_session(sid, STATUS_QC_REJECTED, reject_reason=reason, reject_notes=notes)
            log_event(
                sid,
                self.current_operator["id"],
                "reject",
                {"resi": resi_code, "reason": reason, "notes": notes},
            )
            return True

        def done(_):
            self._refresh_stats_label()
            self.beep_mismatch()
            self.speak("Rejected")
            self._show_idle()
            self.lbl_idle_status.configure(
                text=f"✗ Resi {resi_code} di-reject ({reason}). Pisahkan polymailer untuk koreksi.",
                text_color=COLOR_MERAH,
            )

        def err(e):
            messagebox.showerror(
                "Gagal Update Sheet",
                f"Status reject sudah disimpan lokal, tapi gagal update sheet:\n{e}",
                parent=self,
            )
            close_session(sid, STATUS_QC_REJECTED, reject_reason=reason, reject_notes=notes)

        self._run_async(do_update, on_done=done, on_error=err)

    def _on_cancel_session(self):
        if not messagebox.askyesno(
            "Batalkan Sesi",
            "Sesi QC akan tetap tersimpan sebagai 'in_progress'. "
            "Anda bisa scan resi yang sama lagi nanti untuk lanjut dari progress sekarang.\n\n"
            "Lanjutkan?",
            parent=self,
        ):
            return
        log_event(
            self.current_session_id,
            self.current_operator["id"],
            "cancel_session",
            {"resi": self.current_resi_code},
        )
        self._show_idle()

    # ---- SHEET REFRESH ----
    def _refresh_sheet_data(self, silent=False, then=None):
        if not silent:
            self.btn_refresh.configure(state="disabled", text="Refreshing...")

        def do():
            self.adapter.refresh()
            return self.adapter.get_pending_resi_count()

        def done(count):
            self.lbl_pending.configure(
                text=f"Pending di sheet: {count} resi", text_color=COLOR_INFO
            )
            if not silent:
                self.btn_refresh.configure(state="normal", text="Refresh Data")
            if then:
                then()

        def err(e):
            if not silent:
                self.btn_refresh.configure(state="normal", text="Refresh Data")
            messagebox.showerror(
                "Gagal Refresh",
                f"Gagal fetch sheet LIST_PESANAN:\n{e}",
                parent=self,
            )

        self._run_async(do, on_done=done, on_error=err)

    # ---- CLOSE ----
    def _on_close(self):
        if self.current_session_id and self.current_operator:
            log_event(
                self.current_session_id,
                self.current_operator["id"],
                "window_close_with_active_session",
                {"resi": self.current_resi_code},
            )
        self.destroy()


# ============================================================
# DIALOGS
# ============================================================
class RejectDialog(ctk.CTkToplevel):
    def __init__(self, parent, on_submit):
        super().__init__(parent)
        self.on_submit = on_submit
        self.title("Reject Resi")
        self.geometry("440x360")
        self.resizable(False, False)
        try:
            self.transient(parent)
            self.grab_set()
        except Exception:
            pass

        ctk.CTkLabel(
            self, text="Reject Resi", font=("Segoe UI", 16, "bold")
        ).pack(pady=(15, 5))

        ctk.CTkLabel(
            self, text="Pilih alasan reject:", font=("Segoe UI", 12)
        ).pack(anchor="w", padx=20, pady=(10, 4))

        self.reason_var = ctk.StringVar(value=REJECT_REASONS[0])
        ctk.CTkOptionMenu(
            self,
            variable=self.reason_var,
            values=REJECT_REASONS,
            width=400,
            height=36,
        ).pack(padx=20, pady=4)

        ctk.CTkLabel(
            self, text="Catatan tambahan (opsional):", font=("Segoe UI", 12)
        ).pack(anchor="w", padx=20, pady=(15, 4))

        self.txt_notes = ctk.CTkTextbox(self, height=100)
        self.txt_notes.pack(fill="x", padx=20, pady=4)

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(pady=15)
        ctk.CTkButton(
            btn_frame,
            text="Batal",
            fg_color=COLOR_GREY_BTN,
            hover_color=COLOR_GREY_BTN_HOVER,
            width=120,
            command=self.destroy,
        ).pack(side="left", padx=5)
        ctk.CTkButton(
            btn_frame,
            text="Submit Reject",
            fg_color=COLOR_MERAH,
            hover_color="#a82835",
            width=160,
            command=self._submit,
        ).pack(side="left", padx=5)

    def _submit(self):
        reason = self.reason_var.get()
        notes = self.txt_notes.get("1.0", "end").strip()
        self.destroy()
        self.on_submit(reason, notes)


class ManualEntryDialog(ctk.CTkToplevel):
    def __init__(self, parent, on_submit):
        super().__init__(parent)
        self.on_submit = on_submit
        self.title("Manual Entry")
        self.geometry("420x220")
        self.resizable(False, False)
        try:
            self.transient(parent)
            self.grab_set()
        except Exception:
            pass

        ctk.CTkLabel(
            self,
            text="Manual Entry (Barcode Rusak)",
            font=("Segoe UI", 15, "bold"),
        ).pack(pady=(15, 5))
        ctk.CTkLabel(
            self,
            text="Ketik SKU / ID Desain secara manual.",
            font=("Segoe UI", 11),
            text_color=COLOR_INFO,
        ).pack(pady=(0, 15))

        ctk.CTkLabel(
            self, text="SKU / ID Desain:", font=("Segoe UI", 12)
        ).pack(anchor="w", padx=20)
        self.entry_sku = ctk.CTkEntry(self, height=38, font=("Segoe UI", 14))
        self.entry_sku.pack(fill="x", padx=20, pady=4)

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(pady=15)
        ctk.CTkButton(
            btn_frame,
            text="Batal",
            fg_color=COLOR_GREY_BTN,
            hover_color=COLOR_GREY_BTN_HOVER,
            width=120,
            command=self.destroy,
        ).pack(side="left", padx=5)
        ctk.CTkButton(
            btn_frame,
            text="Submit",
            fg_color=COLOR_HIJAU,
            hover_color="#1e7e34",
            width=160,
            command=self._submit,
        ).pack(side="left", padx=5)

        self.after(100, self.entry_sku.focus_set)
        self.bind("<Return>", lambda e: self._submit())

    def _submit(self):
        sku = self.entry_sku.get().strip()
        if not sku:
            messagebox.showwarning(
                "Input Kurang", "SKU harus diisi.", parent=self
            )
            return
        self.destroy()
        self.on_submit(sku)
