"""
test_qc_parser.py
Unit test untuk parser SKU & marketplace di qc_stasiun.

Run dengan:
    python -m unittest test_qc_parser.py -v
"""

import unittest

from qc_stasiun import parse_sku, calculate_packs_needed, detect_marketplace


class TestParseSku(unittest.TestCase):
    def test_simple_format(self):
        # Format simple: id-NNpcs
        self.assertEqual(parse_sku("445-10Pcs"), ("445", 10))
        self.assertEqual(parse_sku("445-50Pcs"), ("445", 50))

    def test_with_design_name(self):
        # Format real BigSeller: id-NAMA-NNpcs (id selalu di awal)
        self.assertEqual(parse_sku("431-RETRO-10PCS"), ("431", 10))
        self.assertEqual(parse_sku("501-HAWAI-10PCS"), ("501", 10))
        # SKU yang TIDAK diawali angka di-treat sebagai non-stiker (numeric_id=None),
        # tapi pcs multiplier tetap di-extract kalau ada
        self.assertEqual(parse_sku("ABC123-100Pcs"), (None, 100))

    def test_with_trailing_whitespace(self):
        # Real data ada trailing newline/space
        self.assertEqual(parse_sku("1446-20pcs\n"), ("1446", 20))
        self.assertEqual(parse_sku(" 445-10Pcs "), ("445", 10))
        self.assertEqual(parse_sku("1532-20pcs\n"), ("1532", 20))

    def test_case_insensitive_pcs(self):
        self.assertEqual(parse_sku("445-10PCS"), ("445", 10))
        self.assertEqual(parse_sku("445-10pcs"), ("445", 10))
        self.assertEqual(parse_sku("445-10Pcs"), ("445", 10))

    def test_non_stiker_returns_none_id(self):
        # Item non-stiker tanpa numeric prefix
        self.assertEqual(parse_sku("BANNER-A3"), (None, 1))
        self.assertEqual(parse_sku("STAMP-ABC"), (None, 1))

    def test_empty_or_none(self):
        self.assertEqual(parse_sku(""), (None, 1))
        self.assertEqual(parse_sku(None), (None, 1))
        self.assertEqual(parse_sku("   "), (None, 1))

    def test_id_only(self):
        # Tanpa pcs suffix, multiplier default 1
        self.assertEqual(parse_sku("445"), ("445", 1))


class TestCalculatePacksNeeded(unittest.TestCase):
    def test_basic_10pcs(self):
        # 1 paket × 10pcs = 10 pcs = 1 pack
        self.assertEqual(calculate_packs_needed(10, 1), 1)

    def test_20pcs_paket(self):
        # 1 paket × 20pcs = 20 pcs = 2 pack
        self.assertEqual(calculate_packs_needed(20, 1), 2)

    def test_50pcs_paket(self):
        self.assertEqual(calculate_packs_needed(50, 1), 5)

    def test_100pcs_paket(self):
        self.assertEqual(calculate_packs_needed(100, 1), 10)

    def test_multi_qty(self):
        # 2 paket × 50pcs = 100 pcs = 10 pack
        self.assertEqual(calculate_packs_needed(50, 2), 10)
        # 3 paket × 20pcs = 60 pcs = 6 pack
        self.assertEqual(calculate_packs_needed(20, 3), 6)

    def test_zero_qty(self):
        self.assertEqual(calculate_packs_needed(10, 0), 0)


class TestDetectMarketplace(unittest.TestCase):
    def test_shopee_express(self):
        self.assertEqual(detect_marketplace("SPXID060155202261"), "Shopee Express")
        self.assertEqual(detect_marketplace("SPX12345"), "Shopee Express")

    def test_shopee(self):
        self.assertEqual(
            detect_marketplace("SHPE260120DE3B70014325"), "Shopee"
        )
        self.assertEqual(detect_marketplace("SHP123456"), "Shopee")

    def test_jnt(self):
        self.assertEqual(detect_marketplace("JNT123456"), "J&T Express")
        self.assertEqual(detect_marketplace("JT987654"), "J&T Express")

    def test_jne(self):
        self.assertEqual(detect_marketplace("JNE12345"), "JNE")

    def test_lowercase_input(self):
        # Function should normalize to upper
        self.assertEqual(detect_marketplace("spxid060155202261"), "Shopee Express")

    def test_unknown(self):
        self.assertEqual(detect_marketplace("XYZ123456"), "Unknown")
        self.assertEqual(detect_marketplace(""), "Unknown")
        self.assertEqual(detect_marketplace(None), "Unknown")

    def test_specific_prefix_priority(self):
        # SPXID lebih spesifik dari SPX → harus dapat 'Shopee Express' (sama hasil)
        # SHPE lebih spesifik dari SHP → harus dapat 'Shopee' (sama hasil)
        # Test ini memastikan tidak ada bug priority kalau kedua mapping ke value berbeda
        self.assertEqual(detect_marketplace("SPXID999"), "Shopee Express")
        self.assertEqual(detect_marketplace("SHPE999"), "Shopee")


if __name__ == "__main__":
    unittest.main(verbosity=2)
