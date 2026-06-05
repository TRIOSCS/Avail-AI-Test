"""Truth-table tests for the OEM/FRU vendor classifier (real not_found samples)."""

import pytest

from app.services.enrichment_worker.oem_classifier import classify_oem_vendor


@pytest.mark.parametrize(
    "mpn,vendor",
    [
        ("01HW917", "lenovo"),
        ("00E2891", "lenovo"),
        ("01LV731", "lenovo"),
        ("00HW132", "lenovo"),
        ("38L7669", "lenovo"),
        ("46C9040", "lenovo"),
        ("5B20L64949", "lenovo"),
        ("5C10Q59981", "lenovo"),
        ("5T10Q96500", "lenovo"),
        ("918042-601", "hpe"),
        ("619559-001", "hpe"),
        ("486301-001", "hpe"),
        ("628668-001", "hpe"),
        ("902499-856", "hpe"),
        ("NB.MBC11.003", "acer"),
        ("KT.00403.025", "acer"),
        ("33.G55N7.002", "acer"),
        ("NB.GKH11.002", "acer"),
        ("60NB0690-MB1820", "asus"),
        ("0B200-00930000", "asus"),
        ("HV52W", "dell"),
        ("66YYK", "dell"),
    ],
)
def test_classifies_known_oem_codes(mpn, vendor):
    assert classify_oem_vendor(mpn) == vendor


@pytest.mark.parametrize(
    "mpn",
    [
        "M393A2K40EB3-CWEB/C",  # real Samsung DDR4 RDIMM
        "LM2596S",
        "ATMEGA328P-PU",
        "STM32F407VGT6",
        "",
        "  ",
        None,
        "AB",  # too short / empty
    ],
)
def test_rejects_non_oem(mpn):
    assert classify_oem_vendor(mpn) is None


def test_case_insensitive_and_stripped():
    assert classify_oem_vendor("  0b200-00930000  ") == "asus"
    assert classify_oem_vendor("nb.mbc11.003") == "acer"


def test_dell_pattern_is_broad_known_tradeoff():
    """The Dell 5-char alnum-with-letter pattern is deliberately broad: a generic part like
    LM317 classifies as 'dell' (accepted false positive — it only costs a wasted web call,
    and a Dell miss terminates not_found, not not_catalogued). This pins the breadth so any
    future narrowing is a deliberate change, not an accident."""
    assert classify_oem_vendor("LM317") == "dell"
