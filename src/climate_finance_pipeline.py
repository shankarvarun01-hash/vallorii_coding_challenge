from __future__ import annotations

import argparse
import io
import re
import sqlite3
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests


OECD_CRS_DATAFLOW_URL = (
    "https://sdmx.oecd.org/public/rest/dataflow/"
    "OECD.DCD.FSD/DSD_CRS@DF_CRS/1.5?references=all"
)
WORLD_BANK_COEFFICIENTS_URL = "https://devinit.github.io/media/documents/WB_with_climate_coefficients.xlsx"
MDB_DATASET_URL = (
    "https://www.publishwhatyoufund.org/app/uploads/dlm_uploads/2026/03/"
    "mdb-climate-finance-dataset-2.0.xlsx"
)


SECTOR_KEYWORDS: dict[str, tuple[str, ...]] = {
    "energy": (
        "energy",
        "electric",
        "power",
        "grid",
        "renewable",
        "solar",
        "wind",
        "hydro",
        "geothermal",
        "battery",
        "clean cooking",
    ),
    "transport": (
        "transport",
        "road",
        "rail",
        "metro",
        "bus",
        "mobility",
        "port",
        "airport",
        "shipping",
        "traffic",
    ),
    "water": (
        "water",
        "sanitation",
        "wastewater",
        "irrigation",
        "drainage",
        "flood",
        "river basin",
        "desalination",
    ),
    "buildings": (
        "building",
        "housing",
        "urban development",
        "municipal services",
        "city",
        "real estate",
    ),
    "agriculture_land_use": (
        "agriculture",
        "forestry",
        "land use",
        "crop",
        "livestock",
        "fish",
        "fisher",
        "agri",
        "pasture",
        "forest",
    ),
    "industry_mining": (
        "industry",
        "industrial",
        "manufacturing",
        "mining",
        "extractive",
        "cement",
        "steel",
    ),
    "waste": ("waste", "solid waste", "landfill", "circular economy"),
    "health": ("health", "hospital", "disease"),
    "education": ("education", "school", "training"),
    "finance_policy": (
        "financial",
        "banking",
        "policy",
        "public administration",
        "budget support",
        "governance",
        "institutional",
        "insurance",
    ),
}


@dataclass
class PipelineResult:
    projects: pd.DataFrame
    flow_edges: pd.DataFrame


def requests_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        }
    )
    return session


def download_to_cache(session: requests.Session, url: str, cache_path: Path, force: bool = False) -> Path:
    if cache_path.exists() and not force:
        return cache_path
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    resp = session.get(url, timeout=180)
    resp.raise_for_status()
    cache_path.write_bytes(resp.content)
    return cache_path


