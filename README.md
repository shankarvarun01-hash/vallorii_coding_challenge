# Scottish Power Refinancing Strategy Simulation

This repository contains a pragmatic Python pipeline for:

1. Ingesting and explaining a debt maturity stack from the Scottish Power 2024 accounts table.
2. Simulating annual interest costs under 2,000 rate scenarios for two refinancing regimes:
   - **Regime A:** run-off (no refinancing)
   - **Regime B:** full refinancing (replace maturities with 20-year fixed debt)
3. Exporting charts, tables, and a short slide-style summary plus product-design notes.

## Project structure

- `data/debt_schedule_2024.csv` - cleaned debt stack from the provided screenshot/table.
- `src/refinancing_pipeline.py` - end-to-end simulation and report generation.
- `outputs/` - generated charts, scenario tables, and markdown reports (created at runtime).

## Run

```bash
python3 -m pip install -r requirements.txt
python3 src/refinancing_pipeline.py
```

Optional arguments:

```bash
python3 src/refinancing_pipeline.py --debt-path data/debt_schedule_2024.csv --outdir outputs
```

## Key modelling assumptions

- Snapshot date is treated as year-end 2024; simulation years are 2025-2054.
- On-demand loans are treated as maturing in **2025**.
- Existing floating coupons (`Base + spread`, `SONIA + spread`) are proxied as:
  - `scenario_market_rate(year) + contractual_spread`.
- Annual time step:
  - Instruments are considered outstanding through their maturity year.
  - Refinancing occurs in the maturity year and starts accruing coupon from the following year.
- Refinanced debt is always issued as 20-year fixed at that year's scenario market rate.
- Principal flows are not modelled in P&L (focus is annual interest cost).

## Outputs

The script writes:

- Debt stack charts:
  - `maturity_wall.png`
  - `runoff_outstanding_notional.png`
- Regime cost charts:
  - `annual_interest_regime_a.png`
  - `annual_interest_regime_b.png`
  - `cumulative_interest_distribution.png`
- CSV summaries:
  - `annual_interest_summary_regime_a.csv`
  - `annual_interest_summary_regime_b.csv`
  - `cumulative_interest_summary.csv`
  - `high_risk_years_regime_b.csv`
- Report docs:
  - `slide_deck.md`
  - `product_design.md`

---

## Climate finance database pipeline (OECD + World Bank + MDB/DFI)

The repository now also includes a second pipeline for building a project-level climate finance database and flow view:

- Script: `src/climate_finance_pipeline.py`
- Main sources:
  - OECD CRS project-level files (official OECD bulk downloads discovered via OECD SDMX metadata).
  - World Bank project-level climate coefficient workbook.
  - MDB/DFI project-level climate disclosures dataset (includes AsDB and other MDBs).

### Install

```bash
python3 -m pip install -r requirements.txt
```

### Run (default OECD years = 2024 and 2023)

```bash
python3 src/climate_finance_pipeline.py
```

Run with multiple OECD years:

```bash
python3 src/climate_finance_pipeline.py --oecd-years 2024 2023
```

### Outputs

The climate pipeline writes to `outputs/`:

- `climate_finance_projects.csv` (normalized project-level table)
- `climate_finance_flow_edges.csv` (edge list for actor -> instrument -> objective -> sector)
- `climate_finance_flow_cube.csv` (aggregated 4D flow cube)
- `climate_finance.db` (SQLite database with project and edge tables)
- `climate_finance_sankey.html` (interactive Sankey flow visualization)
- `climate_finance_summary.md` (coverage, totals, top actors/sectors, methodology notes)