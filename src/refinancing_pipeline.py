from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


BASE_YEAR = 2025
HORIZON_YEARS = 30
N_SCENARIOS = 2000
REFI_TENOR_YEARS = 20
RNG_SEED = 42


@dataclass
class PipelineOutputs:
    summary_stats: dict[str, float]
    annual_a: pd.DataFrame
    annual_b: pd.DataFrame
    cumulative: pd.DataFrame
    risk_years: pd.DataFrame


def load_debt_stack(path: Path) -> pd.DataFrame:
    debt = pd.read_csv(path)
    debt["maturity_year"] = np.where(
        debt["maturity_date"].eq("On demand"),
        BASE_YEAR,
        pd.to_datetime(debt["maturity_date"]).dt.year,
    ).astype(int)
    debt["fixed_coupon_decimal"] = debt["fixed_coupon_decimal"].astype(float)
    debt["spread_decimal"] = debt["spread_decimal"].astype(float)
    debt["years_to_maturity"] = (debt["maturity_year"] - BASE_YEAR + 1).clip(lower=0)
    return debt


def debt_stack_summary(debt: pd.DataFrame) -> dict[str, float]:
    total_notional = float(debt["notional_mn_gbp"].sum())
    fixed_notional = float(debt.loc[debt["rate_type"] == "fixed", "notional_mn_gbp"].sum())
    floating_notional = float(
        debt.loc[debt["rate_type"] == "floating_proxy", "notional_mn_gbp"].sum()
    )
    wam = float((debt["notional_mn_gbp"] * debt["years_to_maturity"]).sum() / total_notional)
    return {
        "total_notional_mn_gbp": total_notional,
        "pct_fixed": fixed_notional / total_notional,
        "pct_floating_proxy": floating_notional / total_notional,
        "weighted_average_maturity_years": wam,
    }


def make_year_grid(base_year: int = BASE_YEAR, horizon_years: int = HORIZON_YEARS) -> np.ndarray:
    return np.arange(base_year, base_year + horizon_years)


def build_forward_curve(horizon_years: int = HORIZON_YEARS) -> np.ndarray:
    t = np.arange(1, horizon_years + 1)
    # Stylized SONIA-forward proxy: mildly downward-sloping then stable.
    curve = 0.047 - 0.012 * (1.0 - np.exp(-t / 6.0)) + 0.0008 * np.sin(t / 3.2)
    return np.clip(curve, 0.02, 0.09)


def generate_rate_scenarios(
    n_scenarios: int = N_SCENARIOS,
    horizon_years: int = HORIZON_YEARS,
    seed: int = RNG_SEED,
    kappa: float = 0.35,
    sigma: float = 0.008,
) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    forward = build_forward_curve(horizon_years)
    scenarios = np.zeros((n_scenarios, horizon_years), dtype=float)
    scenarios[:, 0] = np.clip(rng.normal(forward[0], 0.003, size=n_scenarios), 0.0, 0.20)
    for t in range(1, horizon_years):
        shock = rng.normal(0.0, sigma, size=n_scenarios)
        scenarios[:, t] = scenarios[:, t - 1] + kappa * (forward[t] - scenarios[:, t - 1]) + shock
    scenarios = np.clip(scenarios, 0.0, 0.20)
    return scenarios, forward


def existing_interest_matrix(
    debt: pd.DataFrame,
    scenarios: np.ndarray,
    year_grid: np.ndarray,
) -> np.ndarray:
    n_scenarios, horizon = scenarios.shape
    interest = np.zeros((n_scenarios, horizon), dtype=float)
    for _, row in debt.iterrows():
        outstanding_mask = (year_grid <= int(row["maturity_year"])).astype(float)
        notional = float(row["notional_mn_gbp"])
        if row["rate_type"] == "fixed":
            coupon = np.full_like(scenarios, float(row["fixed_coupon_decimal"]))
        else:
            coupon = scenarios + float(row["spread_decimal"])
        interest += notional * coupon * outstanding_mask
    return interest


def runoff_notional_profile(debt: pd.DataFrame, year_grid: np.ndarray) -> np.ndarray:
    profile = []
    for y in year_grid:
        outstanding = debt.loc[debt["maturity_year"] >= y, "notional_mn_gbp"].sum()
        profile.append(outstanding)
    return np.array(profile, dtype=float)


