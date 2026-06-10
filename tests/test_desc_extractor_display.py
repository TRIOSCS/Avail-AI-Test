"""Accuracy guard for the display/panel description extractor — REAL corpus strings →
exact specs.

Every description below is verbatim from TRIO's part master
(/root/source_ingest/LSC1__Material__c.csv, Material_Description__c) or the staged
inventory sheets (Firesale_inventory). Expectations are FULL equality — a new key
appearing unexpectedly is as much a failure as a missing one.
"""

import pytest

from app.services.desc_extractor import extract_desc

# (real description, commodity_hint or None, exact expected specs)
CASES = [
    # ── TRIO part-master "<Label>, …" grammar ────────────────────────────
    ('LCD, 21.5", LG', None, {"diagonal_size": 21.5}),
    (
        "PNL,15.6 FHD AG WLED SVA 45% 220neDP,INX",
        None,
        {"resolution": "1920x1080", "diagonal_size": 15.6, "backlight": "LED"},
    ),
    (
        'LCD, 15.6", FHD 1920x1080, LQ156M1JW43, Dell branded, Sharp',  # named + explicit agree
        None,
        {"resolution": "1920x1080", "diagonal_size": 15.6},
    ),
    (
        "HU, FHD AG LED UWVA 13 TS PVCY",  # HP display-unit lead; bare integer 13 — no inch mark
        None,
        {"resolution": "1920x1080", "backlight": "LED"},
    ),
    (
        'Innolux, LCD, 21.5"',  # neutral brand lead; body LCD token routes
        None,
        {"diagonal_size": 21.5},
    ),
    # ── body-token / first-token routing ─────────────────────────────────
    (
        "TFD LCD  TOUCH HU 11.6 HD EDP WLAN DUNES3",
        None,
        {"resolution": "1366x768", "diagonal_size": 11.6},
    ),
    ("SPS-DSPLY HU 17.3 DRM UHD IR", None, {"resolution": "3840x2160"}),  # bare 17.3 missed
    (
        "Lenovo 300E Chromebook Lcd Touch Screen w Bezel 11.6 HD 1366x768 5D10Q93993",
        None,
        {"resolution": "1366x768", "diagonal_size": 11.6},
    ),
    ("HP 22kd 21.5-IN Display-EMEA", None, {"diagonal_size": 21.5}),
    ("DISPLAY TIO27-Monitor(27inch)", None, {"diagonal_size": 27}),
    (
        "LCD LED 15.6W WXGA GLARE AUO B156XTK01.0 LF 200NIT 8MS 500:1 (ULTRA-SLIM) (EDP) OTP LITE",
        None,
        {"backlight": "LED"},  # 15.6W = wide (no inch mark); WXGA deliberately unmapped
    ),
    # ── hint-routed grammar without any commodity token ──────────────────
    (
        "19.5 WVA,AG,WLED,250,RGB,NZBD,INX",  # RGB is the color interface — never "LED RGB"
        "displays",
        {"backlight": "LED"},
    ),
    # ── deliberate misses (conservative > wrong) ─────────────────────────
    ("ZB555KL-4A 5.5 HD+LCD MODULE", None, {}),  # HD+ excluded; 5.5 below the 7" floor
]


@pytest.mark.parametrize("description,hint,expected", CASES)
def test_display_extract_exact(description, hint, expected):
    result = extract_desc(description, commodity_hint=hint)
    assert result is not None, f"{description!r} did not extract"
    assert result.commodity == "displays"
    assert result.specs == expected
    assert result.confidence == 0.90


def test_backlight_is_always_the_generic_led_bucket():
    # WLED (white) and bare LED collapse to the generic seeded "LED" member — the
    # seeded "LED White"/"LED RGB" members are never emitted from descriptions
    # (white-vs-RGB is not expressible in TRIO desc grammar).
    for desc in ("PNL,15.6 FHD AG WLED SVA 45% 220neDP,INX", "HU, FHD AG LED UWVA 13 TS PVCY"):
        result = extract_desc(desc)
        assert result is not None
        assert result.specs["backlight"] == "LED"
