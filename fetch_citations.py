#!/usr/bin/env python3
"""Fetch citation counts from NASA ADS and safely update ``citations.json``.

The script is intentionally importable without an ADS token so its data and merge
logic can be unit-tested. A token is required only when :func:`main` performs
network requests.
"""
from __future__ import annotations

import json
import os
import time
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import requests

BIGQUERY_URL = "https://api.adsabs.harvard.edu/v1/search/bigquery"
SEARCH_URL = "https://api.adsabs.harvard.edu/v1/search/query"
REQUEST_TIMEOUT = (10, 60)
FALLBACK_CHUNK_SIZE = 20
FALLBACK_ATTEMPTS = 4

FIRST_AUTHOR = {
    "17": "2026ApJ...996L..22P",
    "16": "2024ApJ...973L..45P",
    "15": "2024A&A...685A.115P",
    "14": "2023ApJ...958...28P",
    "13": "2023ApJ...958...27P",
    "12": "2022Galax..10..102P",
    "11": "2021ApJ...922..180P",
    "10": "2021ApJ...909...76P",
    "9":  "2021ApJ...906...85P",
    "8":  "2019ApJ...887..147P",
    "7":  "2019ApJ...877..106P",
    "6":  "2019ApJ...871..257P",
    "5":  "2018ApJ...860..112P",
    "4":  "2017ApJ...834..157P",
    "3":  "2015A&A...576L..16P",
    "2":  "2014ApJ...785...76P",
    "1":  "2012JKAS...45..147P",
}

STUDENT = {
    "s1": "2024A&A...688A..94Y",
    "s2": "2026arXiv260316796K",
    "s3": "2026arXiv260325185L",
}

COAUTHOR = {
    "83": "2026ApJ..1000..231B",
    "82": "2026ApJ...999..169R",
    "81": "2026arXiv260113356G",
    "80": "2026A&A...706A..27S",
    "79": "2026A&A...705A..23G",
    "78": "2025A&A...704A..91E",
    "77": "2025A&A...699A.279D",
    "76": "2025A&A...699A.265G",
    "75": "2025ApJ...986...49K",
    "74": "2025A&A...695A.233R",
    "73": "2025A&A...694A.291K",
    "72": "2025JKAS...58...17C",
    "71": "2025A&A...693A.265E",
    "70": "2024A&A...692A.205B",
    "69": "2024A&A...692A.140A",
    "68": "2024ApJ...973..100K",
    "67": "2024AJ....168..130R",
    "66": "2024ApJ...970..176K",
    "65": "2024ApJ...964L..26E",
    "64": "2024ApJ...964L..25E",
    "63": "2024A&A...682L...3P",
    "62": "2024A&A...681A..79E",
    "61": "2023ApJ...959...14T",
    "60": "2023ApJ...957L..21R",
    "59": "2023ApJ...957L..20E",
    "58": "2023PASP..135i5001C",
    "57": "2023Natur.621..711C",
    "56": "2023MNRAS.523.5703J",
    "55": "2023ApJ...952...47T",
    "54": "2023ApJ...950...35P",
    "53": "2023ApJ...950...10L",
    "52": "2023A&A...673A.159R",
    "51": "2023Natur.616..686L",
    "50": "2023ApJ...943..170J",
    "49": "2023JKAS...56....1K",
    "48": "2022Galax..10..113A",
    "47": "2022ApJ...939...83K",
    "46": "2022ApJ...935...61B",
    "45": "2022ApJ...934..145I",
    "44": "2022ApJ...932...72Z",
    "43": "2022ApJ...930L..21B",
    "42": "2022ApJ...930L..20G",
    "41": "2022ApJ...930L..19W",
    "40": "2022ApJ...930L..18F",
    "39": "2022ApJ...930L..17E",
    "38": "2022ApJ...930L..16E",
    "37": "2022ApJ...930L..15E",
    "36": "2022ApJ...930L..14E",
    "35": "2022ApJ...930L..13E",
    "34": "2022ApJ...930L..12E",
    "33": "2022ApJ...926..108C",
    "32": "2022ApJ...925...13S",
    "31": "2021NatAs...5.1017J",
    "30": "2021ApJ...914...43H",
    "29": "2021A&A...651A..74K",
    "28": "2021PhRvD.103j4047K",
    "27": "2021ApJ...912...35N",
    "26": "2021RAA....21..205C",
    "25": "2021ApJ...911L..11E",
    "24": "2021ApJ...910L..14G",
    "23": "2021ApJ...910L..13E",
    "22": "2021ApJ...910L..12E",
    "21": "2020PhRvL.125n1104P",
    "20": "2020ApJ...902..104L",
    "19": "2020ApJ...901...67W",
    "18": "2020A&A...640A..69K",
    "17c": "2020ApJ...897..148G",
    "16c": "2020ApJ...897..139B",
    "15c": "2020A&A...637L...6K",
    "14c": "2019ApJ...886...85A",
    "13c": "2019MNRAS.486.2412L",
    "12c": "2019JKAS...52...23Z",
    "11c": "2018MNRAS.480.2324K",
    "10c": "2018ApJ...859..128A",
    "9c": "2018MNRAS.475..368H",
    "8c": "2018ApJ...852...30A",
    "7c": "2018AJ....155...26Z",
    "6c": "2017JKAS...50..167K",
    "5c": "2017PASJ...69...71H",
    "4c": "2016ApJS..227....8L",
    "3c": "2015JKAS...48..299O",
    "2c": "2015JKAS...48..285K",
    "1c": "2015JKAS...48..237A",
}