def build_refinancing_issue_schedule(debt: pd.DataFrame, year_grid: np.ndarray) -> pd.Series:
    original_maturity = debt.groupby("maturity_year")["notional_mn_gbp"].sum().to_dict()
    issued_by_year: dict[int, float] = {}
    for y in year_grid:
        roll_maturity = issued_by_year.get(y - REFI_TENOR_YEARS, 0.0)
        current_maturity = original_maturity.get(y, 0.0)
        issued_by_year[y] = current_maturity + roll_maturity
    return pd.Series(issued_by_year, name="issued_notional_mn_gbp")


def refinancing_interest_addon(
    issued_by_year: pd.Series,
    scenarios: np.ndarray,
    year_grid: np.ndarray,
) -> np.ndarray:
    n_scenarios, horizon = scenarios.shape
    addon = np.zeros((n_scenarios, horizon), dtype=float)
    year_to_idx = {int(y): i for i, y in enumerate(year_grid)}

    for issue_year, notional in issued_by_year.items():
        issue_year = int(issue_year)
        if issue_year not in year_to_idx or notional <= 0:
            continue
        issue_idx = year_to_idx[issue_year]
        coupon_at_issue = scenarios[:, issue_idx]
        final_pay_idx = min(horizon - 1, issue_idx + REFI_TENOR_YEARS)
        for pay_idx in range(issue_idx + 1, final_pay_idx + 1):
            addon[:, pay_idx] += notional * coupon_at_issue
    return addon


def quantile_summary_by_year(matrix: np.ndarray, year_grid: np.ndarray) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "year": year_grid,
            "mean_mn_gbp": matrix.mean(axis=0),
            "p5_mn_gbp": np.quantile(matrix, 0.05, axis=0),
            "p50_mn_gbp": np.quantile(matrix, 0.50, axis=0),
            "p95_mn_gbp": np.quantile(matrix, 0.95, axis=0),
        }
    )


def cumulative_summary(interest_a: np.ndarray, interest_b: np.ndarray) -> pd.DataFrame:
    totals_a = interest_a.sum(axis=1)
    totals_b = interest_b.sum(axis=1)
    records = []
    for label, values in [("regime_a_runoff", totals_a), ("regime_b_refinance", totals_b)]:
        records.append(
            {
                "regime": label,
                "mean_mn_gbp": float(values.mean()),
                "p5_mn_gbp": float(np.quantile(values, 0.05)),
                "p50_mn_gbp": float(np.quantile(values, 0.50)),
                "p95_mn_gbp": float(np.quantile(values, 0.95)),
            }
        )
    return pd.DataFrame(records)


def save_debt_stack_charts(debt: pd.DataFrame, year_grid: np.ndarray, outdir: Path) -> None:
    maturity_wall = (
        debt.groupby(["maturity_year", "rate_type"], as_index=False)["notional_mn_gbp"].sum()
        .pivot(index="maturity_year", columns="rate_type", values="notional_mn_gbp")
        .reindex(year_grid, fill_value=0.0)
    )
    maturity_wall.plot(kind="bar", stacked=True, figsize=(11, 4), width=0.85)
    plt.title("Maturity wall (GBP mn) by rate type")
    plt.xlabel("Year")
    plt.ylabel("Notional maturing (GBP mn)")
    plt.tight_layout()
    plt.savefig(outdir / "maturity_wall.png", dpi=160)
    plt.close()

    runoff_profile = runoff_notional_profile(debt, year_grid)
    plt.figure(figsize=(11, 4))
    plt.plot(year_grid, runoff_profile, linewidth=2.2)
    plt.title("Outstanding notional under run-off (no refinancing)")
    plt.xlabel("Year")
    plt.ylabel("Outstanding notional (GBP mn)")
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(outdir / "runoff_outstanding_notional.png", dpi=160)
    plt.close()


def save_interest_fan_chart(summary: pd.DataFrame, title: str, outpath: Path) -> None:
    plt.figure(figsize=(11, 4))
    plt.plot(summary["year"], summary["mean_mn_gbp"], label="Mean", linewidth=2.0)
    plt.plot(summary["year"], summary["p50_mn_gbp"], label="Median (P50)", linewidth=1.5)
    plt.fill_between(
        summary["year"],
        summary["p5_mn_gbp"],
        summary["p95_mn_gbp"],
        alpha=0.2,
        label="P5 - P95",
    )
    plt.title(title)
    plt.xlabel("Year")
    plt.ylabel("Annual interest cost (GBP mn)")
    plt.grid(alpha=0.3)
    plt.legend()
    plt.tight_layout()
    plt.savefig(outpath, dpi=160)
    plt.close()


