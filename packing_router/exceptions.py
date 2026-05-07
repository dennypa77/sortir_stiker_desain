"""Custom exceptions untuk packing_router."""


class PackingRouterError(Exception):
    """Base exception."""


class BufferFullError(PackingRouterError):
    """Tidak ada slot kosong di seluruh wadah aktif."""


class HarvesterMismatchError(PackingRouterError):
    """Validasi double-scan harvester gagal."""


class BarcodeFormatError(PackingRouterError):
    """Format barcode tidak sesuai skema."""


class ResiNotFoundError(PackingRouterError):
    """Resi tidak ditemukan di DB."""


class WaveTransitionError(PackingRouterError):
    """Gagal transisi wave (mis. wave berikutnya tidak ada)."""


class UndoWindowExpiredError(PackingRouterError):
    """Tidak ada scan dalam window undo."""


class SlotAktifConflictError(PackingRouterError):
    """Slot aktif sudah terisi resi lain."""


class WadahConflictError(PackingRouterError):
    """Wadah tidak bisa dihapus karena masih ada plastik."""
