"""Dataclass return types untuk function inti."""
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class BufferLocation:
    """Posisi slot di buffer."""
    buffer_slot_id: int
    wadah_id: int
    wadah_nomor: int
    slot_number: int
    sku: Optional[str]
    plastik_count: int
    is_overflow_of: Optional[int] = None

    def label(self) -> str:
        if self.is_overflow_of:
            return f"WADAH {self.wadah_nomor} SLOT {self.slot_number} (overflow)"
        return f"WADAH {self.wadah_nomor} SLOT {self.slot_number}"


@dataclass
class Wadah:
    id: int
    nomor: int
    capacity: int
    is_active: bool


@dataclass
class ScanResult:
    action: str  # 'place_in_slot_aktif' | 'place_in_buffer_existing' | 'place_in_buffer_new'
    target_label: str  # legacy full string — kept for backwards compat / logs
    barcode: str
    sku: str
    varian: int
    target_slot_aktif_number: Optional[int] = None
    target_resi_id: Optional[int] = None
    target_resi_nomor: Optional[str] = None
    target_buffer_slot_id: Optional[int] = None
    existing_plastik_count: Optional[int] = None
    extra: dict = field(default_factory=dict)
    # Structured target untuk visual hierarchy di scan_result UI:
    #   prefix = "LETAKKAN KE" (kecil)
    #   main   = "WADAH 1 SLOT 8" / "SLOT 2" (BIG, attention-grabbing)
    #   suffix = "(slot baru)" / "(sudah berisi 7 bundle)" / "(RESI XXX)" (kecil)
    target_prefix: Optional[str] = None
    target_main: Optional[str] = None
    target_suffix: Optional[str] = None


@dataclass
class HarvesterPickupResult:
    task_id: int
    barcode: str
    sku: str
    buffer_slot_id: int
    buffer_label: str
    target_slot_aktif_number: int
    target_resi_nomor: str


@dataclass
class HarvesterDropoffResult:
    task_id: int
    barcode: str
    target_slot_aktif_number: int
    target_resi_nomor: str
    resi_completed: bool


@dataclass
class HarvesterTaskRow:
    id: int
    buffer_slot_id: int
    buffer_label: str
    target_resi_id: int
    target_resi_nomor: str
    target_slot_aktif_number: int
    sku: str
    status: str
    created_at: str
    started_at: Optional[str] = None


@dataclass
class SetupResult:
    resi_id: int
    nomor_resi: str
    slot_number: int
    harvester_tasks_created: List[int] = field(default_factory=list)
    buffer_pickups: List[dict] = field(default_factory=list)


@dataclass
class ImportResult:
    batch_id: str
    waves_created: int
    resis_imported: int
    items_imported: int
    first_wave_id: int
    setup_results: List[SetupResult] = field(default_factory=list)


@dataclass
class SlotStatus:
    slot_number: int
    resi_id: Optional[int]
    nomor_resi: Optional[str]
    status: str  # 'kosong', 'merah', 'hijau', 'kuning'
    missing_skus: List[dict] = field(default_factory=list)
    completed_at: Optional[str] = None
    minutes_waiting: Optional[int] = None


@dataclass
class BufferStatus:
    total_wadah_aktif: int
    total_slot: int
    slot_terpakai: int
    slot_kosong: int
    breakdown: List[dict] = field(default_factory=list)


@dataclass
class AgingItem:
    buffer_slot_id: int
    wadah_nomor: int
    slot_number: int
    sku: str
    plastik_count: int
    first_plastik_at: str
    age_hours: float


@dataclass
class UndoResult:
    event_id: int
    action_undone: str
    detail: str
