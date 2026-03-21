from __future__ import annotations

import argparse
import io
import json
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
from pypdf import PdfReader


OECD_CRS_DATAFLOW_URL = (
    "https://sdmx.oecd.org/public/rest/dataflow/"
    "OECD.DCD.FSD/DSD_CRS@DF_CRS/1.5?references=all"
)
WORLD_BANK_COEFFICIENTS_URL = "https://devinit.github.io/media/documents/WB_with_climate_coefficients.xlsx"
AIIB_ALL_PROJECTS_JS_URL = "https://www.aiib.org/en/projects/list/.content/all-projects-data.js"
IDB_2023_CLIMATE_CSV_URL = "https://data.iadb.org/file/download/5f463558-6c7f-41ac-9ab9-6b08bc0e3df5"
IDB_2024_CLIMATE_CSV_URL = "https://data.iadb.org/file/download/bc40fafc-4241-4d9a-afe6-e28a56a17697"
# CDB publishes quarterly climate-loan disclosures as PDFs.
CDB_DISCLOSURE_PDFS = {
    "2024Q1": "https://www.cdb.com.cn/xwzx/xxgg/qtgg/202407/W020240729639508752090.pdf",
    "2024Q3": "https://www.cdb.com.cn/xwzx/xxgg/qtgg/202501/W020250113626252031243.pdf",
    "2025Q1": "https://www.cdb.cn/xwzx/xxgg/qtgg/202506/W020250618525732514513.pdf",
}


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


def coalesce_columns(df: pd.DataFrame, columns: list[str], default: object = np.nan) -> pd.Series:
    available = [col for col in columns if col in df.columns]
    if not available:
        return pd.Series([default] * len(df), index=df.index)
    out = df[available[0]]
    for col in available[1:]:
        out = out.where(out.notna(), df[col])
    return out


def coalesce_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:
    if not df.columns.duplicated().any():
        return df
    unique_names = pd.Index(df.columns).unique()
    out = pd.DataFrame(index=df.index)
    for name in unique_names:
        matches = np.where(df.columns == name)[0]
        if len(matches) == 1:
            out[name] = df.iloc[:, matches[0]]
            continue
        series = df.iloc[:, matches[0]]
        for pos in matches[1:]:
            series = series.where(series.notna(), df.iloc[:, pos])
        out[name] = series
    return out


def extract_first_float(text: str) -> float | None:
    if text is None:
        return None
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(text))
    if not m:
        return None
    return float(m.group(0).replace(",", ""))


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


