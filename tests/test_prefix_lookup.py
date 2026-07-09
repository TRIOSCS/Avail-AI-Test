"""tests/test_prefix_lookup.py — Tests for app/services/prefix_lookup.py.

Covers lookup_manufacturer_by_prefix: prefix matching, longest-first resolution,
2-char prefix skip, case-insensitive input, and no-match cases.

Called by: pytest
Depends on: app.services.prefix_lookup (pure logic, no DB/API)
"""

import os

os.environ["TESTING"] = "1"

from app.services.prefix_lookup import PREFIX_TABLE, lookup_manufacturer_by_prefix


class TestLookupManufacturerByPrefix:
    def test_known_three_char_prefix_returns_manufacturer(self):
        mfr, conf = lookup_manufacturer_by_prefix("TPS62840")
        assert mfr == "Texas Instruments"
        assert conf == 0.9

    def test_confidence_is_always_0_9_on_match(self):
        _, conf = lookup_manufacturer_by_prefix("NRF52840")
        assert conf == 0.9

    def test_no_match_returns_none_and_zero(self):
        mfr, conf = lookup_manufacturer_by_prefix("XYZ99999")
        assert mfr is None
        assert conf == 0.0

    def test_empty_string_returns_no_match(self):
        mfr, conf = lookup_manufacturer_by_prefix("")
        assert mfr is None
        assert conf == 0.0

    def test_lowercase_input_is_uppercased(self):
        mfr_lower, _ = lookup_manufacturer_by_prefix("tps62840")
        mfr_upper, _ = lookup_manufacturer_by_prefix("TPS62840")
        assert mfr_lower == mfr_upper == "Texas Instruments"

    def test_mixed_case_input(self):
        mfr, conf = lookup_manufacturer_by_prefix("Esp32Wroom")
        assert mfr == "Espressif Systems"
        assert conf == 0.9

    def test_longest_prefix_wins_over_shorter(self):
        # "STM32F407" should match "STM32" (5 chars) not "STM" (3 chars)
        mfr, _ = lookup_manufacturer_by_prefix("STM32F407")
        assert mfr == "STMicroelectronics"

    def test_stm8_matches_before_stm(self):
        # "STM8S003" should match "STM8" (4 chars) not "STM" (3 chars)
        mfr, _ = lookup_manufacturer_by_prefix("STM8S003")
        assert mfr == "STMicroelectronics"

    def test_sn7_matches_before_two_char_sn(self):
        # "SN7400" → "SN7" is 3 chars, wins over "SN" (2 chars)
        mfr, _ = lookup_manufacturer_by_prefix("SN7400")
        assert mfr == "Texas Instruments"

    def test_two_char_prefix_alone_is_not_returned(self):
        # "AD1234" → "ADS"/"ADM"/"ADP"/"ADG" don't match; "AD" (2 chars) → skipped
        mfr, conf = lookup_manufacturer_by_prefix("AD1234")
        assert mfr is None
        assert conf == 0.0

    def test_two_char_lm_prefix_skipped(self):
        # "LM317T" → "LMR" doesn't match; "LM" (2 chars) → skipped
        mfr, conf = lookup_manufacturer_by_prefix("LM317T")
        assert mfr is None
        assert conf == 0.0

    def test_lmr_three_char_prefix_matches(self):
        mfr, _ = lookup_manufacturer_by_prefix("LMR14006")
        assert mfr == "Texas Instruments"

    def test_nxp_lpc_prefix(self):
        mfr, _ = lookup_manufacturer_by_prefix("LPC1768")
        assert mfr == "NXP Semiconductors"

    def test_nordic_nrf_prefix(self):
        mfr, _ = lookup_manufacturer_by_prefix("NRF52840")
        assert mfr == "Nordic Semiconductor"

    def test_espressif_esp_prefix(self):
        mfr, _ = lookup_manufacturer_by_prefix("ESP8266")
        assert mfr == "Espressif Systems"

    def test_microchip_pic_prefix(self):
        mfr, _ = lookup_manufacturer_by_prefix("PIC16F877A")
        assert mfr == "Microchip Technology"

    def test_microchip_pic32_before_pic(self):
        # "PIC32MZ" → "PIC32" (5 chars) wins over "PIC" (3 chars)
        mfr, _ = lookup_manufacturer_by_prefix("PIC32MZ2048EFH144")
        assert mfr == "Microchip Technology"

    def test_gd25_prefix_gigadevice(self):
        mfr, _ = lookup_manufacturer_by_prefix("GD25Q128CSIG")
        assert mfr == "GigaDevice"

    def test_gd32_prefix_gigadevice(self):
        mfr, _ = lookup_manufacturer_by_prefix("GD32F103CBT6")
        assert mfr == "GigaDevice"

    def test_analog_devices_ltc(self):
        mfr, _ = lookup_manufacturer_by_prefix("LTC3780")
        assert mfr == "Analog Devices"

    def test_max3_prefix_before_max(self):
        # "MAX3232" → "MAX3" (4 chars) wins over "MAX" (3 chars)
        mfr, _ = lookup_manufacturer_by_prefix("MAX3232")
        assert mfr == "Analog Devices"

    def test_stm_prefix_st_microelectronics(self):
        mfr, _ = lookup_manufacturer_by_prefix("STMPE811")
        assert mfr == "STMicroelectronics"

    def test_infineon_irf_prefix(self):
        mfr, _ = lookup_manufacturer_by_prefix("IRF540N")
        assert mfr == "Infineon Technologies"

    def test_onsemi_ncp_prefix(self):
        mfr, _ = lookup_manufacturer_by_prefix("NCP1529")
        assert mfr == "onsemi"

    def test_renesas_r5f_prefix(self):
        mfr, _ = lookup_manufacturer_by_prefix("R5F100LEA")
        assert mfr == "Renesas Electronics"

    def test_murata_grm_prefix(self):
        mfr, _ = lookup_manufacturer_by_prefix("GRM188R71H104KA93")
        assert mfr == "Murata"

    def test_samsung_k4_two_char_skipped(self):
        # "K4" is 2 chars — intentionally skipped as too ambiguous
        mfr, conf = lookup_manufacturer_by_prefix("K4B8G1646E")
        assert mfr is None
        assert conf == 0.0

    def test_xilinx_xcku_four_char_matches(self):
        # "XCKU" is 4 chars — matches; "XC" (2 chars) would be skipped
        mfr, _ = lookup_manufacturer_by_prefix("XCKU040FFVA1156I")
        assert mfr == "Xilinx"

    def test_xilinx_xcvu_four_char_matches(self):
        mfr, _ = lookup_manufacturer_by_prefix("XCVU9P-2FLGB2104E")
        assert mfr == "Xilinx"

    def test_xilinx_xc_two_char_skipped(self):
        # "XC" is 2 chars — skipped; XC7 Artix series won't match
        mfr, conf = lookup_manufacturer_by_prefix("XC7A35T")
        assert mfr is None
        assert conf == 0.0

    def test_broadcom_bcm_prefix(self):
        mfr, _ = lookup_manufacturer_by_prefix("BCM2711")
        assert mfr == "Broadcom"

    def test_mp_two_char_skipped(self):
        # "MP" is 2 chars — intentionally skipped as too ambiguous
        mfr, conf = lookup_manufacturer_by_prefix("MP2315")
        assert mfr is None
        assert conf == 0.0

    def test_short_mpn_two_chars_no_match(self):
        # A 2-char MPN that hits a 2-char prefix entry is skipped
        mfr, conf = lookup_manufacturer_by_prefix("SN")
        assert mfr is None
        assert conf == 0.0

    def test_single_char_no_match(self):
        mfr, conf = lookup_manufacturer_by_prefix("T")
        assert mfr is None
        assert conf == 0.0


class TestPrefixTable:
    def test_prefix_table_is_non_empty(self):
        assert len(PREFIX_TABLE) > 50

    def test_all_values_are_strings(self):
        for prefix, manufacturer in PREFIX_TABLE.items():
            assert isinstance(prefix, str), f"Key {prefix!r} is not a string"
            assert isinstance(manufacturer, str), f"Value for {prefix!r} is not a string"
            assert manufacturer, f"Empty manufacturer for prefix {prefix!r}"

    def test_no_empty_prefix_keys(self):
        for prefix in PREFIX_TABLE:
            assert prefix, "Empty prefix key found in PREFIX_TABLE"
