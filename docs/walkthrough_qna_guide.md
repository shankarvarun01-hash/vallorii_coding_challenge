# 40-minute walkthrough and Q&A guide

## Suggested agenda

1. **(5 min) Problem framing and assumptions**
   - What was provided vs what was missing.
   - Why specific modelling simplifications were used (annual time step, on-demand handling, floating proxy).

2. **(10 min) Data pipeline and debt-stack explanation**
   - How the debt table was cleaned into a machine-friendly schema.
   - Debt composition, maturity wall, and run-off profile interpretation.

3. **(12 min) Simulation engine**
   - Scenario generation approach (mean-reverting random walk to forward anchor).
   - Regime A and Regime B mechanics.
   - Why Regime B builds long-tail uncertainty.

4. **(8 min) Results and risk interpretation**
   - Annual P5/P50/P95 profiles.
   - Cumulative cost distributions and high-risk years.
   - Sensitivity intuition (floating share, refinancing waves, tenor lock-in).

5. **(5 min) Productization roadmap**
   - Strategy-rule UI.
   - Required data extensions for SONIA/index-linked fidelity.
   - TMS integration and controls.

## Anticipated Q&A prompts

- **How robust is the result to scenario assumptions?**
  - Re-run with different kappa/sigma/forward curves and compare cumulative distributions.
- **Why is WAM relatively short?**
  - Large 2025-2035 maturity concentration and on-demand balances.
- **What if refinancing is not always 20y fixed?**
  - Replace static tenor with rule-based tenor function and re-simulate.
- **How to model SONIA-linked properly?**
  - Introduce reset calendars, compounding conventions, and spread/floor terms.
- **How to validate model quality in production?**
  - Benchmark against historical realized cashflows, scenario backtesting, and controlled release with audit trail.