def load_idb_projects(session: requests.Session, cache_dir: Path) -> pd.DataFrame:
    files = [
        ("2023", IDB_2023_CLIMATE_CSV_URL, cache_dir / "idb" / "idb_2023_climate.csv"),
        ("2024", IDB_2024_CLIMATE_CSV_URL, cache_dir / "idb" / "idb_2024_climate.csv"),
    ]
    frames: list[pd.DataFrame] = []
    for _, url, path in files:
        csv_path = download_to_cache(session, url, path)
        frames.append(pd.read_csv(csv_path))
    idb = pd.concat(frames, ignore_index=True)
    idb = idb.rename(columns=lambda c: str(c).strip())

    # Normalize column variants across years.
    col_map = {
        "Instrument Type (1)": "instrument_type",
        "Instrument Type": "instrument_type",
        "Project Number": "project_number",
        "Project Name": "project_name",
        "Country": "country",
        "Approval Year": "approval_year",
        "Approved Amount": "approved_amount",
        "Original Approved Amount": "approved_amount",
        "Use": "use",
        "Mitigation sector": "mitigation_sector",
        "Mitigation Sector": "mitigation_sector",
        "Adaptation sector": "adaptation_sector",
        "Adaptation Sector": "adaptation_sector",
        "US$ Mitigation": "usd_mitigation",
        "US$ Adaptation": "usd_adaptation",
        "US$ Dual-use": "usd_dual",
        "Climate Finance Amount": "usd_total_cf",
        "CF": "usd_total_cf",
    }
    idb = idb.rename(columns={k: v for k, v in col_map.items() if k in idb.columns})
    # Coalesce duplicate normalized columns produced by 2023 vs 2024 schema variants.
    idb = coalesce_duplicate_columns(idb)

    for col in ["usd_mitigation", "usd_adaptation", "usd_dual", "usd_total_cf", "approved_amount"]:
        if col not in idb.columns:
            idb[col] = np.nan
        if isinstance(idb[col], pd.DataFrame):
            idb[col] = idb[col].bfill(axis=1).iloc[:, 0]
        idb[col] = (
            idb[col]
            .astype(str)
            .str.replace(r"[\$, ]", "", regex=True)
            .replace({"-": np.nan, "nan": np.nan, "None": np.nan, "": np.nan})
        )
        idb[col] = pd.to_numeric(idb[col], errors="coerce")

    idb["usd_mitigation"] = idb["usd_mitigation"].fillna(0.0)
    idb["usd_adaptation"] = idb["usd_adaptation"].fillna(0.0)
    idb["usd_dual"] = idb["usd_dual"].fillna(0.0)
    idb["usd_total_cf"] = idb["usd_total_cf"].fillna(idb["usd_mitigation"] + idb["usd_adaptation"] + idb["usd_dual"])
    idb = idb[idb["usd_total_cf"] > 0].copy()
    if idb.empty:
        return pd.DataFrame()

    objective = np.where(
        idb["usd_dual"] > 0,
        "both",
        np.where(idb["usd_mitigation"] > 0, "mitigation", np.where(idb["usd_adaptation"] > 0, "adaptation", "none")),
    )
    idb = idb[objective != "none"].copy()
    objective = np.where(
        idb["usd_dual"] > 0,
        "both",
        np.where(idb["usd_mitigation"] > 0, "mitigation", "adaptation"),
    )
    objective_amount = np.where(
        objective == "both",
        idb["usd_dual"],
        np.where(objective == "mitigation", idb["usd_mitigation"], idb["usd_adaptation"]),
    )

    sector_text = idb["mitigation_sector"].fillna("").astype(str) + " " + idb["adaptation_sector"].fillna("").astype(str)
    out = pd.DataFrame(
        {
            "source_dataset": "IDB_CLIMATE_DATA_DIRECT",
            "source_url": np.where(
                pd.to_numeric(idb["approval_year"], errors="coerce").fillna(0).astype(int) >= 2024,
                IDB_2024_CLIMATE_CSV_URL,
                IDB_2023_CLIMATE_CSV_URL,
            ),
            "actor": "Inter-American Development Bank",
            "project_id": "IDB-" + idb["project_number"].fillna("").astype(str),
            "project_name": idb["project_name"].fillna("").astype(str),
            "country": idb["country"].fillna("Unspecified").astype(str),
            "year": pd.to_numeric(idb["approval_year"], errors="coerce"),
            "instrument": map_text_instrument(idb["instrument_type"].fillna("").astype(str)),
            "objective": objective,
            "sector": sector_text.map(map_sector),
            "raw_sector": sector_text,
            "raw_instrument": idb["instrument_type"].fillna("").astype(str),
            "commitment_amount_usd_m": pd.to_numeric(idb["approved_amount"], errors="coerce") / 1_000_000.0,
            "climate_amount_usd_m": objective_amount / 1_000_000.0,
            "mitigation_amount_usd_m": np.where(objective == "mitigation", idb["usd_mitigation"] / 1_000_000.0, 0.0),
            "adaptation_amount_usd_m": np.where(objective == "adaptation", idb["usd_adaptation"] / 1_000_000.0, 0.0),
            "both_amount_usd_m": np.where(objective == "both", idb["usd_dual"] / 1_000_000.0, 0.0),
        }
    )
    return out


def parse_aiib_project_js(text: str) -> list[dict]:
    m = re.search(r"var\s+data\s*=\s*(\[.*\])\s*;", text, re.S)
    if not m:
        return []
    payload = m.group(1)
    payload = re.sub(r",\s*]", "]", payload)
    payload = re.sub(r",\s*}", "}", payload)
    return json.loads(payload)