def rio_coefficient(marker_value: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(marker_value, errors="coerce").fillna(0.0)
    return np.where(numeric >= 2.0, 1.0, np.where(numeric >= 1.0, 0.4, 0.0))


def infer_objective(mit_amount: pd.Series, adp_amount: pd.Series) -> pd.Series:
    mit_positive = mit_amount.fillna(0.0) > 0
    adp_positive = adp_amount.fillna(0.0) > 0
    return np.select(
        [mit_positive & adp_positive, mit_positive, adp_positive],
        ["both", "mitigation", "adaptation"],
        default="none",
    )


def map_sector(text: str) -> str:
    lowered = str(text).lower()
    for sector, keywords in SECTOR_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return sector
    return "other"


def map_oecd_instrument(finance_code: pd.Series) -> pd.Series:
    cleaned = (
        finance_code.astype("string")
        .str.replace(r"\.0$", "", regex=True)
        .str.strip()
        .fillna("")
    )
    return np.select(
        [
            cleaned.str.startswith("1"),
            cleaned.str.startswith("4"),
            cleaned.str.startswith("5"),
            cleaned.str.startswith("6"),
        ],
        ["grant", "debt", "equity", "guarantee"],
        default="other",
    )


def map_text_instrument(text: pd.Series) -> pd.Series:
    lowered = text.fillna("").astype(str).str.lower()
    return np.select(
        [
            lowered.str.contains("grant|donation|technical assistance|ta"),
            lowered.str.contains("equity|share"),
            lowered.str.contains("guarantee|risk transfer"),
            lowered.str.contains("loan|credit|debt|bond|financing|ibrd|ida"),
        ],
        ["grant", "equity", "guarantee", "debt"],
        default="other",
    )


def discover_oecd_crs_urls(session: requests.Session) -> dict[int, str]:
    resp = session.get(OECD_CRS_DATAFLOW_URL, timeout=60)
    resp.raise_for_status()

    ns = {"c": "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common"}
    root = ET.fromstring(resp.content)

    year_urls: dict[int, str] = {}
    for annotation in root.findall(".//c:Annotation", ns):
        annotation_type = annotation.find("c:AnnotationType", ns)
        if annotation_type is None or (annotation_type.text or "").strip() != "EXT_RESOURCE":
            continue
        for annotation_text in annotation.findall("c:AnnotationText", ns):
            lang = annotation_text.attrib.get("{http://www.w3.org/XML/1998/namespace}lang")
            text = (annotation_text.text or "").strip()
            if lang != "en" or "CRS " not in text or "dotStat format" not in text:
                continue
            parts = text.split("|")
            if len(parts) != 2:
                continue
            label, url = parts
            year_match = re.search(r"CRS (\d{4})", label)
            if year_match:
                year_urls[int(year_match.group(1))] = url
    if not year_urls:
        raise RuntimeError("Could not discover OECD CRS annual download URLs from dataflow metadata.")
    return year_urls


def load_oecd_projects(
    session: requests.Session,
    cache_dir: Path,
    years: Iterable[int],
) -> pd.DataFrame:
    urls_by_year = discover_oecd_crs_urls(session)
    missing_years = sorted([year for year in years if year not in urls_by_year])
    if missing_years:
        raise ValueError(f"OECD CRS URLs not found for year(s): {missing_years}")

    all_chunks: list[pd.DataFrame] = []
    usecols = [
        "Year",
        "DonorName",
        "AgencyName",
        "CrsID",
        "ProjectNumber",
        "RecipientName",
        "USD_Commitment",
        "ProjectTitle",
        "PurposeName",
        "SectorName",
        "FlowName",
        "Finance_T",
        "Aid_T",
        "ClimateMitigation",
        "ClimateAdaptation",
    ]

    for year in years:
        zip_path = download_to_cache(
            session,
            urls_by_year[year],
            cache_dir / "oecd" / f"CRS_{year}.zip",
        )
        with zipfile.ZipFile(zip_path) as zf:
            members = zf.namelist()
            if not members:
                continue
            with zf.open(members[0]) as handle:
                reader = pd.read_csv(
                    handle,
                    sep="|",
                    quotechar='"',
                    usecols=usecols,
                    low_memory=False,
                    chunksize=100_000,
                )
                for chunk in reader:
                    commitment = pd.to_numeric(chunk["USD_Commitment"], errors="coerce")
                    chunk = chunk[commitment > 0].copy()
                    if chunk.empty:
                        continue

                    chunk = chunk[chunk["ProjectTitle"].notna()].copy()
                    if chunk.empty:
                        continue

                    mit_coeff = rio_coefficient(chunk["ClimateMitigation"])
                    adp_coeff = rio_coefficient(chunk["ClimateAdaptation"])
                    objective = infer_objective(pd.Series(mit_coeff), pd.Series(adp_coeff))
                    chunk = chunk[objective != "none"].copy()
                    if chunk.empty:
                        continue

                    mit_coeff = rio_coefficient(chunk["ClimateMitigation"])
                    adp_coeff = rio_coefficient(chunk["ClimateAdaptation"])
                    objective = infer_objective(pd.Series(mit_coeff), pd.Series(adp_coeff))
                    objective_share = np.where(
                        objective == "both",
                        np.maximum(mit_coeff, adp_coeff),
                        np.where(objective == "mitigation", mit_coeff, adp_coeff),
                    )
                    climate_amount = pd.to_numeric(chunk["USD_Commitment"], errors="coerce").fillna(0.0) * objective_share

                    actor_base = chunk["DonorName"].fillna("").astype(str).str.strip()
                    agency = chunk["AgencyName"].fillna("").astype(str).str.strip()
                    actor = np.where(
                        (agency != "") & (agency != actor_base),
                        actor_base + " / " + agency,
                        actor_base,
                    )

                    sector_text = (
                        chunk["SectorName"].fillna("").astype(str)
                        + " "
                        + chunk["PurposeName"].fillna("").astype(str)
                        + " "
                        + chunk["ProjectTitle"].fillna("").astype(str)
                    )
                    out = pd.DataFrame(
                        {
                            "source_dataset": "OECD_CRS",
                            "source_url": urls_by_year[year],
                            "actor": actor,
                            "project_id": (
                                "OECD-"
                                + chunk["Year"].astype("Int64").astype(str)
                                + "-"
                                + chunk["CrsID"].fillna("").astype(str)
                                + "-"
                                + chunk["ProjectNumber"].fillna("").astype(str)
                            ),
                            "project_name": chunk["ProjectTitle"].astype(str),
                            "country": chunk["RecipientName"].fillna("Unspecified").astype(str),
                            "year": pd.to_numeric(chunk["Year"], errors="coerce"),
                            "instrument": map_oecd_instrument(chunk["Finance_T"]),
                            "objective": objective,
                            "sector": sector_text.map(map_sector),
                            "raw_sector": chunk["SectorName"].fillna("").astype(str),
                            "raw_instrument": chunk["Finance_T"].astype("string"),
                            "commitment_amount_usd_m": pd.to_numeric(chunk["USD_Commitment"], errors="coerce"),
                            "climate_amount_usd_m": climate_amount,
                            "mitigation_amount_usd_m": climate_amount.where(objective == "mitigation", 0.0),
                            "adaptation_amount_usd_m": climate_amount.where(objective == "adaptation", 0.0),
                            "both_amount_usd_m": climate_amount.where(objective == "both", 0.0),
                        }
                    )
                    all_chunks.append(out)

    if not all_chunks:
        return pd.DataFrame()
    return pd.concat(all_chunks, ignore_index=True)


def load_world_bank_projects(session: requests.Session, cache_dir: Path) -> pd.DataFrame:
    xlsx_path = download_to_cache(
        session,
        WORLD_BANK_COEFFICIENTS_URL,
        cache_dir / "world_bank" / "WB_with_climate_coefficients.xlsx",
    )

    wb = pd.read_excel(xlsx_path, sheet_name="WB_with_climate_coefficients")
    wb["total_clim"] = pd.to_numeric(wb["total_clim"], errors="coerce").fillna(0.0)
    wb["total_mitig"] = pd.to_numeric(wb["total_mitig"], errors="coerce").fillna(0.0)
    wb["total_adapt"] = pd.to_numeric(wb["total_adapt"], errors="coerce").fillna(0.0)
    wb = wb[wb["total_clim"] > 0].copy()
    if wb.empty:
        return pd.DataFrame()

    objective = infer_objective(wb["total_mitig"], wb["total_adapt"])
    wb = wb[objective != "none"].copy()
    objective = infer_objective(wb["total_mitig"], wb["total_adapt"])
    objective_amount = np.where(
        objective == "both",
        wb["total_clim"],
        np.where(objective == "mitigation", wb["total_mitig"], wb["total_adapt"]),
    )

    commitment = pd.to_numeric(wb["curr_total_commitment"], errors="coerce")
    fallback_commitment = pd.Series(
        np.where(
            wb["Climate_coef"].fillna(0) > 0,
            wb["total_clim"] / wb["Climate_coef"].replace(0, np.nan),
            wb["total_clim"],
        ),
        index=wb.index,
    ).fillna(wb["total_clim"])
    commitment = commitment.where(commitment.notna(), fallback_commitment)

    instrument_text = (
        wb["projectfinancialtype"].fillna("").astype(str)
        + " "
        + wb["lendinginstr"].fillna("").astype(str)
        + " "
        + np.where(pd.to_numeric(wb["grantamt"], errors="coerce").fillna(0) > 0, "grant", "")
    )
    sector_text = (
        wb["sector1"].fillna("").astype(str)
        + " "
        + wb["sector2"].fillna("").astype(str)
        + " "
        + wb["theme1"].fillna("").astype(str)
    )

    out = pd.DataFrame(
        {
            "source_dataset": "WORLD_BANK_PROJECT_CLIMATE_COEFFICIENTS",
            "source_url": WORLD_BANK_COEFFICIENTS_URL,
            "actor": "World Bank (IBRD/IDA)",
            "project_id": "WB-" + wb["id"].astype(str),
            "project_name": wb["project_name"].fillna("").astype(str),
            "country": wb["countryname"].fillna("Unspecified").astype(str),
            "year": pd.to_numeric(wb["commitment_year"], errors="coerce"),
            "instrument": map_text_instrument(instrument_text),
            "objective": objective,
            "sector": sector_text.map(map_sector),
            "raw_sector": wb["sector1"].fillna("").astype(str),
            "raw_instrument": wb["lendinginstr"].fillna("").astype(str),
            "commitment_amount_usd_m": commitment,
            "climate_amount_usd_m": objective_amount,
            "mitigation_amount_usd_m": wb["total_mitig"].where(objective == "mitigation", 0.0),
            "adaptation_amount_usd_m": wb["total_adapt"].where(objective == "adaptation", 0.0),
            "both_amount_usd_m": wb["total_clim"].where(objective == "both", 0.0),
        }
    )
    return out


def load_mdb_projects(session: requests.Session, cache_dir: Path) -> pd.DataFrame:
    xlsx_path = download_to_cache(
        session,
        MDB_DATASET_URL,
        cache_dir / "mdb" / "mdb-climate-finance-dataset-2.0.xlsx",
    )
    mdb = pd.read_excel(xlsx_path, sheet_name="1. Enriched Dataset")
    mdb = mdb.rename(columns=lambda c: str(c).strip())

    climate_amount = pd.to_numeric(mdb["Climate finance ($ million)"], errors="coerce").fillna(0.0)
    mdb = mdb[climate_amount > 0].copy()
    if mdb.empty:
        return pd.DataFrame()

    # Keep ADB and other MDBs/DFIs, while avoiding overlap with dedicated WB source.
    mdb = mdb[~mdb["MDB"].fillna("").astype(str).str.contains(r"\bWB\b|World Bank", case=False, regex=True)].copy()
    if mdb.empty:
        return pd.DataFrame()

    climate_amount = pd.to_numeric(mdb["Climate finance ($ million)"], errors="coerce").fillna(0.0)
    mitigation = pd.to_numeric(mdb["Mitigation ($ million)"], errors="coerce").fillna(0.0)
    adaptation = pd.to_numeric(mdb["Adaptation ($ million)"], errors="coerce").fillna(0.0)
    objective = infer_objective(mitigation, adaptation)
    objective_text = mdb["Type"].fillna("").astype(str).str.lower()
    objective = np.where(objective_text.str.contains("dual|both"), "both", objective)
    objective = np.where(objective_text.str.contains("mitig"), "mitigation", objective)
    objective = np.where(objective_text.str.contains("adapt"), "adaptation", objective)

    objective_amount = np.where(
        objective == "both",
        climate_amount,
        np.where(objective == "mitigation", mitigation, adaptation),
    )

    instrument = map_text_instrument(mdb["Investment instrument"].fillna("").astype(str))
    sector_text = (
        mdb["Sector 1"].fillna("").astype(str)
        + " "
        + mdb["Sector 2"].fillna("").astype(str)
        + " "
        + mdb["Mitigation sector"].fillna("").astype(str)
        + " "
        + mdb["Adaptation sector"].fillna("").astype(str)
    )

    out = pd.DataFrame(
        {
            "source_dataset": "MDB_PUBLIC_DISCLOSURES",
            "source_url": MDB_DATASET_URL,
            "actor": mdb["MDB"].fillna("Unspecified").astype(str),
            "project_id": "MDB-" + mdb["MDB"].fillna("").astype(str) + "-" + mdb["Project ID"].fillna("").astype(str),
            "project_name": mdb["Project name"].fillna("").astype(str),
            "country": mdb["Country"].fillna("Unspecified").astype(str),
            "year": pd.to_numeric(mdb["Approval / reporting year"], errors="coerce"),
            "instrument": instrument,
            "objective": objective,
            "sector": sector_text.map(map_sector),
            "raw_sector": mdb["Sector 1"].fillna("").astype(str),
            "raw_instrument": mdb["Investment instrument"].fillna("").astype(str),
            "commitment_amount_usd_m": pd.to_numeric(mdb["Total commitment ($ million)"], errors="coerce"),
            "climate_amount_usd_m": objective_amount,
            "mitigation_amount_usd_m": mitigation.where(objective == "mitigation", 0.0),
            "adaptation_amount_usd_m": adaptation.where(objective == "adaptation", 0.0),
            "both_amount_usd_m": climate_amount.where(objective == "both", 0.0),
        }
    )
    out = out[out["objective"].isin(["mitigation", "adaptation", "both"])].copy()
    return out


def build_flow_edges(projects: pd.DataFrame) -> pd.DataFrame:
    actor_to_instrument = (
        projects.groupby(["actor", "instrument"], as_index=False)["climate_amount_usd_m"].sum()
        .rename(columns={"actor": "from_node", "instrument": "to_node", "climate_amount_usd_m": "amount_usd_m"})
        .assign(stage="actor_to_instrument")
    )
    instrument_to_objective = (
        projects.groupby(["instrument", "objective"], as_index=False)["climate_amount_usd_m"].sum()
        .rename(columns={"instrument": "from_node", "objective": "to_node", "climate_amount_usd_m": "amount_usd_m"})
        .assign(stage="instrument_to_objective")
    )
    objective_to_sector = (
        projects.groupby(["objective", "sector"], as_index=False)["climate_amount_usd_m"].sum()
        .rename(columns={"objective": "from_node", "sector": "to_node", "climate_amount_usd_m": "amount_usd_m"})
        .assign(stage="objective_to_sector")
    )
    return pd.concat([actor_to_instrument, instrument_to_objective, objective_to_sector], ignore_index=True)


def write_sankey(projects: pd.DataFrame, outpath: Path, top_actors: int = 20, top_sectors: int = 12) -> None:
    if projects.empty:
        return

    df = projects.copy()
    actor_rank = (
        df.groupby("actor", as_index=False)["climate_amount_usd_m"]
        .sum()
        .sort_values("climate_amount_usd_m", ascending=False)
    )
    top_actor_set = set(actor_rank.head(top_actors)["actor"])
    df["actor_plot"] = np.where(df["actor"].isin(top_actor_set), df["actor"], "Other actors")

    sector_rank = (
        df.groupby("sector", as_index=False)["climate_amount_usd_m"]
        .sum()
        .sort_values("climate_amount_usd_m", ascending=False)
    )
    top_sector_set = set(sector_rank.head(top_sectors)["sector"])
    df["sector_plot"] = np.where(df["sector"].isin(top_sector_set), df["sector"], "other")

    links = pd.concat(
        [
            df.groupby(["actor_plot", "instrument"], as_index=False)["climate_amount_usd_m"]
            .sum()
            .rename(columns={"actor_plot": "source", "instrument": "target", "climate_amount_usd_m": "value"}),
            df.groupby(["instrument", "objective"], as_index=False)["climate_amount_usd_m"]
            .sum()
            .rename(columns={"instrument": "source", "objective": "target", "climate_amount_usd_m": "value"}),
            df.groupby(["objective", "sector_plot"], as_index=False)["climate_amount_usd_m"]
            .sum()
            .rename(columns={"objective": "source", "sector_plot": "target", "climate_amount_usd_m": "value"}),
        ],
        ignore_index=True,
    )
    links = links[links["value"] > 0].copy()
    if links.empty:
        return

    actor_nodes = actor_rank["actor"].where(actor_rank["actor"].isin(top_actor_set), "Other actors").drop_duplicates().tolist()
    instrument_nodes = ["grant", "debt", "equity", "guarantee", "other"]
    objective_nodes = ["mitigation", "adaptation", "both"]
    sector_nodes = (
        sector_rank["sector"].where(sector_rank["sector"].isin(top_sector_set), "other").drop_duplicates().tolist()
    )
    nodes = actor_nodes + instrument_nodes + objective_nodes + sector_nodes
    node_index = {label: idx for idx, label in enumerate(nodes)}

    links["source_idx"] = links["source"].map(node_index)
    links["target_idx"] = links["target"].map(node_index)
    links = links[links["source_idx"].notna() & links["target_idx"].notna()].copy()

    fig = go.Figure(
        data=[
            go.Sankey(
                arrangement="snap",
                node=dict(
                    pad=15,
                    thickness=18,
                    line=dict(color="rgba(0,0,0,0.2)", width=0.5),
                    label=nodes,
                ),
                link=dict(
                    source=links["source_idx"].astype(int),
                    target=links["target_idx"].astype(int),
                    value=links["value"],
                ),
            )
        ]
    )
    fig.update_layout(
        title="Climate finance flow: actor -> instrument -> objective -> sector (USD million)",
        font=dict(size=11),
    )
    outpath.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(outpath, include_plotlyjs="cdn")


def write_sqlite(projects: pd.DataFrame, flow_edges: pd.DataFrame, sqlite_path: Path) -> None:
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(sqlite_path) as con:
        projects.to_sql("project_flows", con, index=False, if_exists="replace")
        flow_edges.to_sql("flow_edges", con, index=False, if_exists="replace")

        actor_summary = (
            projects.groupby("actor", as_index=False)["climate_amount_usd_m"]
            .sum()
            .sort_values("climate_amount_usd_m", ascending=False)
        )
        actor_summary.to_sql("summary_actor", con, index=False, if_exists="replace")

        objective_summary = projects.groupby("objective", as_index=False)["climate_amount_usd_m"].sum()
        objective_summary.to_sql("summary_objective", con, index=False, if_exists="replace")


def write_summary_markdown(projects: pd.DataFrame, outpath: Path, selected_oecd_years: list[int]) -> None:
    total = projects["climate_amount_usd_m"].sum()
    by_source = projects.groupby("source_dataset", as_index=False)["climate_amount_usd_m"].sum()
    by_objective = projects.groupby("objective", as_index=False)["climate_amount_usd_m"].sum()
    top_actors = (
        projects.groupby("actor", as_index=False)["climate_amount_usd_m"]
        .sum()
        .sort_values("climate_amount_usd_m", ascending=False)
        .head(10)
    )
    top_sectors = (
        projects.groupby("sector", as_index=False)["climate_amount_usd_m"]
        .sum()
        .sort_values("climate_amount_usd_m", ascending=False)
        .head(10)
    )

    lines = [
        "# Climate finance database summary",
        "",
        "## Coverage",
        f"- OECD CRS project-level commitments for year(s): {selected_oecd_years}",
        f"- World Bank project-level climate coefficients dataset: {WORLD_BANK_COEFFICIENTS_URL}",
        f"- MDB/DFI public disclosure dataset (includes AsDB and other MDBs): {MDB_DATASET_URL}",
        "",
        "## Totals",
        f"- Total climate finance represented: **{total:,.1f} USD million**",
        f"- Number of project rows: **{len(projects):,}**",
        "",
        "## Climate amount by source dataset",
    ]
    for row in by_source.itertuples():
        lines.append(f"- {row.source_dataset}: {row.climate_amount_usd_m:,.1f} USD million")

    lines += ["", "## Climate amount by objective"]
    for row in by_objective.itertuples():
        lines.append(f"- {row.objective}: {row.climate_amount_usd_m:,.1f} USD million")

    lines += ["", "## Top actors by climate amount"]
    for row in top_actors.itertuples():
        lines.append(f"- {row.actor}: {row.climate_amount_usd_m:,.1f} USD million")

    lines += ["", "## Top sectors by climate amount"]
    for row in top_sectors.itertuples():
        lines.append(f"- {row.sector}: {row.climate_amount_usd_m:,.1f} USD million")

    lines += [
        "",
        "## Method notes",
        "- OECD climate amounts are estimated from Rio markers using coefficients: principal=100%, significant=40%.",
        "- Objective classification is mutually exclusive (`mitigation`, `adaptation`, `both`) for flow visualisation.",
        "- Instrument classes are normalized to `grant`, `debt`, `equity`, `guarantee`, `other`.",
        "- Sector classes are keyword-based harmonization across source taxonomies.",
    ]

    outpath.parent.mkdir(parents=True, exist_ok=True)
    outpath.write_text("\n".join(lines))


def run_pipeline(outdir: Path, cache_dir: Path, oecd_years: list[int]) -> PipelineResult:
    session = requests_session()

    oecd_projects = load_oecd_projects(session=session, cache_dir=cache_dir, years=oecd_years)
    wb_projects = load_world_bank_projects(session=session, cache_dir=cache_dir)
    mdb_projects = load_mdb_projects(session=session, cache_dir=cache_dir)

    projects = pd.concat([oecd_projects, wb_projects, mdb_projects], ignore_index=True)
    projects = projects.replace([np.inf, -np.inf], np.nan)
    projects["climate_amount_usd_m"] = pd.to_numeric(projects["climate_amount_usd_m"], errors="coerce").fillna(0.0)
    projects = projects[projects["climate_amount_usd_m"] > 0].copy()
    projects["year"] = pd.to_numeric(projects["year"], errors="coerce").astype("Int64")

    for col in ["commitment_amount_usd_m", "mitigation_amount_usd_m", "adaptation_amount_usd_m", "both_amount_usd_m"]:
        projects[col] = pd.to_numeric(projects[col], errors="coerce").fillna(0.0)

    projects = projects.sort_values(["source_dataset", "year", "actor", "project_name"]).reset_index(drop=True)
    flow_edges = build_flow_edges(projects)

    outdir.mkdir(parents=True, exist_ok=True)
    projects.to_csv(outdir / "climate_finance_projects.csv", index=False)
    flow_edges.to_csv(outdir / "climate_finance_flow_edges.csv", index=False)
    (
        projects.groupby(["actor", "instrument", "objective", "sector"], as_index=False)["climate_amount_usd_m"]
        .sum()
        .sort_values("climate_amount_usd_m", ascending=False)
        .to_csv(outdir / "climate_finance_flow_cube.csv", index=False)
    )

    write_sqlite(projects, flow_edges, outdir / "climate_finance.db")
    write_sankey(projects, outdir / "climate_finance_sankey.html")
    write_summary_markdown(projects, outdir / "climate_finance_summary.md", selected_oecd_years=oecd_years)

    return PipelineResult(projects=projects, flow_edges=flow_edges)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a climate finance project database and flow view from OECD/WB/MDB public data."
    )
    parser.add_argument(
        "--outdir",
        type=Path,
        default=Path("outputs"),
        help="Directory to write CSV/SQLite/report outputs",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path("data/raw"),
        help="Directory for downloaded source files",
    )
    parser.add_argument(
        "--oecd-years",
        type=int,
        nargs="+",
        default=[2024, 2023],
        help="OECD CRS years to ingest (must be available in OECD metadata links)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = run_pipeline(outdir=args.outdir, cache_dir=args.cache_dir, oecd_years=args.oecd_years)
    print("Climate finance pipeline complete.")
    print(f"Projects loaded: {len(result.projects):,}")
    print(f"Total climate amount (USD million): {result.projects['climate_amount_usd_m'].sum():,.1f}")
    print("Outputs written to:", args.outdir)


if __name__ == "__main__":
    main()
