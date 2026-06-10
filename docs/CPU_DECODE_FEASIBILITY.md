# CPU Commodity Decode Feasibility — SFDC LSC1__Material__c

> In-repo copy: this report is the normative grammar + step-0 deny-list source for
> `app/services/desc_extractor/cpu.py` (and the ingest pollution guard in
> `app/services/source_ingest/clean.py`). The corpus extracts it references are
> host-local analysis artifacts (SFDC Weekly Export working set), not committed.

Date: 2026-06-09. Source: `/root/source_ingest/LSC1__Material__c.csv` (775,955 rows), `Commodity_Code__c == 'CPU'` → **39,979 rows** (all MPNs distinct), extracted to `/root/source_ingest/analysis/cpu_rows.csv`. Supporting raw counts/samples in `_classify.json`.

## Verdict (read this first)

A deterministic CPU decoder **is feasible and worth building, but it is a small lever on this bucket**: only **~2.2% (868 rows)** of CPU rows carry a directly decodable Intel/AMD identifier (model string, s-spec, ordering code, or OPN), rising to **~3.1% (1,258 rows)** if partial facets (GHz/core-count tokens in descriptions) count. The bucket's real structure is:

| Segment | Rows | % | Decodable how |
|---|---|---|---|
| Direct Intel/AMD identifier (MPN or desc) | 868 | 2.2% | **Parser + lookup tables (build this)** |
| Partial spec tokens only (GHz/NC/NNW in desc, no model) | 392 | 1.0% | Desc token parser (clock/cores/tdp only) |
| OEM/spare part numbers (HP, IBM/Lenovo, Compaq, Dell, Sun, NetApp) | ~13,800 | ~34.5% | **OEM PN→CPU-model crosswalk only** (external data: PartSurfer, Lenovo parts DB, broker history) |
| Confirmed non-CPU pollution (MLCC caps, TE connectors, EPCOS/AVX passives, logic ICs, tape libraries, arcade boards) | ≥5,600 | ≥14% | Re-bucket / exclude (step 0) |
| Bare numeric/alnum PNs, no description, unidentified vendor | ~19,300 | ~48% | Not deterministically decodable |

So: build the decoder (steps 1–4 below, one shared model→spec table), but pair it with a **pollution filter (step 0)** and treat the **OEM crosswalk (step 5)** as the actual coverage lever — it is 15x larger than everything parseable.

## Dataset profile (why decode is hard here)

- `LSC1__Material_Short_Description__c` **equals the MPN on 99.98% of rows** (39,971/39,979). Only **1,366 rows (3.4%)** have any real description text (in `Material_Description__c` / `LSC1__Material_Detail_Description__c` / a non-echo short desc).
- `LSC1__Manufacturer_Brand__c` is blank on **98.9%** of rows (resolved names: Intel 201, Lenovo 73, HP 65, IBM 61, AMD 36, rest <10 each).
- The SFDC structured CPU columns are empty: `Family__c` 3 rows, `Number_of_Cores__c` 3, `Speed__c`/`Socked__c`/`Pins__c` 0. **Nothing to import; everything must be derived.**
- `Commodity_Code__c='CPU'` is unreliable: it contains Murata GRM MLCCs, Panasonic EEEF/EEUF caps, TE Connectivity connectors (`640456-9`, `1-640456-0`), EPCOS/TDK (`B72220P…`), AVX (`06035A101JAT2A`), TI logic (`SN74ALVC244PWR`), TVS diodes (`SMAJ24CA-13-F`), StorageTek SL500 tape-library parts, even a Konami arcade board.

## 1. MPN shape census (priority-classified, mutually exclusive)

| Class | Rows | % | Sample MPNs |
|---|---|---|---|
| other / unknown | 24,209 | 60.6% | `90005933`, `5DX0J46488`, `5056706131`, `0094535-03`, `51-0135-01-01` |
| HP/HPE spare PN (`######-x##`, `L#####-###`) | 9,155 | 22.9% | `732505-001`, `816789-141`, `802450-B41`, `L17833-003` |
| IBM/Lenovo FRU (7-char `##XX###`) | 3,270 | 8.2% | `01EF243`, `00YA813`, `04X6436`, `44V4430`, `01AG619` |
| Passives / non-CPU semis (telltale prefixes) | 2,370 | 5.9% | `GRM155R71C104MA88D`, `EEEFK1E471GP`, `SN74ALVC244PWR`, `SMAJ24CA-13-F` |
| Intel/AMD model string in MPN or desc | 293 | 0.7% | `589MT` ("PROC,I3-7100T…"), `L22630-003` ("IC,uP,CFL,i5-8500…"), `914472-041` |
| Intel s-spec as whole MPN (`S[RL]xxx`) | 290 | 0.7% | `SR3QS`, `SR3B3`, `SLC2N`, `SRELS`, `SR2KT`, `SR341` |
| Intel ordering code (`CM8/BX8/CD8/AT8…`) | 159 | 0.4% | `CM8068403358316`, `BX80684I58500`, `CD8067303405900` |
| Sun/Oracle PN (`3##-####`) | 93 | 0.2% | `370-2851`, `300-1338`, `541-…` |
| Intel s-spec embedded in MPN/desc | 77 | 0.2% | `SR3NH CD8067303753400`, `AV8063801058401(SR0N9)`, `AJSR29HVT05` — ⚠ ~24 of the raw matches are StorageTek `SL500`/`SL85Z` tape parts, excluded in strict counts |
| AMD OPN | 63 raw / **53 strict** | 0.13% | `100-000000342`, `100-100000344WOF`, `YD1700BBM88AE`, `YM3200C4T2OFG` — ⚠ raw class caught TI/ADI parts (`ADS1118IDGST`, `AMC1311DWVR`), excluded |

