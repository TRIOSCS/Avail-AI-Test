"""Accuracy guard for the motherboard description extractor — REAL corpus strings →
exact specs.

Every description below is verbatim from TRIO's part master
(/root/source_ingest/LSC1__Material__c.csv, Material_Description__c). Expectations are
FULL equality. The mb route has no GB key at all, so laptop-board dram/gpu tokens
("…GTX1050 4GB i7…") are inert by construction.
"""

import pytest

from app.services.desc_extractor import extract_desc

# (real description, commodity_hint or None, exact expected specs)
CASES = [
    # ── TRIO part-master "<Label>, …" grammar ────────────────────────────
    ("MB, B82CD NOK A49120C UMA 4G32G", None, {"board_type": "System Board"}),
    ("MB, L 80YN, WIN, i-7-7820 HK 8 G - Lenovo", None, {"board_type": "System Board"}),
    (
        "MB, Motherboard, Cel 1007U y-TPM, W8p for ThinkPad X131e, Lenovo",
        None,
        {"board_type": "System Board"},
    ),
    (
        "BDPLANAR WIN,i5-10210U,16G,9560,yTPM2",  # "BDPLANAR WIN" lead-map rescue
        None,
        {"board_type": "System Board"},
    ),
    (
        "SUPERMICRO FRU,DAUGHTER CARD REPLACEMENT KIT",  # brand+packaging lead is neutral
        "motherboards",
        {"board_type": "Daughter Board"},
    ),
    # ── body-token / first-token routing ─────────────────────────────────
    ("BDPLANAR Lenovo MB ALC WIN R7-5700U UMA", None, {"board_type": "System Board"}),
    ("5B20T04908:MB WHL I7 DIS 4G 8G WIN", None, {"board_type": "System Board"}),
    (
        "SPS-MB DSC GTX1050 4GB i7-7700HQ WIN",  # stays a motherboard: no GB, no gpu_family
        None,
        {"board_type": "System Board"},
    ),
    # ── hint-routed grammar without a routing token ──────────────────────
    ("SPS - BD SYS SL390/SL2x390", "motherboards", {"board_type": "System Board"}),
    # ── conflicts and mis-filed MB-bucket rows ───────────────────────────
    ("MB, 7063-CR1 System backplane kit", None, {}),  # MB×Backplane conflict — it IS a kit
    ("Function Board C 82L7 IO", "motherboards", {}),
    ("TOUCHPAD", "motherboards", {}),
    ("HS 2TB 3.5", "motherboards", {}),  # drive-ish text on an mb card — no board token, no GB key
]


@pytest.mark.parametrize("description,hint,expected", CASES)
def test_board_extract_exact(description, hint, expected):
    result = extract_desc(description, commodity_hint=hint)
    assert result is not None, f"{description!r} did not extract"
    assert result.commodity == "motherboards"
    assert result.specs == expected
    assert result.confidence == 0.90


def test_spaced_megabytes_never_read_as_a_board_token():
    # "16 MB" (spaced megabytes — real corpus drive string) is killed by the MB
    # lookbehind; glued "512MB"/"36MB" never had a boundary. Neither may emit a
    # System Board. extract_board expects upper-cased, whitespace-collapsed text.
    from app.services.desc_extractor.board import extract_board

    assert extract_board("HD 500GB 7200RPM CACHE 16 MB SATA 6.0GB/S.") == {}
    assert extract_board('HDD, 450GB 15000RPM 16MB 3.5" SAS, N-SERIES') == {}
