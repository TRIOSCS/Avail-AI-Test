# Materials Filter Expansion Design

**Date**: 2026-03-20
**Status**: Approved

## Summary

Expand the Materials tab filtering from surface-level specs (3-5 per commodity) to practical sourcing-depth specs across all 16 commodity categories, add a global manufacturer filter, and implement automated lifecycle status detection.

## Goals

1. Engineers/buyers can filter parts by the specs they'd use on an RFQ
2. Manufacturer is a first-class global filter (type-ahead dropdown)
3. Lifecycle status stays current automatically without manual effort

---

## 1. Global Filters

### 1.1 Manufacturer Dropdown

- **Location**: Above the commodity tree in the filter sidebar
- **Type**: Searchable type-ahead dropdown, multi-select
- **Data source**: Distinct `MaterialCard.manufacturer` values from DB
- **Scoping**: If a commodity is selected, show only manufacturers within that commodity; otherwise show all
- **Display**: Top 20 by card count, type-ahead search for the rest
- **Query**: Filters on `MaterialCard.manufacturer IN (...)`

### 1.2 Lifecycle Auto-Detection

Lifecycle status (`active`, `eol`, `obsolete`, `nrfnd`, `ltb`) is maintained automatically — not a user-facing filter.

**Triggers**:
- **On enrichment**: When a part is enriched via Claude AI, extract lifecycle status from enrichment data
- **On part creation**: Check lifecycle when a new MaterialCard is created
- **Periodic sweep**: Background job (weekly) checks lifecycle on all parts marked `active` to detect newly EOL/obsolete parts

**Implementation**:
- Add lifecycle detection to the Claude AI enrichment prompt
- On part creation: set lifecycle from enrichment data during `upsert_material_card()` in search service
- Periodic sweep: register a weekly task in `app/scheduler.py` that queries Nexar and DigiKey (the two connectors that expose lifecycle data) for all parts currently marked `active`
- Sweep updates `MaterialCard.lifecycle_status` and logs changes
- Add an index on `MaterialCard.lifecycle_status` for the sweep query

---

## 2. Expanded Commodity Specs

Additions per commodity (on top of existing specs). All new specs are `is_filterable: true`. Specs marked `[PRIMARY]` show as chips on the list view.

### Passives

**Capacitors** (existing: capacitance, voltage_rating, dielectric, tolerance, package)
| Spec Key | Display Name | Data Type | Values/Range | Unit |
|----------|-------------|-----------|--------------|------|
| mounting | Mounting | enum | SMD, through-hole, press-fit | — |

**Resistors** (existing: resistance, power_rating, tolerance, package)
| Spec Key | Display Name | Data Type | Values/Range | Unit |
|----------|-------------|-----------|--------------|------|
| mounting | Mounting | enum | SMD, through-hole, press-fit | — |

**Inductors** (existing: inductance, current_rating, package)
| Spec Key | Display Name | Data Type | Values/Range | Unit |
|----------|-------------|-----------|--------------|------|
| mounting | Mounting | enum | SMD, through-hole, press-fit | — |
| inductor_type | Type | enum | Ferrite, Wirewound, Multilayer, Film, Ceramic | — |

### Semiconductors — Discrete

**Diodes** (existing: type, voltage, current, package)
| Spec Key | Display Name | Data Type | Values/Range | Unit |
|----------|-------------|-----------|--------------|------|
| mounting | Mounting | enum | SMD, through-hole, press-fit | — |

**MOSFETs** (existing: channel_type, vds, rds_on, id_max, package)
| Spec Key | Display Name | Data Type | Values/Range | Unit |
|----------|-------------|-----------|--------------|------|
| mounting | Mounting | enum | SMD, through-hole, press-fit | — |

### Processors & Programmable

**Microcontrollers** (existing: core, flash_kb, ram_kb, clock_mhz, package)
| Spec Key | Display Name | Data Type | Values/Range | Unit |
|----------|-------------|-----------|--------------|------|
| supply_voltage | Supply Voltage | numeric | 1.0–5.5 | V |
| has_uart | UART | boolean | — | — |
| has_spi | SPI | boolean | — | — |
| has_i2c | I2C | boolean | — | — |
| has_usb | USB | boolean | — | — |
| has_can | CAN | boolean | — | — |

**CPU** (existing: socket, core_count, clock_speed_ghz, tdp_watts, architecture)
| Spec Key | Display Name | Data Type | Values/Range | Unit |
|----------|-------------|-----------|--------------|------|
| family | Family | enum | Xeon, Core i-series, Ryzen, EPYC, Threadripper, Atom, ARM | — |

### Memory & Storage

**DRAM** (existing: ddr_type, capacity_gb, speed_mhz, ecc, form_factor)
- No additions — already has the right sourcing specs.