def save_cumulative_chart(interest_a: np.ndarray, interest_b: np.ndarray, outpath: Path) -> None:
    totals_a = interest_a.sum(axis=1)
    totals_b = interest_b.sum(axis=1)
    plt.figure(figsize=(11, 4))
    bins = 40
    plt.hist(totals_a, bins=bins, alpha=0.5, label="Regime A: run-off")
    plt.hist(totals_b, bins=bins, alpha=0.5, label="Regime B: full refinancing")
    plt.title("Distribution of cumulative 30-year interest cost")
    plt.xlabel("Cumulative interest (GBP mn)")
    plt.ylabel("Scenario count")
    plt.legend()
    plt.tight_layout()
    plt.savefig(outpath, dpi=160)
    plt.close()


def write_slide_markdown(
    outdir: Path,
    stats: dict[str, float],
    annual_a: pd.DataFrame,
    annual_b: pd.DataFrame,
    cumulative: pd.DataFrame,
    risk_years: pd.DataFrame,
) -> None:
    cum_a = cumulative.loc[cumulative["regime"] == "regime_a_runoff"].iloc[0]
    cum_b = cumulative.loc[cumulative["regime"] == "regime_b_refinance"].iloc[0]
    worst = risk_years.head(3)
    years_block = "\n".join(
        [
            f"- {int(r.year)}: P95-P5 spread = {r.risk_spread_mn_gbp:,.1f} mn GBP"
            for r in worst.itertuples()
        ]
    )

    text = f"""# Refinancing Strategy Simulation (Scottish Power debt stack)

## Slide 1 - Debt stack at a glance
- Total notional modelled: **{stats["total_notional_mn_gbp"]:,.0f} mn GBP**
- Mix by rate type:
  - Fixed: **{stats["pct_fixed"]:.1%}**
  - Floating proxy (Base/SONIA + spread): **{stats["pct_floating_proxy"]:.1%}**
- Weighted-average maturity (WAM): **{stats["weighted_average_maturity_years"]:.2f} years**

![Maturity wall](maturity_wall.png)
![Run-off outstanding](runoff_outstanding_notional.png)

## Slide 2 - Interest outcomes under two regimes
- Regime A (run-off): debt naturally amortizes, reducing tail exposure.
- Regime B (full refinancing): notional is repeatedly rolled into 20-year fixed bonds at scenario rates.
- Annual distributions are shown with mean/median and P5-P95 bands.

![Annual interest fan - run-off](annual_interest_regime_a.png)
![Annual interest fan - full refinancing](annual_interest_regime_b.png)

## Slide 3 - Cumulative cost impact and risk years
- 30-year cumulative interest (mn GBP):
  - Regime A mean: **{cum_a["mean_mn_gbp"]:,.0f}**, P5/P50/P95: **{cum_a["p5_mn_gbp"]:,.0f} / {cum_a["p50_mn_gbp"]:,.0f} / {cum_a["p95_mn_gbp"]:,.0f}**
  - Regime B mean: **{cum_b["mean_mn_gbp"]:,.0f}**, P5/P50/P95: **{cum_b["p5_mn_gbp"]:,.0f} / {cum_b["p50_mn_gbp"]:,.0f} / {cum_b["p95_mn_gbp"]:,.0f}**
- The full-refinancing regime typically has larger cumulative cost and wider distribution because the portfolio keeps reloading duration.
- Most sensitive years (largest P95-P5 spread):
{years_block}

![Cumulative distribution](cumulative_interest_distribution.png)

## Slide 4 - Why these risk years matter
- Risk is concentrated where both (a) outstanding notional is still high and (b) floating-rate exposure or newly refinanced coupons are active.
- Under full refinancing, scenario dispersion propagates for 20-year windows after each issuance, so uncertainty compounds.
- Under run-off, uncertainty decays as principal declines.

## Slide 5 - SONIA-linked refinancing variant (qualitative)
- If new debt were SONIA-linked instead of fixed:
  - Near-term cashflow volatility increases (coupon resets every period).
  - Duration risk drops because refinancing locks less long-term rate exposure.
  - P5-P95 spread shifts from long-tail level risk to short-horizon path risk.
- Model change: replace fixed coupon lock-in at issuance with yearly/periodic coupon = SONIA path + contractual spread.
"""
    (outdir / "slide_deck.md").write_text(text)


