from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd


RECOMMENDED_COLUMNS = [
    "name",
    "current_role",
    "record_type",
    "institution",
    "title",
    "start_year",
    "end_year",
    "source_url",
]


def _normalize_text(series: pd.Series) -> pd.Series:
    return series.astype("string").str.strip().replace({"": pd.NA})


def _parse_year_value(value: object, reference_year: int) -> float:
    if pd.isna(value):
        return np.nan
    text = str(value).strip().lower()
    if text in {"", "nan", "na", "none"}:
        return np.nan
    if text in {"present", "current", "now", "ongoing"}:
        return float(reference_year)
    digits = "".join(char for char in text if char.isdigit())
    if len(digits) >= 4:
        return float(digits[:4])
    return np.nan


def _normalize_recommended(df: pd.DataFrame, reference_year: int) -> pd.DataFrame:
    expected = set(RECOMMENDED_COLUMNS)
    missing = sorted(expected - set(df.columns))
    if missing:
        raise ValueError(
            "Input is missing required columns for recommended format: "
            + ", ".join(missing)
        )

    normalized = df.copy()
    for column in ["name", "current_role", "record_type", "institution", "title", "source_url"]:
        normalized[column] = _normalize_text(normalized[column])

    normalized["record_type"] = normalized["record_type"].str.lower()
    normalized["start_year"] = normalized["start_year"].apply(_parse_year_value, reference_year=reference_year)
    normalized["end_year"] = normalized["end_year"].apply(_parse_year_value, reference_year=reference_year)
    return normalized


def _normalize_wide(df: pd.DataFrame, reference_year: int) -> pd.DataFrame:
    required_shared = {"name", "current_role", "source_url"}
    if not required_shared.issubset(df.columns):
        missing = sorted(required_shared - set(df.columns))
        raise ValueError(
            "Input did not match known format. Missing shared columns: " + ", ".join(missing)
        )

    working = df.copy()
    for col in working.columns:
        if working[col].dtype == object:
            working[col] = _normalize_text(working[col])

    edu_cols = {"education_school", "education_degree", "education_start_year", "education_end_year"}
    exp_cols = {"experience_org", "experience_role", "experience_start_year", "experience_end_year"}
    if not edu_cols.issubset(working.columns) or not exp_cols.issubset(working.columns):
        missing = sorted((edu_cols | exp_cols) - set(working.columns))
        raise ValueError(
            "Input did not match known wide format. Missing columns: " + ", ".join(missing)
        )

    edu = working[
        ["name", "current_role", "source_url", "education_school", "education_degree", "education_start_year", "education_end_year"]
    ].rename(
        columns={
            "education_school": "institution",
            "education_degree": "title",
            "education_start_year": "start_year",
            "education_end_year": "end_year",
        }
    )
    edu["record_type"] = "education"

    exp = working[
        ["name", "current_role", "source_url", "experience_org", "experience_role", "experience_start_year", "experience_end_year"]
    ].rename(
        columns={
            "experience_org": "institution",
            "experience_role": "title",
            "experience_start_year": "start_year",
            "experience_end_year": "end_year",
        }
    )
    exp["record_type"] = "experience"

    normalized = pd.concat([edu, exp], ignore_index=True)
    normalized["start_year"] = normalized["start_year"].apply(_parse_year_value, reference_year=reference_year)
    normalized["end_year"] = normalized["end_year"].apply(_parse_year_value, reference_year=reference_year)
    return normalized


def load_profiles(path: Path, reference_year: int) -> pd.DataFrame:
    df = pd.read_csv(path)
    if {"record_type", "institution", "title"}.issubset(df.columns):
        normalized = _normalize_recommended(df, reference_year=reference_year)
    else:
        normalized = _normalize_wide(df, reference_year=reference_year)

    normalized = normalized[RECOMMENDED_COLUMNS].copy()
    normalized["name"] = _normalize_text(normalized["name"])
    normalized = normalized.dropna(subset=["name", "record_type"])
    normalized["record_type"] = normalized["record_type"].str.lower()
    normalized = normalized[normalized["record_type"].isin(["education", "experience"])].copy()
    normalized["institution"] = _normalize_text(normalized["institution"])
    normalized["title"] = _normalize_text(normalized["title"])
    normalized["current_role"] = _normalize_text(normalized["current_role"])
    normalized["source_url"] = _normalize_text(normalized["source_url"])
    normalized["record_duration_years"] = np.where(
        normalized["start_year"].notna() & normalized["end_year"].notna(),
        (normalized["end_year"] - normalized["start_year"]).clip(lower=0.0),
        np.nan,
    )
    return normalized.reset_index(drop=True)


