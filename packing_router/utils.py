"""Pure helpers: SKU parsing, barcode parsing.

Logic regex `parse_sku` adalah port dari `BotApp.extract_numeric_id_and_pcs`
di app.py:533 (pure-function version, decoupled dari Tkinter class).
"""
import re
from typing import Optional, Tuple

from .exceptions import BarcodeFormatError


def packs_needed(jumlah: int, varian: int) -> int:
    """Hitung jumlah pack 10pcs yang dibutuhkan untuk fulfill 1 baris pesanan.

    Standar HOG: semua stok disimpan dalam pack 10pcs.
    - varian 10pcs × jumlah 1 → 1 pack
    - varian 20pcs × jumlah 1 → 2 pack
    - varian 50pcs × jumlah 1 → 5 pack
    - varian 100pcs × jumlah 1 → 10 pack
    - varian 50pcs × jumlah 2 → 10 pack
    """
    jumlah = int(jumlah or 0)
    varian = int(varian or 0)
    if jumlah <= 0:
        return 0
    if varian <= 0:
        return jumlah  # fallback: 1 plastik per jumlah
    pack_per_unit = max(1, varian // 10)
    return jumlah * pack_per_unit


_SKU_ID_RE = re.compile(r"^\d+")
_SKU_PCS_RE = re.compile(r"(\d+)pcs", re.IGNORECASE)


def parse_sku(sku: str) -> Tuple[Optional[str], int]:
    """Return (numeric_id, pcs_per_paket).

    Default pcs = 1 jika tidak ada substring "<n>pcs" di SKU
    (matches behavior of app.py:533).
    """
    sku = sku.strip()
    id_match = _SKU_ID_RE.match(sku)
    numeric_id = id_match.group(0) if id_match else None
    pcs_match = _SKU_PCS_RE.search(sku)
    pcs = int(pcs_match.group(1)) if pcs_match else 1
    return numeric_id, pcs


_BARCODE_FULL_RE = re.compile(r"^([A-Za-z0-9]+)-(\d+)PCS-(\d+)$", re.IGNORECASE)
_BARCODE_NUMERIC_RE = re.compile(r"^\d+$")


def parse_barcode(barcode: str) -> str:
    """Parse barcode plastik output weeding ke ``numeric_id`` (= ID stiker desain).

    Format yang diterima:
    - **Numeric murni**: ``445``, ``1446`` — format utama HOG
    - **Full format**: ``1446-10PCS-0001`` — opsional, kalau HOG rollout barcode
      dengan suffix sequence

    1 plastik = 1 pack 10pcs (standar HOG). Varian (10/20/50/100) bukan
    properti barcode plastik — itu properti SKU di resi.

    Return ``numeric_id`` (string). Raise :class:`BarcodeFormatError` kalau
    tidak match dua pola di atas.
    """
    bc = barcode.strip()
    if _BARCODE_NUMERIC_RE.match(bc):
        return bc
    m = _BARCODE_FULL_RE.match(bc)
    if m:
        return m.group(1)
    raise BarcodeFormatError(
        f"Barcode '{barcode}' bukan numeric (e.g. '445') atau format "
        f"{{ID}}-{{VARIAN}}PCS-{{SEQ}} (e.g. '1446-10PCS-0001')"
    )


def derive_sku_full(sku_base: str, varian: int) -> str:
    """Bentuk SKU full untuk display — e.g. (1446, 10) -> '1446-10PCS'."""
    return f"{sku_base}-{varian}PCS"