def write_product_design_doc(outdir: Path) -> None:
    text = """# Product thinking: Refinancing Strategy UI and TMS integration

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
"""
    (outdir / "product_design.md").write_text(text)


def run_pipeline(debt_path: Path, outdir: Path) -> PipelineOutputs:
    outdir.mkdir(parents=True, exist_ok=True)
    debt = load_debt_stack(debt_path)
    year_grid = make_year_grid()
    scenarios, forward_curve = generate_rate_scenarios()

    pd.DataFrame({"year": year_grid, "forward_curve_rate": forward_curve}).to_csv(
        outdir / "forward_curve.csv", index=False
    )
    pd.DataFrame(scenarios).rename(columns=lambda c: f"year_{year_grid[c]}").to_csv(
        outdir / "rate_scenarios.csv", index_label="scenario_id"
    )

    stats = debt_stack_summary(debt)
    interest_existing = existing_interest_matrix(debt, scenarios, year_grid)

    issued_schedule = build_refinancing_issue_schedule(debt, year_grid)
    refi_addon = refinancing_interest_addon(issued_schedule, scenarios, year_grid)

    interest_a = interest_existing
    interest_b = interest_existing + refi_addon

    annual_a = quantile_summary_by_year(interest_a, year_grid)
    annual_b = quantile_summary_by_year(interest_b, year_grid)
    cumulative = cumulative_summary(interest_a, interest_b)

    risk_years = annual_b[["year", "p5_mn_gbp", "p95_mn_gbp"]].copy()
    risk_years["risk_spread_mn_gbp"] = risk_years["p95_mn_gbp"] - risk_years["p5_mn_gbp"]
    risk_years = risk_years.sort_values("risk_spread_mn_gbp", ascending=False).reset_index(drop=True)

    annual_a.to_csv(outdir / "annual_interest_summary_regime_a.csv", index=False)
    annual_b.to_csv(outdir / "annual_interest_summary_regime_b.csv", index=False)
    cumulative.to_csv(outdir / "cumulative_interest_summary.csv", index=False)
    issued_schedule.reset_index().rename(columns={"index": "year"}).to_csv(
        outdir / "refinancing_issued_schedule.csv", index=False
    )
    pd.DataFrame(
        {
            "metric": list(stats.keys()),
            "value": list(stats.values()),
        }
    ).to_csv(outdir / "debt_stack_summary_stats.csv", index=False)
    risk_years.to_csv(outdir / "high_risk_years_regime_b.csv", index=False)

    save_debt_stack_charts(debt, year_grid, outdir)
    save_interest_fan_chart(
        annual_a,
        "Regime A (run-off): annual interest distribution",
        outdir / "annual_interest_regime_a.png",
    )
    save_interest_fan_chart(
        annual_b,
        "Regime B (full refinancing): annual interest distribution",
        outdir / "annual_interest_regime_b.png",
    )
    save_cumulative_chart(interest_a, interest_b, outdir / "cumulative_interest_distribution.png")

    write_slide_markdown(outdir, stats, annual_a, annual_b, cumulative, risk_years)
    write_product_design_doc(outdir)

    return PipelineOutputs(
        summary_stats=stats,
        annual_a=annual_a,
        annual_b=annual_b,
        cumulative=cumulative,
        risk_years=risk_years,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refinancing strategy simulation pipeline")
    parser.add_argument(
        "--debt-path",
        type=Path,
        default=Path("data/debt_schedule_2024.csv"),
        help="Path to debt input CSV",
    )
    parser.add_argument(
        "--outdir",
        type=Path,
        default=Path("outputs"),
        help="Output directory for charts/tables/reporting docs",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    outputs = run_pipeline(args.debt_path, args.outdir)
    print("Pipeline complete.")
    print(f"Total notional (mn GBP): {outputs.summary_stats['total_notional_mn_gbp']:.1f}")
    print(
        "30Y cumulative mean interest (mn GBP): "
        f"run-off={outputs.cumulative.loc[0, 'mean_mn_gbp']:.1f}, "
        f"full-refinancing={outputs.cumulative.loc[1, 'mean_mn_gbp']:.1f}"
    )


if __name__ == "__main__":
    main()
