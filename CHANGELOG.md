# CHANGELOG

All notable changes to the project are logged here.

## 2026-03-12

- Prep for human testing: added STABLE.md (registry of critical files) and this CHANGELOG. No code behavior change.
- Debt/cleanup follow-up: replaced dated Clear-Site-Data TODO with auto-expiry logic, optimized API source quota backfill (no extra quota queries), added regression tests, and fixed two high-risk lint defects (missing VendorCard import + duplicate reject handler name).
