"""
erp_client.py
HTTP client untuk PostgREST di ERP heavyobjectgroup (db.heavyobjectgroup.com).

Menggantikan integrasi gspread (Google Sheets) untuk semua data layer
sortir_stiker_desain. Auth pakai bridge JWT HS256 yang di-sign lokal pakai
VPS_DB_JWT_SECRET — identik dengan pattern erp-frontend/src/lib/server/vpsDbJwt.ts.

Stdlib only (no requests/httpx) supaya konsisten dengan project (updater.py,
HTTP bridge baru di app.py pakai stdlib).

Konfigurasi (di config.json):
    erp_base_url    : "https://db.heavyobjectgroup.com" (atau staging URL)
    erp_jwt_secret  : VPS_DB_JWT_SECRET (string panjang base64; HARUS match
                      dengan PGRST_JWT_SECRET di VPS /opt/db/conf/postgrest.env)
    erp_location_id : UUID lokasi default "Gudang Stiker Siap Jual"
                      (dipakai untuk goods_issued/restock kalau tidak override)
    erp_jwt_role    : opsional, default "service_role" (full access)

Penggunaan dasar:
    from erp_client import ERPClient
    client = ERPClient.from_config(config_data)
    stiker_items = client.fetch_database_stiker()        # list of dicts
    client.issue_goods(item_id, qty, location_id, nomor_resi="SPXID...")
    pesanan = client.fetch_list_pesanan(today=True)
    client.update_qc_status(order_id, "qc_approved", operator_name, notes="")
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any


STIKER_SUB_CATEGORY_ID = "39bcf059-d2df-462a-a403-08faa35d8ba7"
DEFAULT_TIMEOUT_SECONDS = 30
JWT_TTL_SECONDS = 3600
JWT_REFRESH_BUFFER = 60  # refresh kalau sisa < 60s
CACHE_TTL_SECONDS = 300


class ERPClientError(Exception):
    """Generic error untuk semua kegagalan ERPClient (HTTP / parse / auth)."""

    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def mint_jwt(secret: str, role: str = "service_role", user_id: str | None = None,
             ttl_seconds: int = JWT_TTL_SECONDS) -> str:
    """Mint HS256 JWT untuk PostgREST. Compatible dengan vpsDbJwt.ts (frontend SvelteKit).

    Roles yang dikenal PostgREST:
        - "anon"           : akses anonim
        - "authenticated"  : akses user normal (perlu sub=user_id)
        - "service_role"   : bypass RLS (untuk desktop app server-to-server)
    """
    if not secret:
        raise ERPClientError("VPS_DB_JWT_SECRET tidak boleh kosong.")
    now = int(time.time())
    body: dict[str, Any] = {"iat": now, "exp": now + ttl_seconds, "role": role}
    if user_id:
        body["sub"] = user_id
    header_b64 = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body_b64 = _b64url(json.dumps(body, separators=(",", ":")).encode())
    signing_input = f"{header_b64}.{body_b64}".encode("ascii")
    sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header_b64}.{body_b64}.{_b64url(sig)}"


class ERPClient:
    """REST client untuk PostgREST VPS. Thread-safe (lock per refresh JWT)."""

    def __init__(self, base_url: str, jwt_secret: str,
                 default_location_id: str | None = None,
                 role: str = "service_role",
                 user_id: str | None = None,
                 timeout: int = DEFAULT_TIMEOUT_SECONDS):
        self.base_url = base_url.rstrip("/")
        self.jwt_secret = jwt_secret
        self.default_location_id = default_location_id
        self.role = role
        self.user_id = user_id
        self.timeout = timeout
        self._jwt: str | None = None
        self._jwt_exp: int = 0
        self._lock = threading.Lock()
        # Cache untuk fetch_database_stiker (8K SKU ~ 2-3 detik per fetch)
        self._stiker_cache: list[dict[str, Any]] | None = None
        self._stiker_cache_at: float = 0.0
        self._location_cache: dict[str, str] | None = None  # name(lower) -> id

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------
    @classmethod
    def from_config(cls, config: dict[str, Any]) -> ERPClient:
        base = (config.get("erp_base_url") or "").strip()
        secret = (config.get("erp_jwt_secret") or "").strip()
        loc_id = (config.get("erp_location_id") or "").strip() or None
        role = (config.get("erp_jwt_role") or "service_role").strip()
        if not base:
            raise ERPClientError("config.json: 'erp_base_url' belum di-set.")
        if not secret:
            raise ERPClientError("config.json: 'erp_jwt_secret' belum di-set.")
        return cls(base_url=base, jwt_secret=secret, default_location_id=loc_id, role=role)

    # ------------------------------------------------------------------
    # JWT
    # ------------------------------------------------------------------
    def _ensure_jwt(self) -> str:
        with self._lock:
            now = int(time.time())
            if not self._jwt or (self._jwt_exp - now) < JWT_REFRESH_BUFFER:
                self._jwt = mint_jwt(self.jwt_secret, role=self.role, user_id=self.user_id)
                self._jwt_exp = now + JWT_TTL_SECONDS
            return self._jwt

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------
    def _request(self, method: str, path: str,
                 params: dict[str, Any] | None = None,
                 body: Any = None,
                 prefer: str | None = None) -> Any:
        """Kirim request ke /rest/v1/<path>. Return parsed JSON atau None.

        Raise ERPClientError untuk HTTP error / parse error / timeout.
        """
        jwt = self._ensure_jwt()
        url = f"{self.base_url}/rest/v1/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True, safe="*,():.=")
        headers = {
            "Authorization": f"Bearer {jwt}",
            "apikey": jwt,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer

        data: bytes | None = None
        if body is not None:
            data = json.dumps(body, default=_json_default).encode("utf-8")

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                if not raw:
                    return None
                ctype = resp.headers.get("Content-Type", "")
                txt = raw.decode("utf-8", errors="replace")
                if "json" in ctype.lower():
                    try:
                        return json.loads(txt)
                    except json.JSONDecodeError as je:
                        raise ERPClientError(f"Parse JSON gagal: {je} body={txt[:200]}") from je
                return txt
        except urllib.error.HTTPError as he:
            err_body = ""
            try:
                err_body = he.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise ERPClientError(
                f"HTTP {he.code} {method} {path}: {err_body[:400]}",
                status=he.code,
                body=err_body,
            ) from he
        except urllib.error.URLError as ue:
            raise ERPClientError(f"Koneksi ERP gagal ({method} {path}): {ue.reason}") from ue
        except TimeoutError as te:
            raise ERPClientError(f"Timeout {self.timeout}s ke ERP ({method} {path})") from te

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------
    def ping(self) -> bool:
        """Test koneksi + JWT valid. Return True kalau OK, raise kalau gagal."""
        # PostgREST root endpoint return OpenAPI JSON kalau OK
        self._request("GET", "", params={"select": "id"})
        return True

    # ------------------------------------------------------------------
    # Master desain stiker (replace DATABASE_STIKER sheet)
    # ------------------------------------------------------------------
    def fetch_database_stiker(self, use_cache: bool = True) -> list[dict[str, Any]]:
        """Fetch master SKU induk stiker + inventory aggregate.

        Return list of dict:
            {
                "id": uuid,
                "sku": str,
                "name": str,
                "item_code": str,
                "stok": int (sum inventory.current_stock semua lokasi),
                "stok_by_location": {location_id: pcs, ...},
                "pcs_per_lembar": int,
                "safety_stock": int,
                "reorder_point": int,
                "kategori_stok": "STOK" | "NON-STOK",
            }
        """
        now = time.time()
        if use_cache and self._stiker_cache is not None and (now - self._stiker_cache_at) < CACHE_TTL_SECONDS:
            return self._stiker_cache

        # PostgREST embedded select — sekali round-trip ambil items + inventory + attributes
        params = {
            "sub_category_id": f"eq.{STIKER_SUB_CATEGORY_ID}",
            "parent_item_id": "is.null",
            "is_deleted": "eq.false",
            "select": (
                "id,sku,name,item_code,"
                "inventory(current_stock,location_id),"
                "stiker_design_attributes(pcs_per_lembar,safety_stock,reorder_point,kategori_stok)"
            ),
            "order": "sku.asc",
        }
        rows = self._request("GET", "items", params=params) or []

        result: list[dict[str, Any]] = []
        for row in rows:
            inv_list = row.get("inventory") or []
            stok_by_loc: dict[str, int] = {}
            for inv in inv_list:
                loc_id = inv.get("location_id")
                qty = int(inv.get("current_stock") or 0)
                if loc_id:
                    stok_by_loc[loc_id] = stok_by_loc.get(loc_id, 0) + qty
            total_stok = sum(stok_by_loc.values())

            attrs = row.get("stiker_design_attributes")
            if isinstance(attrs, list):
                attrs = attrs[0] if attrs else None
            attrs = attrs or {}

            result.append({
                "id": row.get("id"),
                "sku": (row.get("sku") or "").strip(),
                "name": row.get("name") or "",
                "item_code": row.get("item_code") or "",
                "stok": total_stok,
                "stok_by_location": stok_by_loc,
                "pcs_per_lembar": int(attrs.get("pcs_per_lembar") or 100),
                "safety_stock": int(attrs.get("safety_stock") or 0),
                "reorder_point": int(attrs.get("reorder_point") or 0),
                "kategori_stok": (attrs.get("kategori_stok") or "STOK"),
            })

        self._stiker_cache = result
        self._stiker_cache_at = now
        return result

    def invalidate_stiker_cache(self) -> None:
        self._stiker_cache = None
        self._stiker_cache_at = 0.0

    def get_stock_dict(self, use_cache: bool = True) -> dict[str, int]:
        """Convenience: return {sku: total_stok_pcs} untuk semua SKU stiker.

        Format yang sama dengan `stock_dict` di app.py:Tab 3/4. Drop-in replacement.
        """
        items = self.fetch_database_stiker(use_cache=use_cache)
        return {it["sku"]: it["stok"] for it in items if it["sku"]}

    def get_item_id_by_sku(self, sku: str) -> str | None:
        """Lookup item_id by SKU dari cache (atau fetch kalau cache empty)."""
        items = self.fetch_database_stiker(use_cache=True)
        for it in items:
            if it["sku"] == sku:
                return it["id"]
        return None

    # ------------------------------------------------------------------
    # Pengeluaran stok (replace LOG_KELUAR sheet)
    # ------------------------------------------------------------------
    def issue_goods(self, item_id: str, quantity: int,
                    nomor_resi: str | None = None,
                    location_id: str | None = None,
                    batch_code: str | None = None,
                    extra_notes: str | None = None,
                    date_iso: str | None = None) -> dict[str, Any]:
        """Insert ke goods_issued. Trigger DB auto-decrement inventory.current_stock.

        Return inserted row (dict).
        """
        if not item_id:
            raise ERPClientError("item_id wajib.")
        if quantity <= 0:
            raise ERPClientError("quantity harus > 0.")
        loc_id = location_id or self.default_location_id
        if not loc_id:
            raise ERPClientError(
                "location_id tidak di-set dan default_location_id juga kosong. "
                "Set erp_location_id di config.json (UUID 'Gudang Stiker Siap Jual')."
            )

        note_parts: list[str] = []
        if nomor_resi:
            note_parts.append(f"Resi: {nomor_resi}")
        if batch_code:
            note_parts.append(f"Batch: {batch_code}")
        if extra_notes:
            note_parts.append(extra_notes)
        notes = " | ".join(note_parts) or None

        body = {
            "item_id": item_id,
            "location_id": loc_id,
            "date": date_iso or datetime.now().strftime("%Y-%m-%d"),
            "quantity": int(quantity),
            "status": "Completed",
            "notes": notes,
        }
        # Prefer=return=representation supaya dapat row hasil insert
        rows = self._request("POST", "goods_issued", body=body, prefer="return=representation")
        # Invalidate cache karena stok berubah
        self.invalidate_stiker_cache()
        return (rows or [{}])[0] if isinstance(rows, list) else (rows or {})

    def issue_goods_batch(self, items: list[dict[str, Any]]) -> int:
        """Batch insert ke goods_issued — efisien untuk Tab 3 yang punya banyak resi.

        Each item: {item_id, quantity, nomor_resi, ...optional}
        Return: jumlah row yang berhasil insert.
        """
        if not items:
            return 0
        body = []
        for it in items:
            loc_id = it.get("location_id") or self.default_location_id
            if not loc_id:
                raise ERPClientError("location_id wajib (atau set default_location_id).")
            note_parts: list[str] = []
            if it.get("nomor_resi"):
                note_parts.append(f"Resi: {it['nomor_resi']}")
            if it.get("batch_code"):
                note_parts.append(f"Batch: {it['batch_code']}")
            if it.get("extra_notes"):
                note_parts.append(it["extra_notes"])
            body.append({
                "item_id": it["item_id"],
                "location_id": loc_id,
                "date": it.get("date_iso") or datetime.now().strftime("%Y-%m-%d"),
                "quantity": int(it["quantity"]),
                "status": "Completed",
                "notes": " | ".join(note_parts) or None,
            })
        rows = self._request("POST", "goods_issued", body=body, prefer="return=representation")
        self.invalidate_stiker_cache()
        return len(rows) if isinstance(rows, list) else 0

    # ------------------------------------------------------------------
    # LIST_PESANAN — stiker_orders (untuk QC station + scanner)
    # ------------------------------------------------------------------
    def fetch_list_pesanan(self, batch_id: str | None = None,
                           today_only: bool = False,
                           limit: int = 5000) -> list[dict[str, Any]]:
        """Fetch stiker_orders dengan optional filter batch_id atau today only.

        Return list of dict serupa baris sheet LIST_PESANAN:
            id, nomor_resi, sku_varian, jumlah_pack, jumlah_pcs, marketplace,
            qc_status, qc_operator_id, qc_completed_at, qc_notes,
            stok_action, kekurangan_pcs, batch_id, batch_code, item: {...}
        """
        params: dict[str, Any] = {
            "select": (
                "id,batch_id,nomor_resi,sku_varian,matched_item_id,jumlah_pack,jumlah_pcs,"
                "marketplace,qc_status,qc_operator_id,qc_completed_at,qc_notes,"
                "stok_action,kekurangan_pcs,created_at,"
                "batch:stiker_order_batches!inner(batch_code,uploaded_at),"
                "item:items(name,sku,item_code)"
            ),
            "order": "created_at.desc",
            "limit": str(limit),
        }
        if batch_id:
            params["batch_id"] = f"eq.{batch_id}"
        elif today_only:
            today_iso = datetime.now().strftime("%Y-%m-%dT00:00:00")
            params["created_at"] = f"gte.{today_iso}"
        return self._request("GET", "stiker_orders", params=params) or []

    def update_qc_status(self, order_id: str, status: str,
                         operator_user_id: str | None = None,
                         notes: str = "") -> dict[str, Any]:
        """Update kolom qc_* di stiker_orders (PATCH single row).

        status: 'pending' | 'qc_approved' | 'qc_rejected'
        """
        if not order_id:
            raise ERPClientError("order_id wajib.")
        if status not in ("pending", "qc_approved", "qc_rejected", "in_progress"):
            raise ERPClientError(f"qc_status tidak valid: {status!r}.")
        body: dict[str, Any] = {
            "qc_status": status,
            "qc_completed_at": datetime.now(timezone.utc).isoformat(),
            "qc_notes": notes or None,
        }
        if operator_user_id:
            body["qc_operator_id"] = operator_user_id
        params = {"id": f"eq.{order_id}"}
        rows = self._request("PATCH", "stiker_orders", params=params, body=body,
                             prefer="return=representation")
        return (rows or [{}])[0] if isinstance(rows, list) else (rows or {})

    def find_resi(self, nomor_resi: str) -> list[dict[str, Any]]:
        """Cari semua stiker_orders dengan nomor_resi tertentu (1 resi bisa multi SKU)."""
        if not nomor_resi:
            return []
        params = {
            "nomor_resi": f"eq.{nomor_resi.strip()}",
            "select": (
                "id,batch_id,nomor_resi,sku_varian,matched_item_id,jumlah_pack,jumlah_pcs,"
                "marketplace,qc_status,qc_operator_id,qc_completed_at,qc_notes,"
                "stok_action,kekurangan_pcs,"
                "batch:stiker_order_batches!inner(batch_code),"
                "item:items(name,sku,item_code)"
            ),
            "order": "created_at.desc",
        }
        return self._request("GET", "stiker_orders", params=params) or []

    def update_resi_qc_status(self, nomor_resi: str, status: str,
                              operator_user_id: str | None = None,
                              notes: str = "") -> int:
        """Update qc_status untuk SEMUA row dengan nomor_resi tsb. Return jumlah row updated."""
        if not nomor_resi:
            return 0
        body: dict[str, Any] = {
            "qc_status": status,
            "qc_completed_at": datetime.now(timezone.utc).isoformat(),
            "qc_notes": notes or None,
        }
        if operator_user_id:
            body["qc_operator_id"] = operator_user_id
        params = {"nomor_resi": f"eq.{nomor_resi.strip()}"}
        rows = self._request("PATCH", "stiker_orders", params=params, body=body,
                             prefer="return=representation")
        return len(rows) if isinstance(rows, list) else 0

    # ------------------------------------------------------------------
    # PERMINTAAN_RESTOCK — stiker_restock_requests
    # ------------------------------------------------------------------
    def fetch_restock_requests(self, status_filter: list[str] | None = None,
                               wip_only: bool = False) -> list[dict[str, Any]]:
        """Fetch restock requests. Default: WIP saja (pending/in_progress/menunggu_approval)."""
        params: dict[str, Any] = {
            "select": (
                "id,item_id,sku_raw,jumlah_pcs_request,jml_bundle,status,"
                "tanggal_mulai_print,print_operator_id,jumlah_aktual_gudang,"
                "approved,tanggal_approve,location_id,goods_receipt_id,catatan,created_at,"
                "item:items(sku,name,item_code),"
                "requester:users!stiker_restock_requests_requester_id_fkey(full_name),"
                "print_operator:users!stiker_restock_requests_print_operator_id_fkey(full_name)"
            ),
            "order": "created_at.desc",
        }
        if status_filter:
            quoted = ",".join(s.strip() for s in status_filter if s.strip())
            params["status"] = f"in.({quoted})"
        elif wip_only:
            params["status"] = "in.(pending,in_progress,menunggu_approval)"
        return self._request("GET", "stiker_restock_requests", params=params) or []

    def submit_restock_request(self, item_id: str, jumlah_pcs: int,
                               requester_user_id: str | None = None,
                               sku_raw: str | None = None,
                               catatan: str | None = None) -> dict[str, Any]:
        """Submit permintaan baru (status=pending)."""
        if not item_id:
            raise ERPClientError("item_id wajib.")
        if jumlah_pcs <= 0:
            raise ERPClientError("jumlah_pcs harus > 0.")
        body = {
            "item_id": item_id,
            "sku_raw": sku_raw,
            "jumlah_pcs_request": int(jumlah_pcs),
            "requester_id": requester_user_id,
            "created_by": requester_user_id,
            "catatan": catatan,
            "status": "pending",
        }
        rows = self._request("POST", "stiker_restock_requests", body=body,
                             prefer="return=representation")
        return (rows or [{}])[0] if isinstance(rows, list) else (rows or {})

    def start_restock_production(self, request_id: str,
                                 print_operator_user_id: str | None = None) -> dict[str, Any]:
        """Tim print klik 'Mulai Produksi' (pending → in_progress)."""
        body = {
            "status": "in_progress",
            "tanggal_mulai_print": datetime.now(timezone.utc).isoformat(),
        }
        if print_operator_user_id:
            body["print_operator_id"] = print_operator_user_id
        rows = self._request("PATCH", "stiker_restock_requests",
                             params={"id": f"eq.{request_id}"}, body=body,
                             prefer="return=representation")
        return (rows or [{}])[0] if isinstance(rows, list) else (rows or {})

    def finish_restock_production(self, request_id: str) -> dict[str, Any]:
        """Tim print klik 'Selesai Produksi' (in_progress → menunggu_approval)."""
        rows = self._request("PATCH", "stiker_restock_requests",
                             params={"id": f"eq.{request_id}"},
                             body={"status": "menunggu_approval"},
                             prefer="return=representation")
        return (rows or [{}])[0] if isinstance(rows, list) else (rows or {})

    def delete_restock_request(self, request_id: str) -> bool:
        """Hapus permintaan (precondition: status='pending' — di-enforce di app side)."""
        self._request("DELETE", "stiker_restock_requests",
                      params={"id": f"eq.{request_id}"})
        return True

    # ------------------------------------------------------------------
    # Lokasi resolver
    # ------------------------------------------------------------------
    def resolve_location_id(self, name: str) -> str | None:
        """Lookup location_id by name (case-insensitive). Cache 1 jam."""
        if self._location_cache is None:
            rows = self._request("GET", "locations",
                                 params={"is_active": "eq.true", "select": "id,name"}) or []
            self._location_cache = {
                (r.get("name") or "").strip().lower(): r.get("id")
                for r in rows
                if r.get("id")
            }
        return self._location_cache.get(name.strip().lower())


# ----------------------------------------------------------------------
# JSON serializer helper (handle datetime objects)
# ----------------------------------------------------------------------
def _json_default(o: Any) -> str:
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")