Inside "other/unknown", shape clustering identifies further attackable OEM clusters: Compaq option PNs `######-XX#` (647: `314756-ED0`), Dell SKUs `400-XXXX` (114: `400-ABRJ`) and Dell 5-char PNs (`0WJ129`-style, ~90–1,200 loose), NetApp `108-000##` (36), HP A-series/ME-series (`AB584A`, `A9733-69004`, `ME060-50006`, ~345) — plus ~3,200 more passives/connectors (TE 2,112; EPCOS 940; AVX 170).

## 2. Description grammar inventory (1,366 desc rows)

| Grammar / token | Rows | Example |
|---|---|---|
| GHz clock token | 909 (2.3% of all) | `IC,uP,CFL,i5-8400,2.8GHz,65W,9MB` |
| Core count (`12C`, `8-Core`, `/4C/`) | 339 | `SPS-CPU BDW E5-2650L V4 14C 1_7GHZ 65W` |
| TDP watts (`95W`) | 370 | `Xeon GOLD 6134 3.2G 8C 130W` |
| Generation suffix `vN` | 63 | `SPS-PROC XEON E5-2690V4 2.6 2400 14C` |
| Socket (`LGA####`/`1366`) | 15 | `PROCESSOR XEON E5606 2.13GHZ 1366` |
| HP board-IC grammar `IC,uP,<codename>,<model>,<GHz>,<W>,<cache>` | 82 | `IC uP KBL i7-7600U 2.8GHz 15W BGA H-0: Spec code: SR3RZ` |
| HP spares grammar `SPS-CPU/SPS-PROC <codename> <model> <NC> <GHz> <W>` | 67 | `SPS-CPU INTCFL-R i7-9700K 8C 3.6GHz 95W` |
| Itanium/PA-RISC free text | 177 | `1.6GHZ ITANIUM2 DUAL CORE CPU` |
| "Xeon" word | 183 | — |
| Core iN model | 273 | — |
| AMD family word (EPYC/Ryzen/Opteron…) | 74 | `SP AMD Ryzen 5 2400GE 3.2GHz/4C/4M/35W` |

The HP `IC,uP` and `SPS-` grammars are fully deterministic (comma/space-delimited fields) and double as **codename → architecture** sources (CFL=Coffee Lake, KBL=Kaby Lake, BDW=Broadwell, SKL=Skylake, HSW=Haswell, CLX=Cascade Lake).

## 3. Lookup-table analysis

**Intel s-spec table (pathway b):** 351 strict rows carry an s-spec; **~340 distinct codes, almost all appearing exactly once** (whole-MPN: 290 rows / 290 distinct). Distribution is flat — top-100 covers only 36% of s-spec rows. A "top-N" table is useless; you need a **full public s-spec→model dump** (Intel ARK + cpu-world, ~5–8k entries, one-time scrape). Good news: 239/290 whole-MPN s-spec rows also have descriptions, so the table mainly adds confirmation + the rows whose desc lacks the model.

**Model→spec table (required by every pathway):** the parser only yields a model string (`E5-2680 v4`, `Gold 6248`, `i7-9700K`, `EPYC 7551`). Family is derivable from the string itself; **core_count / clock / tdp / socket / architecture require a model→spec table** (Intel ARK dump ≈ 3–4k Xeon/Core/Pentium models + AMD ≈ 500). This single table is the decoder's backbone.

**Intel ordering codes (pathway d):** 200 rows. `BX8…` retail codes embed the model readably (`BX80684I58500` → i5-8500); `CM8/CD8` tray codes need an ordering-code→s-spec/model table (also public via ARK).

