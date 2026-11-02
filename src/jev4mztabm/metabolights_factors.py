#!/usr/bin/env python3
"""Export MetaboLights study factors and their deposited ontology annotations."""

import argparse
import csv
import json
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_URL = "https://www.ebi.ac.uk/metabolights/ws"
STUDY_ID = re.compile(r"MTBLS[1-9][0-9]*\Z")
FACTOR_FIELDS = (
    "study_id", "factor_name", "factor_type", "ontology_source",
    "ontology_accession", "ontology_version", "ontology_file",
)
STATUS_FIELDS = ("study_id", "status", "factor_count", "source", "error")


def parse_study_ids(payload):
    """Read the public study listing, checking its optional advertised count."""
    ids = payload.get("content") if isinstance(payload, dict) else payload
    if not isinstance(ids, list) or any(
        not isinstance(item, str) or not STUDY_ID.fullmatch(item) for item in ids
    ):
        raise ValueError("Expected a list of MTBLS accessions in the study listing")
    if isinstance(payload, dict) and "studies" in payload:
        count = payload["studies"]
        if type(count) is not int or count != len(ids):
            raise ValueError("Public study count does not match the returned accessions")
    return sorted(set(ids), key=lambda item: int(item[5:]))


def _text(value):
    if value is None:
        return ""
    if not isinstance(value, (str, int, float)):
        raise ValueError("Expected text in a factor annotation")
    return str(value)


def factor_rows(study_id, payload):
    """Keep every study factor, including factors without ontology annotations."""
    if not isinstance(payload, dict) or not isinstance(payload.get("factors"), list):
        raise ValueError("Expected a JSON object containing a factors list")
    for factor in payload["factors"]:
        if not isinstance(factor, dict):
            raise ValueError("Expected each factor to be a JSON object")
        annotation = factor.get("factorType")
        if annotation is None:
            annotation = {}
        if not isinstance(annotation, dict):
            raise ValueError("Expected factorType to be an object or null")
        source = annotation.get("termSource")
        if source is None:
            source = {}
        if not isinstance(source, dict):
            raise ValueError("Expected termSource to be an object or null")
        yield {
            "study_id": study_id,
            "factor_name": _text(factor.get("factorName")),
            "factor_type": _text(annotation.get("annotationValue")),
            "ontology_source": _text(source.get("name")),
            "ontology_accession": _text(annotation.get("termAccession")),
            "ontology_version": _text(source.get("version")),
            "ontology_file": _text(source.get("file")),
        }


def _retry_delay(error, attempt):
    """Honor Retry-After (seconds or HTTP date), otherwise use backoff."""
    value = error.headers.get("Retry-After") if isinstance(error, HTTPError) and error.headers else None
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return min(30, 2 ** attempt)


class ApiClient:
    """Small, sequential public API client; no third-party packages required."""

    def __init__(self, timeout=30, retries=3, interval=0.2):
        self.timeout = timeout
        self.retries = retries
        self.interval = interval
        self._last_request = None

    def get_json(self, path):
        request = Request(
            API_URL + "/" + path.lstrip("/"),
            headers={"Accept": "application/json", "User-Agent": "metabolights-factor-export/1.0"},
        )
        for attempt in range(self.retries + 1):
            if self._last_request is not None:
                delay = self.interval - (time.monotonic() - self._last_request)
                if delay > 0:
                    time.sleep(delay)
            self._last_request = time.monotonic()
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    return json.load(response)
            except HTTPError as error:
                if error.code != 429 and not 500 <= error.code < 600:
                    raise
                if attempt == self.retries:
                    raise
                time.sleep(_retry_delay(error, attempt))
            except (URLError, OSError, HTTPException):
                if attempt == self.retries:
                    raise
                time.sleep(min(30, 2 ** attempt))


def _study_rows(study_id, client, cache_dir, refresh):
    cache_path = cache_dir / (study_id + ".json")
    if cache_path.exists() and not refresh:
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            return list(factor_rows(study_id, payload)), "cache"
        except (OSError, ValueError):
            print(f"{study_id}: ignoring unreadable or invalid cache", file=sys.stderr)
    payload = client.get_json(f"studies/{study_id}/factors")
    rows = list(factor_rows(study_id, payload))
    # Rename only a fully written, validated response into the cache.
    temporary = cache_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(cache_path)
    return rows, "api"


def export_factors(study_ids, client, output_dir, refresh=False):
    """Write factors and study coverage; return the number of failed studies."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = output_dir / ".cache"
    cache_dir.mkdir(exist_ok=True)
    failures = 0
    total_factors = 0
    with (output_dir / "factors.csv").open("w", newline="", encoding="utf-8") as factors_file, \
            (output_dir / "study_status.csv").open("w", newline="", encoding="utf-8") as status_file:
        factors_writer = csv.DictWriter(factors_file, fieldnames=FACTOR_FIELDS)
        status_writer = csv.DictWriter(status_file, fieldnames=STATUS_FIELDS)
        factors_writer.writeheader()
        status_writer.writeheader()
        for index, study_id in enumerate(study_ids, start=1):
            try:
                rows, source = _study_rows(study_id, client, cache_dir, refresh)
            except (OSError, ValueError, HTTPException) as error:
                failures += 1
                status_writer.writerow({
                    "study_id": study_id, "status": "error", "error": str(error),
                })
                print(f"[{index}/{len(study_ids)}] {study_id}: ERROR: {error}", file=sys.stderr)
            else:
                factors_writer.writerows(rows)
                total_factors += len(rows)
                status_writer.writerow({
                    "study_id": study_id, "status": "ok" if rows else "no_factors",
                    "factor_count": len(rows), "source": source, "error": "",
                })
                print(f"[{index}/{len(study_ids)}] {study_id}: {len(rows)} factors ({source})", file=sys.stderr)
            factors_file.flush()
            status_file.flush()
    print(f"Wrote {total_factors} factors from {len(study_ids) - failures}/{len(study_ids)} studies to {output_dir}", file=sys.stderr)
    return failures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--studies", nargs="+", metavar="MTBLS_ID", help="Export only these IDs (default: all public studies)")
    parser.add_argument("--output-dir", type=Path, default=Path("results"))
    parser.add_argument("--refresh", action="store_true", help="Refetch factors instead of reusing cached responses")
    parser.add_argument("--timeout", type=float, default=30, help="Request timeout in seconds (default: 30)")
    parser.add_argument("--retries", type=int, default=3, help="Retries per transient failure (default: 3)")
    parser.add_argument("--interval", type=float, default=0.2, help="Minimum seconds between API request starts (default: 0.2)")
    args = parser.parse_args(argv)
    if args.timeout <= 0 or args.retries < 0 or args.interval < 0:
        parser.error("timeout must be positive; retries and interval must be nonnegative")
    client = ApiClient(args.timeout, args.retries, args.interval)
    try:
        study_ids = parse_study_ids(args.studies if args.studies is not None else client.get_json("studies"))
        print(f"Exporting {len(study_ids)} studies", file=sys.stderr)
        failures = export_factors(study_ids, client, args.output_dir, args.refresh)
    except (OSError, ValueError, HTTPException) as error:
        print(f"Export failed: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted. Rerun the same command to reuse completed study caches.", file=sys.stderr)
        return 130
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
