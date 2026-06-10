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
    # ── packaging-suffixed display leads (explicit _LEAD_MAP entries) ────
    ("LCD ASSY, 15, HD, 1MIC 8.1", None, {"resolution": "1366x768"}),
    (
        "PNL KIT, LCD 15.6 FHD UWVA W/BEZEL",
        None,
        {"resolution": "1920x1080", "diagonal_size": 15.6},
    ),
    # ── camera tokens: HD/FHD before CAM/CAMERA/WEBCAM is the camera, not the panel ──
    ("PANEL, W/HD CAMERA", None, {}),
    ("SPS-LCD BEZEL HD WEBCAM LANNISTER", None, {}),  # a bezel, not a panel
    ("SPS-LCD CABLE TS PANEL HD WEBCAM", None, {}),  # a cable, not a panel
    ("CAMERA AIO520 FHD CAM BSN", "displays", {}),
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


def test_bare_spaced_in_is_a_preposition_not_an_inch_mark():
    # "<number> IN <word>" is English ("IN STOCK", "IN RACK") — only quote marks,
    # glued/hyphenated IN, or INCH(ES) count as a diagonal unit.
    for desc in ("PANEL 15 IN STOCK", "CHASSIS 19 IN RACK", "BACKLIGHT 60 IN"):
        result = extract_desc(desc, commodity_hint="displays")
        assert result is not None
        assert result.specs == {}, f"{desc!r} must not emit a diagonal"


def test_n_in_1_dock_grammar_is_not_a_diagonal():
    # "8-IN-1"/"10 IN 1" multiplexer/dock counts are rejected by the trailing-digit
    # lookahead (the 7-86 range alone would pass N>=7).
    for desc in ("LCD, 8-IN-1 docking module", "MONITOR, 10 IN 1 KVM"):
        result = extract_desc(desc)
        assert result is not None
        assert result.specs == {}, f"{desc!r} must not emit a diagonal"


def test_distinct_named_and_explicit_resolutions_omit_the_key():
    # Conflict pin: FHD (1920x1080) vs explicit 1366x768 ⇒ resolution omitted;
    # the diagonal before the named class still extracts.
    result = extract_desc("LCD, 15.6 FHD 1366x768")
    assert result is not None
    assert result.specs == {"diagonal_size": 15.6}


def test_unseeded_explicit_pixel_pair_is_dropped():
    # 1024x768 matches _RES_EXPLICIT but is not a seeded member — the hardcoded
    # _RES_SEEDED allowlist is the only barrier before record_spec.
    result = extract_desc("LCD, 1024x768 panel")
    assert result is not None
    assert result.specs == {}


def test_backlight_is_always_the_generic_led_bucket():
    # WLED (white) and bare LED collapse to the generic seeded "LED" member — the
    # seeded "LED White"/"LED RGB" members are never emitted from descriptions
    # (white-vs-RGB is not expressible in TRIO desc grammar).
    for desc in ("PNL,15.6 FHD AG WLED SVA 45% 220neDP,INX", "HU, FHD AG LED UWVA 13 TS PVCY"):
        result = extract_desc(desc)
        assert result is not None
        assert result.specs["backlight"] == "LED"