# arXiv identifiers let the fallback resolve a paper after ADS replaces an
# arXiv bibcode with the canonical journal bibcode. The lookup is still batched,
# so this does not add one request per paper.
ARXIV_IDENTIFIERS = {
    "2026arXiv260316796K": "2603.16796",
    "2026arXiv260325185L": "2603.25185",
    "2026arXiv260113356G": "2601.13356",
}

PUBLICATION_MAPS: dict[str, dict[str, str]] = {
    "first_author": FIRST_AUTHOR,
    "student": STUDENT,
    "coauthor": COAUTHOR,
}


class CitationUpdateError(RuntimeError):
    """Raised when a safe citation update cannot be produced."""


def utc_stamp(now: datetime | None = None) -> str:
    """Return the timestamp format consumed by the website."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def clean_timestamp(value: Any, fallback: str) -> str:
    """Normalize current and legacy ``updated`` values without ever throwing."""
    if isinstance(value, str):
        cleaned = value.split(" (", 1)[0].strip()
        if cleaned:
            return cleaned
    return fallback


def normalize_count(value: Any) -> int | None:
    """Return a non-negative integer citation count, or ``None`` if invalid."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def log_quota(resp: requests.Response, label: str) -> None:
    """Print ADS rate-limit headers without allowing logging to break a run."""
    try:
        limit = resp.headers.get("X-RateLimit-Limit")
        remaining = resp.headers.get("X-RateLimit-Remaining")
        reset = resp.headers.get("X-RateLimit-Reset")
        if limit or remaining:
            reset_text = ""
            if reset:
                try:
                    reset_at = datetime.fromtimestamp(int(reset), tz=timezone.utc)
                    reset_text = " resets " + reset_at.strftime("%Y-%m-%d %H:%M UTC")
                except (TypeError, ValueError, OSError):
                    reset_text = f" reset={reset}"
            print(
                f"    [{label}] HTTP {resp.status_code} | "
                f"quota {remaining}/{limit}{reset_text}"
            )
        else:
            print(
                f"    [{label}] HTTP {resp.status_code} | "
                "no rate-limit headers returned"
            )
    except Exception as exc:  # pragma: no cover - defensive logging only
        print(f"    [{label}] HTTP {getattr(resp, 'status_code', '?')} | quota log failed: {exc}")


def _extract_docs(resp: requests.Response) -> list[dict[str, Any]]:
    payload = resp.json()
    response = payload.get("response", {}) if isinstance(payload, dict) else {}
    docs = response.get("docs", []) if isinstance(response, dict) else []
    return [doc for doc in docs if isinstance(doc, dict)]


def _add_count(target: dict[str, int], key: str, raw_count: Any) -> None:
    count = normalize_count(raw_count)
    if count is None:
        return
    # If ADS temporarily exposes both an arXiv and canonical record, use the
    # larger count rather than allowing response order to lower the badge.
    target[key] = max(target.get(key, 0), count)


def fetch_bigquery(
    bibcodes: Sequence[str],
    *,
    session: requests.Session,
    token: str,
) -> dict[str, int]:
    """Fetch direct bibcode matches in a single ADS bigquery request."""
    if not bibcodes:
        return {}
    payload = "bibcode\n" + "\n".join(bibcodes)
    resp = session.post(
        BIGQUERY_URL,
        headers={**auth_headers(token), "Content-Type": "big-query/csv"},
        params={"q": "*:*", "fl": "bibcode,citation_count", "rows": len(bibcodes)},
        data=payload.encode("utf-8"),
        timeout=REQUEST_TIMEOUT,
    )
    log_quota(resp, "bigquery")
    resp.raise_for_status()

    requested = set(bibcodes)
    found: dict[str, int] = {}
    for doc in _extract_docs(resp):
        bibcode = doc.get("bibcode")
        if isinstance(bibcode, str) and bibcode in requested:
            _add_count(found, bibcode, doc.get("citation_count", 0))
    return found