**AMD OPN (pathway c):** 90 strict rows (31 EPYC/Ryzen `100-…` + 22 `YD/YM` Ryzen OPNs + desc-text hits). `YD2600BBM6IAF`-style OPNs structurally encode model/cores; `100-000000xxx` are opaque sequence numbers → small lookup table (~200 entries covers all shipped EPYC/Ryzen OPNs).

## 4. Recommended implementation order

| Step | What | Build cost | New rows w/ facets | Cumulative coverage | Facets populated |
|---|---|---|---|---|---|
| 0 | **Pollution filter**: deny-list regex (Murata/TE/EPCOS/AVX/Panasonic/logic-IC/diode shapes) → re-bucket out of CPU | Low | — (removes ≥5,600 junk rows, ~14%) | facet purity, denominators shrink to ~34k | — |
| 1 | **Model-string + desc-token parser** (regex over MPN+desc: `E[357]-####vN`, Gold/Silver/Platinum/Bronze ####, `i[3579]-####X`, `W-####`, legacy `X/L/E5###` w/ Xeon context, GHz/`NC`/`NNW`/LGA tokens, HP `IC,uP`/`SPS-` grammars, codename map) | Low (pure regex + small codename table) | ~1,258 rows ≥1 facet; ~461 full | 3.1% | family, core_count, clock_speed_ghz, tdp_watts, architecture (socket via step-1b table) |
| 1b | **Model→spec table** (ARK dump, ~4k Intel + 500 AMD models) keyed by normalized model string | Medium (one-time scrape/static dataset) | upgrades every step-1 partial row to all-6-facet | 3.1% but full facets | all 6 |
| 2 | **S-spec→model lookup** (full public table; do NOT build top-N — distribution is flat) | Medium (one-time scrape ~5–8k entries) | +~150 net new (351 total, overlaps step 1) | ~2.5% direct / 3.4% any | all 6 (via 1b) |
| 3 | **Intel ordering-code handling**: regex-extract model from `BX8…`; ordering-code table for `CM8/CD8` | Low–Medium | +~100 net new (200 total) | ~3.6% any | all 6 (via 1b) |
| 4 | **AMD OPN parser** (`100-…` table + `YD/YM` structural decode) | Low | +~60 net new (90 total) | ~3.7% any | all 6 (via 1b) |
| 5 | **OEM PN crosswalk** (HP 9.2k, IBM/Lenovo 3.3k, Compaq 0.6k, Dell/Sun/NetApp/HP-A ~0.7k): external spare-PN→model data (HP PartSurfer, Lenovo parts API, broker history) | High (external acquisition; = existing "Phase 2 PartSurfer" enrichment design) | up to +13,800 | **~38% ceiling** | all 6 (via 1b) |

Steps 1–4 are one deliverable ("cpu decoder", same pattern as the HDD/DRAM decoders): regexes + 3 static lookup tables (model→spec, s-spec→model, ordering/OPN→model). Step 0 should ship with it. Step 5 is a separate enrichment program, not a decoder.

## 5. Facet keys (cpu commodity seed: family, socket, core_count, clock_speed_ghz, tdp_watts, architecture)

| Facet key | Source precedence |
|---|---|
| `family` | model string class (Xeon E5/E7/E3, Xeon Scalable Gold/Silver/Platinum/Bronze, Core i3/i5/i7/i9, Pentium/Celeron, EPYC, Ryzen, Opteron, Itanium) — direct from parser |
| `socket` | model→spec table only (desc carries it on just 15 rows) |
| `core_count` | desc token (`12C`, `8-Core`) → else model→spec table |
| `clock_speed_ghz` | desc GHz token → else model→spec table (base clock) |
| `tdp_watts` | desc `NNW` token → else model→spec table |
| `architecture` | HP codename token (CFL/KBL/BDW/SKL/HSW/CLX…) → vN suffix map (v2=Ivy Bridge, v3=Haswell, v4=Broadwell) → model→spec table codename |

## 6. Caveats found during classification (encode as guards in the decoder)

- `S[RL]xxx` embedded matching MUST exclude tape-library contexts (`SL500`, `SL85Z`, `LTO*`, `STK/*`) — 24 false rows otherwise.
- AMD prefix patterns (`AD…`, `AMC…`, `ADS…`, `OS…`) collide with Analog Devices/TI parts; require strict OPN structure (`100-…`, `Y[DM]####…`) or family word in desc.
- Bare `[XLEW]####` model matching (X5650-era Xeons) needs Xeon context or a model allowlist — `W5100` (WIZnet ethernet chip) and `W3010` are in the bucket as whole MPNs.
- Some MPN fields contain compound strings (`SR3NH  CD8067303753400`, `AV8063801058401(SR0N9)`, `BD3420  SLH25`) — tokenize before matching.
- Descriptions can contain HTML blobs (`<table class="ql-table-blob"…`) — strip tags before parsing.
