# Product thinking: Refinancing Strategy UI and TMS integration

## A) Refinancing strategy UI

### User stories
1. As a Treasury analyst, I want to define conditional refinancing rules (rate-threshold, tenor choice) so I can compare expected cost and risk before execution.
2. As a Head of Treasury, I want to run approved scenario sets and see P5/P50/P95 cashflow ranges by year so I can sign off strategy with quantified downside.
3. As an Internal Audit reviewer, I want full model/version traceability so every strategy run can be reproduced and explained.

### Key functional requirements
- Inputs:
  - Debt universe (instrument master + cashflow schedule + current coupons + maturities).
  - Scenario set selection (e.g., internal stochastic set, stress set, vendor curves).
  - Strategy rule builder:
    - Example rule 1: "If market rate > X, issue tenor = short; else tenor = long."
    - Example rule 2: "If market fixed rate is at least Y bps below outstanding fixed coupon, trigger early refinance."
- Outputs:
  - Annual interest distribution by year (mean, P5/P50/P95).
  - Cumulative cost distribution and risk decomposition by instrument/rule.
  - Refinance schedule and implied maturity wall under strategy.
- Auditability / versioning:
  - Immutable run IDs with timestamp, user, strategy JSON, scenario set hash, model version hash.
  - "Diff" view between two strategy runs.
  - Exportable report bundle (CSV + PDF/PowerPoint snapshots).

## B) Minimum additional data for SONIA-linked and index-linked modelling

### SONIA-linked minimum fields
- Reset frequency and fixing lag (daily compounded SONIA, lookback/observation shift).
- Day count convention and business-day calendar.
- Contract spread / CAS spread terms (bps over SONIA).
- Interest period boundaries and payment dates.
- Floor/cap features and fallback language.

### RPI/index-linked minimum fields
- Index reference (RPI/CPI), publication lag (e.g., 3 months), and interpolation rule.
- Base index level at issuance and uplift formula.
- Real vs nominal coupon structure (real coupon + inflation uplift mechanics).
- Deflation floor handling and principal indexation rules.
- Redemption indexation method and any caps/collars.

## C) Integration with a Treasury Management System (TMS)
1. Ingest positions/trades from TMS instrument master (ISIN, desk IDs, legal entity, currency).
2. Pull contractual cashflow schedules and coupon conventions (fixed/floating/index-linked fields).
3. Reconcile current outstanding notionals against GL/settlement records.
4. Connect market data feed (SONIA curves, inflation curves, FX, credit spreads) with timestamped snapshots.
5. Trigger simulation runs via API with selected strategy + scenario package.
6. Persist outputs to TMS analytics store (yearly cashflow forecast, VaR-style quantiles, maturity profile).
7. Provide drill-through from portfolio totals to instrument-level contribution.
8. Export approved forecasts to liquidity planning and hedge-accounting workflows.
9. Enforce RBAC, approval gates, and full audit logs for strategy edits and run execution.
10. Store model metadata and run artifacts for regulatory/internal audit replay.