def parse_aiib_funding_musd(value: str) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    # Examples: "USD500 million", "USD266.64 million"
    match = re.search(r"USD\s*([\d,]+(?:\.\d+)?)\s*million", text, flags=re.I)
    if match:
        return float(match.group(1).replace(",", ""))
    number = extract_first_float(text)
    return float(number) if number is not None else 0.0


def load_aiib_projects(session: requests.Session, cache_dir: Path) -> pd.DataFrame:
    js_path = download_to_cache(session, AIIB_ALL_PROJECTS_JS_URL, cache_dir / "aiib" / "all-projects-data.js")
    text = js_path.read_text(encoding="utf-8", errors="ignore")
    records = parse_aiib_project_js(text)
    if not records:
        return pd.DataFrame()

    aiib = pd.DataFrame(records)
    aiib["approved_musd"] = aiib["approved_funding"].map(parse_aiib_funding_musd)
    aiib["committed_musd"] = aiib["committed_funding"].map(parse_aiib_funding_musd)
    aiib["proposed_musd"] = aiib["proposed_funding"].map(parse_aiib_funding_musd)
    aiib["climate_flag"] = (
        aiib["name"].fillna("").str.contains(
            r"climate|renewable|solar|wind|hydro|green|resilience|adaptation|mitigation|emission|water|flood|transport|rail|metro|energy",
            case=False,
            regex=True,
        )
        | aiib["sector"].fillna("").str.contains(
            r"energy|transport|water|climate|multi-sector|environment",
            case=False,
            regex=True,
        )
    )
    aiib = aiib[aiib["climate_flag"]].copy()
    if aiib.empty:
        return pd.DataFrame()

    aiib["year"] = pd.to_numeric(aiib["date"].astype(str).str.extract(r"(\d{4})")[0], errors="coerce")
    aiib["amount_musd"] = np.where(aiib["approved_musd"] > 0, aiib["approved_musd"], aiib["committed_musd"])
    aiib["amount_musd"] = np.where(aiib["amount_musd"] > 0, aiib["amount_musd"], aiib["proposed_musd"])
    aiib = aiib[aiib["amount_musd"] > 0].copy()
    if aiib.empty:
        return pd.DataFrame()

    objective = np.where(
        aiib["name"].str.contains(r"adapt|resilien|flood", case=False, regex=True)
        & aiib["name"].str.contains(r"mitig|renewable|solar|wind|energy efficiency|emission", case=False, regex=True),
        "both",
        np.where(
            aiib["name"].str.contains(r"adapt|resilien|flood", case=False, regex=True),
            "adaptation",
            "mitigation",
        ),
    )

    out = pd.DataFrame(
        {
            "source_dataset": "AIIB_DIRECT_PROJECT_LIST",
            "source_url": AIIB_ALL_PROJECTS_JS_URL,
            "actor": "Asian Infrastructure Investment Bank",
            "project_id": "AIIB-" + aiib["path"].fillna("").astype(str),
            "project_name": aiib["name"].fillna("").astype(str).str.strip(),
            "country": aiib["economy"].fillna("Unspecified").astype(str),
            "year": aiib["year"],
            "instrument": map_text_instrument(aiib["financing_type"].fillna("").astype(str)),
            "objective": objective,
            "sector": aiib["sector"].fillna("").map(map_sector),
            "raw_sector": aiib["sector"].fillna("").astype(str),
            "raw_instrument": aiib["financing_type"].fillna("").astype(str),
            "commitment_amount_usd_m": aiib["amount_musd"],
            "climate_amount_usd_m": aiib["amount_musd"],
            "mitigation_amount_usd_m": np.where(objective == "mitigation", aiib["amount_musd"], 0.0),
            "adaptation_amount_usd_m": np.where(objective == "adaptation", aiib["amount_musd"], 0.0),
            "both_amount_usd_m": np.where(objective == "both", aiib["amount_musd"], 0.0),
        }
    )
    return out


def extract_cdb_row_text(text: str, row_name: str) -> str | None:
    m = re.search(rf"{row_name}\s+([^\n]+)", text)
    if not m:
        return None
    return m.group(1).strip()