def _identifier_strings(doc: Mapping[str, Any]) -> set[str]:
    raw = doc.get("identifier", [])
    if isinstance(raw, str):
        values: Iterable[Any] = [raw]
    elif isinstance(raw, Sequence):
        values = raw
    else:
        values = []
    return {str(value).strip().lower() for value in values if value is not None}


def _retry_delay(resp: requests.Response | None, attempt: int) -> int:
    if resp is not None:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return max(1, min(int(float(retry_after)), 60))
            except (TypeError, ValueError):
                pass
    return min(15 * (attempt + 1), 60)


def fetch_missing(
    bibcodes: Sequence[str],
    *,
    session: requests.Session,
    token: str,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, int]:
    """Resolve missing bibcodes with batched query requests and bounded retries."""
    found: dict[str, int] = {}
    total_chunks = (len(bibcodes) + FALLBACK_CHUNK_SIZE - 1) // FALLBACK_CHUNK_SIZE

    for chunk_index, start in enumerate(range(0, len(bibcodes), FALLBACK_CHUNK_SIZE), start=1):
        batch = list(bibcodes[start : start + FALLBACK_CHUNK_SIZE])
        terms: list[str] = []
        for bibcode in batch:
            terms.append(f'bibcode:"{bibcode}"')
            arxiv_id = ARXIV_IDENTIFIERS.get(bibcode)
            if arxiv_id:
                terms.append(f'identifier:"arXiv:{arxiv_id}"')

        query = " OR ".join(terms)
        for attempt in range(FALLBACK_ATTEMPTS):
            response: requests.Response | None = None
            try:
                response = session.get(
                    SEARCH_URL,
                    headers=auth_headers(token),
                    params={
                        "q": query,
                        "fl": "bibcode,citation_count,identifier",
                        "rows": max(len(batch) * 2, 20),
                    },
                    timeout=REQUEST_TIMEOUT,
                )
                log_quota(response, f"fallback {chunk_index}/{total_chunks}")

                if response.status_code == 429 or 500 <= response.status_code < 600:
                    if attempt == FALLBACK_ATTEMPTS - 1:
                        print(f"    fallback chunk failed after {FALLBACK_ATTEMPTS} attempts")
                        break
                    delay = _retry_delay(response, attempt)
                    print(f"    transient HTTP {response.status_code}; retrying in {delay}s")
                    sleep(delay)
                    continue

                response.raise_for_status()
                docs = _extract_docs(response)
                requested = set(batch)
                arxiv_to_bibcode = {
                    arxiv_id.lower(): bibcode
                    for bibcode in batch
                    if (arxiv_id := ARXIV_IDENTIFIERS.get(bibcode))
                }

                for doc in docs:
                    returned_bibcode = doc.get("bibcode")
                    if isinstance(returned_bibcode, str) and returned_bibcode in requested:
                        _add_count(found, returned_bibcode, doc.get("citation_count", 0))

                    identifiers = _identifier_strings(doc)
                    for arxiv_id, requested_bibcode in arxiv_to_bibcode.items():
                        if any(
                            identifier == arxiv_id
                            or identifier == f"arxiv:{arxiv_id}"
                            or identifier.endswith(f"/{arxiv_id}")
                            for identifier in identifiers
                        ):
                            _add_count(found, requested_bibcode, doc.get("citation_count", 0))
                break
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else "?"
                if status in (401, 403):
                    print(f"    authorization failed with HTTP {status}; aborting fallback requests")
                    return found
                print(f"    non-retryable HTTP {status}; skipping this fallback chunk")
                break
            except (requests.ConnectionError, requests.Timeout, ValueError) as exc:
                if attempt == FALLBACK_ATTEMPTS - 1:
                    print(f"    fallback chunk error: {exc}")
                    break
                delay = _retry_delay(response, attempt)
                print(f"    fallback chunk error: {exc}; retrying in {delay}s")
                sleep(delay)
            except requests.RequestException as exc:
                print(f"    non-retryable request error: {exc}")
                break

        if chunk_index < total_chunks:
            sleep(3)

    return found


def collect_citation_counts(
    publication_maps: Mapping[str, Mapping[str, str]],
    *,
    session: requests.Session,
    token: str,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, int]:
    """Fetch all unique bibcodes, falling back for *every* missing record."""
    all_bibcodes = sorted(
        {bibcode for mapping in publication_maps.values() for bibcode in mapping.values()}
    )
    print(f"Total unique bibcodes: {len(all_bibcodes)}")

    found: dict[str, int] = {}
    try:
        print("Trying bigquery (single request)...")
        found.update(fetch_bigquery(all_bibcodes, session=session, token=token))
        print(f"  bigquery returned {len(found)}/{len(all_bibcodes)} records")
    except (requests.RequestException, ValueError) as exc:
        print(f"  bigquery failed: {exc}")

    missing = [bibcode for bibcode in all_bibcodes if bibcode not in found]
    if missing:
        print(f"Resolving {len(missing)} missing records with batched fallback queries...")
        found.update(fetch_missing(missing, session=session, token=token, sleep=sleep))
        still_missing = [bibcode for bibcode in all_bibcodes if bibcode not in found]
        print(f"  final coverage: {len(found)}/{len(all_bibcodes)} unique bibcodes")
        if still_missing:
            print("  unresolved bibcodes: " + ", ".join(still_missing))

    return found