def build_people_summary(records: pd.DataFrame) -> pd.DataFrame:
    experience = records[records["record_type"] == "experience"].copy()
    education = records[records["record_type"] == "education"].copy()

    experience_summary = (
        experience.groupby("name", dropna=False)
        .agg(
            total_experience_years=("record_duration_years", "sum"),
            experience_entries=("record_type", "count"),
            unique_experience_orgs=("institution", "nunique"),
        )
        .reset_index()
    )
    education_summary = (
        education.groupby("name", dropna=False)
        .agg(
            education_entries=("record_type", "count"),
            unique_education_institutions=("institution", "nunique"),
        )
        .reset_index()
    )
    roles = (
        records[["name", "current_role"]]
        .dropna(subset=["name"])
        .drop_duplicates(subset=["name"], keep="first")
    )

    people = roles.merge(experience_summary, on="name", how="left").merge(education_summary, on="name", how="left")
    fill_cols = [
        "total_experience_years",
        "experience_entries",
        "unique_experience_orgs",
        "education_entries",
        "unique_education_institutions",
    ]
    people[fill_cols] = people[fill_cols].fillna(0.0)
    people["total_experience_years"] = people["total_experience_years"].round(2)
    int_cols = ["experience_entries", "unique_experience_orgs", "education_entries", "unique_education_institutions"]
    people[int_cols] = people[int_cols].astype(int)
    return people.sort_values(["total_experience_years", "name"], ascending=[False, True]).reset_index(drop=True)


def build_experience_by_person_org(records: pd.DataFrame) -> pd.DataFrame:
    experience = records[(records["record_type"] == "experience") & records["institution"].notna()].copy()
    if experience.empty:
        return pd.DataFrame(
            columns=["name", "organization", "roles_count", "total_years", "first_start_year", "last_end_year"]
        )
    grouped = (
        experience.groupby(["name", "institution"], dropna=False)
        .agg(
            roles_count=("record_type", "count"),
            total_years=("record_duration_years", "sum"),
            first_start_year=("start_year", "min"),
            last_end_year=("end_year", "max"),
        )
        .reset_index()
        .rename(columns={"institution": "organization"})
    )
    grouped["total_years"] = grouped["total_years"].round(2)
    return grouped.sort_values(["total_years", "name", "organization"], ascending=[False, True, True]).reset_index(
        drop=True
    )


def build_education_distribution(records: pd.DataFrame) -> pd.DataFrame:
    education = records[(records["record_type"] == "education") & records["institution"].notna()].copy()
    if education.empty:
        return pd.DataFrame(columns=["institution", "records", "unique_people"])
    dist = (
        education.groupby("institution", dropna=False)
        .agg(records=("record_type", "count"), unique_people=("name", "nunique"))
        .reset_index()
    )
    return dist.sort_values(["unique_people", "records", "institution"], ascending=[False, False, True]).reset_index(
        drop=True
    )


def build_experience_distribution(records: pd.DataFrame) -> pd.DataFrame:
    experience = records[(records["record_type"] == "experience") & records["institution"].notna()].copy()
    if experience.empty:
        return pd.DataFrame(columns=["organization", "records", "unique_people", "total_years"])
    dist = (
        experience.groupby("institution", dropna=False)
        .agg(
            records=("record_type", "count"),
            unique_people=("name", "nunique"),
            total_years=("record_duration_years", "sum"),
        )
        .reset_index()
        .rename(columns={"institution": "organization"})
    )
    dist["total_years"] = dist["total_years"].round(2)
    return dist.sort_values(["unique_people", "total_years", "records", "organization"], ascending=[False, False, False, True]).reset_index(drop=True)