**Flash** (existing: capacity_gb, interface, package)
| Spec Key | Display Name | Data Type | Values/Range | Unit |
|----------|-------------|-----------|--------------|------|
| voltage | Voltage | numeric | 1.2–5.0 | V |
| flash_form_factor | Form Factor | enum | DIP, TSOP, BGA, WSON, SOIC | — |

**SSD** (existing: capacity_gb, form_factor, interface, read_speed_mbps)
| Spec Key | Display Name | Data Type | Values/Range | Unit |
|----------|-------------|-----------|--------------|------|
| write_speed_mbps | Write Speed | numeric | 100–7500 | MB/s |
| nand_type | NAND Type | enum | SLC, MLC, TLC, QLC, PLC | — |

**HDD** (existing: capacity_gb, rpm, form_factor, interface)
- No additions — already covers capacity, RPM, form factor, interface.

### Connectors & Electromechanical

**Connectors** (existing: pin_count, pitch_mm, mounting, gender, series)
| Spec Key | Display Name | Data Type | Values/Range | Unit |
|----------|-------------|-----------|--------------|------|
| connector_type | Connector Type | enum | USB, RJ45, HDMI, PCIe, D-Sub, JST, Molex, FPC/FFC, M.2, SATA, SAS | — |

### Power & Energy

**Power Supplies** (existing: wattage, form_factor, efficiency)
| Spec Key | Display Name | Data Type | Values/Range | Unit |
|----------|-------------|-----------|--------------|------|
| input_voltage | Input Voltage | numeric | 90–480 | V |
| output_voltage | Output Voltage | numeric | 1.0–48.0 | V |
| psu_connector_type | Connector Type | enum | ATX 24-pin, EPS 8-pin, PCIe 6-pin, PCIe 8-pin, Barrel, Molex, SATA | — |

### IT / Server Hardware

**Motherboards** (existing: socket, form_factor, chipset, ram_slots)
| Spec Key | Display Name | Data Type | Values/Range | Unit |
|----------|-------------|-----------|--------------|------|
| max_memory_gb | Max Memory | numeric | 8–6144 | GB |
| pcie_gen | PCIe Generation | enum | Gen3, Gen4, Gen5 | — |

**Network Cards** (existing: speed, ports, interface, controller)
| Spec Key | Display Name | Data Type | Values/Range | Unit |
|----------|-------------|-----------|--------------|------|
| media_type | Media Type | enum | Copper, Fiber, Copper/Fiber | — |

### GPU

**GPU** (existing: memory_gb, memory_type, interface)
| Spec Key | Display Name | Data Type | Values/Range | Unit |
|----------|-------------|-----------|--------------|------|
| gpu_family | Family | enum | GeForce, Quadro, RTX, Radeon, Radeon Pro, Tesla, A-series, H-series | — |
| tdp_watts | TDP | numeric | 25–700 | W |

---

## 3. Enrichment Prompt Update

Update the Claude AI enrichment prompt to extract all new spec fields. The prompt should:

- Include the full list of spec keys per commodity
- Ask Claude to extract values from part description, datasheet, and cross-reference data
- Return structured specs in the existing `specs_structured` JSONB format
- Include lifecycle status detection in every enrichment call

---

## 4. Bulk Re-Enrichment

After deploying the new specs:

1. Seed the new `CommoditySpecSchema` rows (add to `commodity_seeds.json`)
2. Run a bulk re-enrichment job on all existing MaterialCards
3. Re-enrichment populates `specs_structured` with new fields
4. Backfill `MaterialSpecFacet` rows from the new structured specs

This can run as a background management command to avoid blocking the app.

---

## 5. Implementation Scope

### In scope
- New specs added to `commodity_seeds.json`
- Seed migration to populate `CommoditySpecSchema`
- Manufacturer global filter (UI + query)
- Lifecycle auto-detection on enrichment, creation, and periodic sweep
- Enrichment prompt update for new specs + lifecycle
- Bulk re-enrichment command
- Facet backfill from re-enriched data

### Out of scope
- Price range filter
- Vendor count filter
- Search recency filter

### Database changes
- Add index on `MaterialCard.manufacturer` (for manufacturer dropdown performance)
- Add index on `MaterialCard.lifecycle_status` (for sweep query)
- Both via Alembic migration

### Testing
- Seed validation: verify new specs load into `CommoditySpecSchema` without errors
- Facet query tests: filter by new specs returns correct results
- Manufacturer filter: endpoint returns filtered results by manufacturer, scoped to commodity
- Lifecycle detection: enrichment sets lifecycle, sweep updates stale records
- Bulk re-enrichment: command populates new facets from re-enriched data