def load_cdb_climate_disclosures(session: requests.Session, cache_dir: Path) -> pd.DataFrame:
    records: list[dict] = []
    for period, url in CDB_DISCLOSURE_PDFS.items():
        pdf_path = download_to_cache(session, url, cache_dir / "cdb" / f"{period}.pdf")
        reader = PdfReader(str(pdf_path))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
        row = extract_cdb_row_text(text, "合计")
        if not row:
            continue
        values = re.findall(r"-?\d[\d,]*(?:\.\d+)?", row)
        # Row contains repeated blocks; use first quarter block where available.
        # Expected first block: projects, loan_amount(万元), rate, annual_CO2e
        if len(values) < 4:
            continue
        projects_count = float(values[0].replace(",", ""))
        loan_amount_wan = float(values[1].replace(",", ""))
        annual_reduction_tco2e = float(values[3].replace(",", ""))
        year_match = re.search(r"(\d{4})", period)
        year = int(year_match.group(1)) if year_match else None

        records.append(
            {
                "source_dataset": "CDB_QUARTERLY_CLIMATE_DISCLOSURE",
                "source_url": url,
                "actor": "China Development Bank",
                "project_id": f"CDB-{period}-AGG",
                "project_name": f"CDB carbon-reduction loan disclosure {period}",
                "country": "China",
                "year": year,
                "instrument": "debt",
                "objective": "mitigation",
                "sector": "energy",
                "raw_sector": "carbon reduction lending (aggregate disclosure)",
                "raw_instrument": "loan",
                "commitment_amount_usd_m": loan_amount_wan * 10_000.0 / 1_000_000.0,
                "climate_amount_usd_m": loan_amount_wan * 10_000.0 / 1_000_000.0,
                "mitigation_amount_usd_m": loan_amount_wan * 10_000.0 / 1_000_000.0,
                "adaptation_amount_usd_m": 0.0,
                "both_amount_usd_m": 0.0,
                "cdb_projects_count": projects_count,
                "cdb_annual_reduction_tco2e": annual_reduction_tco2e,
            }
        )
    if not records:
        return pd.DataFrame()
    out = pd.DataFrame(records)
    for col in ["cdb_projects_count", "cdb_annual_reduction_tco2e"]:
        if col not in out.columns:
            out[col] = np.nan
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
        f"- IDB direct climate datasets: {IDB_2023_CLIMATE_CSV_URL}, {IDB_2024_CLIMATE_CSV_URL}",
        f"- AIIB direct project list feed: {AIIB_ALL_PROJECTS_JS_URL}",
        f"- CDB climate loan disclosures (PDF): {list(CDB_DISCLOSURE_PDFS.values())}",
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
        "- AIIB objective tags are inferred from project title/sector keywords due to no explicit adaptation/mitigation split in the source feed.",
        "- CDB data is currently aggregate climate-loan disclosure rows (quarterly), not fully project-level line items.",
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
    idb_projects = load_idb_projects(session=session, cache_dir=cache_dir)
    aiib_projects = load_aiib_projects(session=session, cache_dir=cache_dir)
    cdb_projects = load_cdb_climate_disclosures(session=session, cache_dir=cache_dir)

    projects = pd.concat(
        [oecd_projects, wb_projects, idb_projects, aiib_projects, cdb_projects],
        ignore_index=True,
    )
    projects = projects.replace([np.inf, -np.inf], np.nan)
    projects["climate_amount_usd_m"] = pd.to_numeric(projects["climate_amount_usd_m"], errors="coerce").fillna(0.0)
    projects = projects[projects["climate_amount_usd_m"] > 0].copy()
    projects["year"] = pd.to_numeric(projects["year"], errors="coerce").astype("Int64")

    for col in ["commitment_amount_usd_m", "mitigation_amount_usd_m", "adaptation_amount_usd_m", "both_amount_usd_m"]:
        projects[col] = pd.to_numeric(projects[col], errors="coerce").fillna(0.0)
    for extra_col in ["cdb_projects_count", "cdb_annual_reduction_tco2e"]:
        if extra_col not in projects.columns:
            projects[extra_col] = np.nan

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