def build_summary_markdown(
    records: pd.DataFrame,
    people: pd.DataFrame,
    edu_dist: pd.DataFrame,
    exp_dist: pd.DataFrame,
    source_path: Path,
    reference_year: int,
    top_n: int,
) -> str:
    n_people = int(people["name"].nunique())
    n_records = int(len(records))
    exp_people = int((people["experience_entries"] > 0).sum())
    edu_people = int((people["education_entries"] > 0).sum())
    median_years = float(people["total_experience_years"].median()) if not people.empty else 0.0
    p75_years = float(people["total_experience_years"].quantile(0.75)) if not people.empty else 0.0

    lines: list[str] = [
        "# LinkedIn profile analysis summary",
        "",
        f"- Source file: `{source_path}`",
        f"- Reference year for ongoing roles: `{reference_year}`",
        f"- People analyzed: **{n_people}**",
        f"- Total profile records analyzed: **{n_records}**",
        f"- People with experience records: **{exp_people}**",
        f"- People with education records: **{edu_people}**",
        f"- Median total experience per person: **{median_years:.2f} years**",
        f"- 75th percentile total experience: **{p75_years:.2f} years**",
        "",
        f"## Top {top_n} education institutions",
        "",
    ]

    if edu_dist.empty:
        lines.append("- No education institutions found in the input.")
    else:
        for row in edu_dist.head(top_n).itertuples(index=False):
            lines.append(f"- {row.institution}: {row.unique_people} people ({row.records} records)")

    lines.extend(["", f"## Top {top_n} prior organizations", ""])
    if exp_dist.empty:
        lines.append("- No experience organizations found in the input.")
    else:
        for row in exp_dist.head(top_n).itertuples(index=False):
            lines.append(
                f"- {row.organization}: {row.unique_people} people, {row.total_years:.2f} cumulative years ({row.records} records)"
            )

    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Experience years are approximated from `end_year - start_year`.",
            "- Ongoing roles (`Present`, `Current`, `Now`) use the provided reference year.",
            "- Results are only as good as the manually captured profile records.",
        ]
    )
    return "\n".join(lines) + "\n"


def run_analysis(input_path: Path, outdir: Path, reference_year: int, top_n: int) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    records = load_profiles(input_path, reference_year=reference_year)
    people = build_people_summary(records)
    edu_dist = build_education_distribution(records)
    exp_dist = build_experience_distribution(records)
    exp_by_person_org = build_experience_by_person_org(records)

    records.to_csv(outdir / "linkedin_normalized_records.csv", index=False)
    people.to_csv(outdir / "linkedin_people_summary.csv", index=False)
    edu_dist.to_csv(outdir / "linkedin_education_distribution.csv", index=False)
    exp_dist.to_csv(outdir / "linkedin_experience_distribution.csv", index=False)
    exp_by_person_org.to_csv(outdir / "linkedin_experience_by_person_org.csv", index=False)

    summary = build_summary_markdown(
        records=records,
        people=people,
        edu_dist=edu_dist,
        exp_dist=exp_dist,
        source_path=input_path,
        reference_year=reference_year,
        top_n=top_n,
    )
    (outdir / "linkedin_profile_summary.md").write_text(summary, encoding="utf-8")

    print(f"Analysis complete. Output written to: {outdir}")
    print(f"- normalized records: {outdir / 'linkedin_normalized_records.csv'}")
    print(f"- people summary: {outdir / 'linkedin_people_summary.csv'}")
    print(f"- education distribution: {outdir / 'linkedin_education_distribution.csv'}")
    print(f"- experience distribution: {outdir / 'linkedin_experience_distribution.csv'}")
    print(f"- experience by person and org: {outdir / 'linkedin_experience_by_person_org.csv'}")
    print(f"- markdown summary: {outdir / 'linkedin_profile_summary.md'}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze manually collected profile education/experience records."
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Path to input CSV containing profile records.",
    )
    parser.add_argument(
        "--outdir",
        type=Path,
        default=Path("outputs/linkedin_profile_analysis"),
        help="Directory to write analysis outputs.",
    )
    parser.add_argument(
        "--reference-year",
        type=int,
        default=date.today().year,
        help="Year used for ongoing experiences (e.g., 'Present').",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=10,
        help="Number of top institutions/organizations shown in markdown summary.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_analysis(
        input_path=args.input,
        outdir=args.outdir,
        reference_year=args.reference_year,
        top_n=args.top_n,
    )


if __name__ == "__main__":
    main()
