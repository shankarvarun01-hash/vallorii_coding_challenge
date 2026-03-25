from __future__ import annotations

import argparse
import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests


TEAM_URL = "https://www.climatepolicyinitiative.org/about-cpi/team/"


def make_session() -> requests.Session:
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


def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return " ".join(value.split()).strip()


def split_sentences(value: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", value) if s.strip()]


def extract_people_index(team_html: str) -> list[tuple[str, str, str]]:
    card_pattern = re.compile(
        r'<a href="(https://www\.climatepolicyinitiative\.org/people/[^"#?]+/)" target="_self">([^<]+)</a>\s*</h2>\s*<div class="post-header--meta">\s*<div class=\'meta-job_title\'>([^<]+)</div>',
        re.S,
    )
    people = [
        (html.unescape(m.group(2)).strip(), html.unescape(m.group(3)).strip(), m.group(1))
        for m in card_pattern.finditer(team_html)
    ]
    # De-duplicate by profile URL.
    return list({url: (name, role, url) for name, role, url in people}.values())


def extract_linkedin_url(page_html: str) -> str:
    links = re.findall(r'href="([^"]+)"', page_html, flags=re.I)
    for link in links:
        lowered = link.lower()
        if "linkedin.com" not in lowered:
            continue
        if "/company/" in lowered or "/admin" in lowered or "sharing/share-offsite" in lowered:
            continue
        return link
    return ""


def extract_bio_sentences(page_html: str) -> list[str]:
    paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", page_html, flags=re.I | re.S)
    cleaned = []
    for paragraph in paragraphs:
        text = clean_text(paragraph)
        if len(text) < 40:
            continue
        if any(
            token in text.lower()
            for token in [
                "var settings",
                "cookie use",
                "technical storage",
                "manage consent",
                "or link to existing content",
            ]
        ):
            continue
        cleaned.append(text)
    return split_sentences(" ".join(cleaned))


def _looks_like_degree(text: str) -> bool:
    lowered = text.lower()
    return any(
        k in lowered
        for k in [
            "phd",
            "dphil",
            "msc",
            "master",
            "mba",
            "ma",
            "ba",
            "bsc",
            "bachelor",
            "degree",
            "llm",
            "llb",
            "postgraduate",
            "diploma",
        ]
    )


def _looks_like_institution(text: str) -> bool:
    lowered = text.lower()
    return any(
        k in lowered
        for k in ["university", "school", "college", "institute", "institut", "academy", "polytechnic"]
    )


def extract_education(sentences: list[str]) -> list[tuple[str, str]]:
    patterns = [
        re.compile(
            r"(?:holds|hold|earned|received|obtained|completed|currently completing|is currently completing)\s+"
            r"(?:an?|the)?\s*([^.,;]{3,120}?)\s+(?:from|at)\s+([^.,;]{3,140})",
            re.I,
        ),
        re.compile(r"(?:with)\s+(?:an?|the)?\s*([^.,;]{3,120}?)\s+(?:from|at)\s+([^.,;]{3,140})", re.I),
    ]

    records: list[tuple[str, str]] = []
    for sentence in sentences:
        lowered = sentence.lower()
        if not any(
            token in lowered
            for token in ["holds", "earned", "received", "obtained", "completed", "degree", "master", "bachelor", "phd", "dphil", "mba", "msc"]
        ):
            continue
        for pattern in patterns:
            for match in pattern.finditer(sentence):
                degree = match.group(1).strip(" ,.-")
                institution = match.group(2).strip(" ,.-")
                if not degree or not institution:
                    continue
                if not _looks_like_degree(degree) or not _looks_like_institution(institution):
                    continue
                # Keep conservative and avoid merged multi-education fragments.
                if " and " in degree.lower() or " and " in institution.lower():
                    continue
                if len(degree) > 100 or len(institution) > 120:
                    continue
                records.append((institution, degree))

    seen = set()
    deduped = []
    for institution, degree in records:
        key = (institution.lower(), degree.lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append((institution, degree))
    return deduped


def extract_experience(sentences: list[str]) -> list[tuple[str, str]]:
    marker_pattern = re.compile(
        r"\b(prior to joining cpi|before joining cpi|previously|earlier in (?:his|her|their) career|before cpi)\b",
        re.I,
    )
    role_org_pattern = re.compile(
        r"(?:served as|worked as|held|was|has worked as)\s+(?:an?\s+)?([^.,;]{3,100}?)\s+at\s+([^.,;]{3,120})",
        re.I,
    )

    records: list[tuple[str, str]] = []
    for sentence in sentences:
        if not marker_pattern.search(sentence):
            continue
        for match in role_org_pattern.finditer(sentence):
            role = match.group(1).strip(" ,.-")
            organization = match.group(2).strip(" ,.-")
            # Trim common continuation fragments to keep organization clean.
            organization = re.split(
                r"\s+(?:and as|where|which|while|including)\s+",
                organization,
                maxsplit=1,
                flags=re.I,
            )[0].strip(" ,.-")
            if len(role) < 3 or len(organization) < 3:
                continue
            if organization.lower() in {"climate policy initiative", "cpi"}:
                continue
            if len(organization) > 100:
                continue
            records.append((organization, role))

    seen = set()
    deduped = []
    for organization, role in records:
        key = (organization.lower(), role.lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append((organization, role))
    return deduped


def fetch_person_record(
    session: requests.Session, person: tuple[str, str, str]
) -> dict[str, object]:
    name, current_role, profile_url = person
    page_html = ""
    try:
        response = session.get(profile_url, timeout=60)
        if response.ok:
            page_html = response.text
    except requests.RequestException:
        page_html = ""

    linkedin_url = extract_linkedin_url(page_html) if page_html else ""
    sentences = extract_bio_sentences(page_html) if page_html else []

    return {
        "name": name,
        "current_role": current_role,
        "profile_url": profile_url,
        "linkedin_url": linkedin_url,
        "education": extract_education(sentences),
        "experience": extract_experience(sentences),
    }


def build_dataset(out_path: Path) -> pd.DataFrame:
    session = make_session()
    team_html = session.get(TEAM_URL, timeout=120).text
    people = extract_people_index(team_html)

    rows: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [executor.submit(fetch_person_record, session, person) for person in people]
        for future in as_completed(futures):
            person = future.result()

            rows.append(
                {
                    "name": person["name"],
                    "current_role": person["current_role"],
                    "record_type": "experience",
                    "institution": "Climate Policy Initiative",
                    "title": person["current_role"],
                    "start_year": "",
                    "end_year": "",
                    "source_url": person["profile_url"],
                    "linkedin_url": person["linkedin_url"],
                }
            )

            for institution, degree in person["education"]:
                rows.append(
                    {
                        "name": person["name"],
                        "current_role": person["current_role"],
                        "record_type": "education",
                        "institution": institution,
                        "title": degree,
                        "start_year": "",
                        "end_year": "",
                        "source_url": person["profile_url"],
                        "linkedin_url": person["linkedin_url"],
                    }
                )

            for institution, title in person["experience"]:
                rows.append(
                    {
                        "name": person["name"],
                        "current_role": person["current_role"],
                        "record_type": "experience",
                        "institution": institution,
                        "title": title,
                        "start_year": "",
                        "end_year": "",
                        "source_url": person["profile_url"],
                        "linkedin_url": person["linkedin_url"],
                    }
                )

    seen = set()
    deduped_rows = []
    for row in rows:
        key = (
            row["name"].lower(),
            row["record_type"],
            row["institution"].lower(),
            row["title"].lower(),
            row["source_url"],
        )
        if key in seen:
            continue
        seen.add(key)
        deduped_rows.append(row)

    df = pd.DataFrame(deduped_rows)
    ordered_columns = [
        "name",
        "current_role",
        "record_type",
        "institution",
        "title",
        "start_year",
        "end_year",
        "source_url",
        "linkedin_url",
    ]
    df = df[ordered_columns]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    return df


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build CPI public profile dataset from public team/bio pages."
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/climate_policy_initiative_public_profiles.csv"),
        help="Output CSV path.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    df = build_dataset(args.out)
    print(f"Generated {len(df)} rows for {df['name'].nunique()} people.")
    print(f"Output: {args.out}")


if __name__ == "__main__":
    main()
