"""Test parse_sku, parse_barcode, packs_needed."""
import pytest

from packing_router.exceptions import BarcodeFormatError
from packing_router.utils import (
    derive_sku_full,
    packs_needed,
    parse_barcode,
    parse_sku,
)


class TestParseSKU:
    def test_with_pcs(self):
        assert parse_sku("1446-RETRO-10pcs") == ("1446", 10)

    def test_uppercase_pcs(self):
        assert parse_sku("431-DESIGN-50PCS") == ("431", 50)

    def test_no_pcs_defaults_to_one(self):
        assert parse_sku("1234-NoVariantTag") == ("1234", 1)

    def test_no_numeric_id(self):
        assert parse_sku("ABC-XYZ-10pcs") == (None, 10)

    def test_strips_whitespace(self):
        assert parse_sku("  431-A-20pcs  ") == ("431", 20)


class TestParseBarcode:
    def test_numeric_only_short(self):
        assert parse_barcode("445") == "445"

    def test_numeric_only_long(self):
        assert parse_barcode("1446") == "1446"

    def test_numeric_with_whitespace(self):
        assert parse_barcode("  1446  ") == "1446"

    def test_full_format_returns_id(self):
        assert parse_barcode("1446-10PCS-0001") == "1446"

    def test_full_format_lowercase(self):
        assert parse_barcode("1446-10pcs-9999") == "1446"

    def test_marketplace_resi_rejected(self):
        # Resi marketplace bukan barcode plastik
        with pytest.raises(BarcodeFormatError):
            parse_barcode("SPXID060408319585")

    def test_alphanumeric_rejected(self):
        with pytest.raises(BarcodeFormatError):
            parse_barcode("ABC123")

    def test_empty(self):
        with pytest.raises(BarcodeFormatError):
            parse_barcode("")


class TestPacksNeeded:
    def test_varian_10_jumlah_1(self):
        assert packs_needed(1, 10) == 1

    def test_varian_20_jumlah_1(self):
        assert packs_needed(1, 20) == 2

    def test_varian_50_jumlah_1(self):
        assert packs_needed(1, 50) == 5

    def test_varian_100_jumlah_1(self):
        assert packs_needed(1, 100) == 10

    def test_varian_50_jumlah_2(self):
        assert packs_needed(2, 50) == 10

    def test_zero_jumlah(self):
        assert packs_needed(0, 50) == 0

    def test_zero_varian_fallback(self):
        # Fallback: kalau varian unknown, return jumlah saja
        assert packs_needed(3, 0) == 3


def test_derive_sku_full():
    assert derive_sku_full("1446", 10) == "1446-10PCS"