def section_results(
    publication_maps: Mapping[str, Mapping[str, str]],
    bibcode_counts: Mapping[str, int],
) -> tuple[dict[str, dict[str, int]], list[str]]:
    """Convert bibcode counts back to website keys and list unresolved entries."""
    result: dict[str, dict[str, int]] = {}
    missing: list[str] = []
    for section, mapping in publication_maps.items():
        section_data: dict[str, int] = {}
        for publication_id, bibcode in mapping.items():
            if bibcode in bibcode_counts:
                section_data[publication_id] = bibcode_counts[bibcode]
            else:
                missing.append(f"{section}:{publication_id}")
        result[section] = section_data
        print(f"  {section}: {len(section_data)}/{len(mapping)}")
    return result, missing


def sanitize_section(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    clean: dict[str, int] = {}
    for key, raw_count in value.items():
        count = normalize_count(raw_count)
        if isinstance(key, str) and count is not None:
            clean[key] = count
    return clean


def load_existing(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"No usable {path.name} to preserve ({exc}).")
        return None
    if not isinstance(data, dict):
        print(f"No usable {path.name} to preserve (root value is not an object).")
        return None
    return data


def build_output(
    fresh_sections: Mapping[str, Mapping[str, int]],
    missing: Sequence[str],
    *,
    existing: Mapping[str, Any] | None,
    stamp: str,
) -> tuple[dict[str, Any], bool]:
    """Build a complete success file or a merged stale file without data loss."""
    if not missing:
        output: dict[str, Any] = {
            section: dict(fresh_sections.get(section, {}))
            for section in PUBLICATION_MAPS
        }
        output.update({"updated": stamp, "status": "ok"})
        return output, True

    if existing is None:
        raise CitationUpdateError(
            "ADS returned an incomplete result and there is no existing citations.json "
            "to preserve; refusing to write a partial file."
        )

    merged: dict[str, Any] = {}
    for section in PUBLICATION_MAPS:
        merged[section] = {
            **sanitize_section(existing.get(section)),
            **dict(fresh_sections.get(section, {})),
        }

    merged.update(
        {
            "updated": clean_timestamp(existing.get("updated"), stamp),
            "last_attempt": stamp,
            "status": "stale",
            "missing": list(missing),
        }
    )
    return merged, False


def atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    """Write JSON atomically so interruption cannot truncate the live file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def duplicate_bibcodes(
    publication_maps: Mapping[str, Mapping[str, str]],
) -> dict[str, list[str]]:
    locations: defaultdict[str, list[str]] = defaultdict(list)
    for section, mapping in publication_maps.items():
        for publication_id, bibcode in mapping.items():
            locations[bibcode].append(f"{section}:{publication_id}")
    return {bibcode: ids for bibcode, ids in locations.items() if len(ids) > 1}


def main() -> int:
    token = os.environ.get("ADS_API_TOKEN", "").strip()
    if not token:
        print("ERROR: ADS_API_TOKEN environment variable not set")
        return 1

    output_path = Path(os.environ.get("CITATIONS_OUTPUT", "citations.json"))
    stamp = utc_stamp()

    for bibcode, locations in duplicate_bibcodes(PUBLICATION_MAPS).items():
        print(
            "::warning::Duplicate ADS bibcode "
            f"{bibcode} is assigned to {', '.join(locations)}. "
            "Verify the publication list; both badges currently receive the same count."
        )

    with requests.Session() as session:
        bibcode_counts = collect_citation_counts(
            PUBLICATION_MAPS,
            session=session,
            token=token,
        )

    fresh_sections, missing = section_results(PUBLICATION_MAPS, bibcode_counts)
    existing = load_existing(output_path) if missing else None

    try:
        output, complete = build_output(
            fresh_sections,
            missing,
            existing=existing,
            stamp=stamp,
        )
    except CitationUpdateError as exc:
        print(f"::error::{exc}")
        return 1

    atomic_write_json(output_path, output)
    total = sum(sum(section.values()) for section in fresh_sections.values())

    if complete:
        print(f"Done! status=ok total citations={total}")
        return 0

    print(
        f"::warning::Incomplete ADS response ({len(missing)} website entries missing). "
        "Preserved last-known values and wrote status=stale."
    )
    print(f"Done! status=stale fresh citation subtotal={total}")
    # The workflow commits the stale metadata, then deliberately fails the job so
    # a degraded daily update cannot remain silently green.
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
