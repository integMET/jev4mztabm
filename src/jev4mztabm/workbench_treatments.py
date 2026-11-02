#!/usr/bin/env python3
"""Export Treatment ID and Treatment Summary for public Workbench studies."""

import argparse
import csv
import re
import sys
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from http.client import HTTPException
from pathlib import Path
from urllib.parse import urlencode

from .workbench_factors import (
    API_URL, STUDY_ID, WorkbenchClient, atomic_json, parse_study_ids, utc_now,
)


TREATMENT_FIELDS = ("study_id", "treatment_id", "treatment_summary")
STATUS_FIELDS = ("study_id", "status", "treatment_count", "source", "source_url", "error")
TREATMENT_ID = re.compile(r"TR[0-9]{6}\Z")
METADATA_URL = "https://www.metabolomicsworkbench.org/data/DRCCMetadata.php"


def treatment_url(study_id):
    """Return the public Treatment tab URL for an exact study accession."""
    if not isinstance(study_id, str) or not STUDY_ID.fullmatch(study_id):
        raise ValueError(f"Invalid Workbench study accession: {study_id!r}")
    return METADATA_URL + "?" + urlencode({
        "Mode": "Study", "StudyID": study_id, "StudyType": "MS",
        "ResultType": "1", "DataMode": "TreatmentData",
    })


def _cell_text(parts):
    # Collapse source formatting whitespace while retaining explicit HTML breaks.
    return "\n".join(" ".join(line.split()) for line in "".join(parts).split("\n")).strip()


class _TreatmentPageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.treatment_tables = []
        self.headings = []
        self.visible = []
        self._tables = []
        self._heading = None
        self._ignored = 0

    def _finish_cell(self, table):
        if table["cell"] is not None:
            table["row"].append(_cell_text(table["cell"]))
            table["cell"] = None

    def _finish_row(self, table):
        self._finish_cell(table)
        if table["row"] is not None:
            self.rows.append(table["row"])
            table["rows"].append(table["row"])
            table["row"] = None

    def _break(self):
        for table in self._tables:
            if table["cell"] is not None:
                table["cell"].append("\n")

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self._ignored += 1
        if self._ignored:
            return
        if tag == "h1":
            self._heading = []
        elif tag == "table":
            treatment = bool(re.search(r"\bTreatment\s*:\s*$", " ".join(self.visible)))
            self._tables.append({"row": None, "cell": None, "rows": [], "treatment": treatment})
        elif tag == "tr" and self._tables:
            self._finish_row(self._tables[-1])
            self._tables[-1]["row"] = []
        elif tag in {"td", "th"} and self._tables and self._tables[-1]["row"] is not None:
            self._finish_cell(self._tables[-1])
            self._tables[-1]["cell"] = []
        elif tag in {"br", "p", "div", "li"}:
            self._break()

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self._ignored = max(0, self._ignored - 1)
            return
        if self._ignored:
            return
        if tag == "h1" and self._heading is not None:
            self.headings.append(" ".join("".join(self._heading).split()))
            self._heading = None
        elif tag in {"td", "th"} and self._tables:
            self._finish_cell(self._tables[-1])
        elif tag == "tr" and self._tables:
            self._finish_row(self._tables[-1])
        elif tag == "table" and self._tables:
            table = self._tables.pop()
            self._finish_row(table)
            if table["treatment"]:
                self.treatment_tables.append(table["rows"])
        elif tag in {"p", "div", "li"}:
            self._break()

    def handle_data(self, data):
        if self._ignored:
            return
        self.visible.append(data)
        if self._heading is not None:
            self._heading.append(data)
        for table in self._tables:
            if table["cell"] is not None:
                # Raw newlines in HTML are formatting, unlike <br> and blocks.
                table["cell"].append(re.sub(r"\s+", " ", data))


def parse_treatments(study_id, html):
    """Extract all deposited treatment records, rejecting error/foreign pages.

    An absent summary is blank; the separate ``Treatment:`` field is never
    substituted for ``Treatment Summary:``. No TR identifier is inferred.
    """
    treatment_url(study_id)  # Validate before using the ID or trusting the page.
    if not isinstance(html, str):
        raise ValueError("Expected a text Workbench metadata page")
    parser = _TreatmentPageParser()
    parser.feed(html)
    parser.close()
    expected = f"Summary of Study {study_id}".casefold()
    if [heading.casefold() for heading in parser.headings] != [expected]:
        raise ValueError(f"Metadata page has a wrong or missing study heading for {study_id}")
    if len(parser.treatment_tables) != 1:
        raise ValueError(f"{study_id}: metadata page needs one complete Treatment table")
    labels = {"study id": "study_id", "treatment id": "treatment_id",
              "treatment summary": "treatment_summary"}
    study_rows = [cells for cells in parser.rows if cells and
                  " ".join(cells[0].split()).rstrip(":").strip().casefold() == "study id"]
    for cells in study_rows:
        if len(cells) != 2 or cells[1] != study_id:
            raise ValueError(f"Metadata page contains a different study ID than {study_id}")
    records, fields = [], {}
    for cells in parser.treatment_tables[0]:
        if not cells:
            continue
        if not any(cells):
            if fields:
                records.append(fields)
                fields = {}
            continue
        label = " ".join(cells[0].split()).rstrip(":").strip().casefold()
        if label not in labels or label == "study id":
            continue
        if len(cells) != 2:
            raise ValueError(f"{study_id}: malformed {label} row")
        field, value = labels[label], cells[1]
        # A study can deposit several TR records in the same HTML table.
        if field == "treatment_id" and fields:
            records.append(fields)
            fields = {}
        if field in fields and fields[field] != value:
            raise ValueError(f"{study_id}: conflicting {label} rows")
        fields[field] = value
    if fields:
        records.append(fields)
    rows, identifiers = [], {}
    for record in records or [{}]:
        identifier = record.get("treatment_id", "")
        summary = record.get("treatment_summary", "")
        if identifier and not TREATMENT_ID.fullmatch(identifier):
            raise ValueError(f"Invalid Workbench treatment accession: {identifier!r}")
        if identifier in identifiers and identifiers[identifier] != summary and identifier:
            raise ValueError(f"{study_id}: conflicting summaries for {identifier}")
        identifiers[identifier] = summary
        row = {"study_id": study_id, "treatment_id": identifier, "treatment_summary": summary}
        if row not in rows:
            rows.append(row)
    return rows


def parse_treatment(study_id, html):
    """Extract a single treatment; use parse_treatments for studies with several."""
    rows = parse_treatments(study_id, html)
    if len(rows) != 1:
        raise ValueError(f"{study_id}: multiple treatment records; use parse_treatments")
    return rows[0]


def _study_result(study_id, client, cache_dir, refresh):
    path = cache_dir / (study_id + ".html")
    if path.exists() and not refresh:
        try:
            return parse_treatments(study_id, path.read_text(encoding="utf-8")), "cache", ""
        except (OSError, ValueError):
            pass  # Retry incomplete, invalid or foreign cached pages.
    try:
        html = client.get_text(treatment_url(study_id))
    except (OSError, ValueError, HTTPException) as error:
        return None, "api", "fetch_error: " + str(error)
    try:
        temporary = path.with_suffix(".html.tmp")
        temporary.write_text(html, encoding="utf-8")
        temporary.replace(path)
    except OSError as error:
        return None, "api", "cache_error: " + str(error)
    try:
        return parse_treatments(study_id, html), "api", ""
    except ValueError as error:
        return None, "api", "parse_error: " + str(error)


def _ordered_results(study_ids, client, cache_dir, refresh, workers):
    iterator = iter(study_ids)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = deque()
        for _ in range(min(len(study_ids), workers * 2)):
            study_id = next(iterator)
            pending.append((study_id, pool.submit(_study_result, study_id, client, cache_dir, refresh)))
        while pending:
            study_id, future = pending.popleft()
            yield study_id, future.result()
            following = next(iterator, None)
            if following is not None:
                pending.append((following, pool.submit(_study_result, following, client, cache_dir, refresh)))


def _status(row):
    if not row["treatment_id"] and not row["treatment_summary"]:
        return "no_treatment"
    if not row["treatment_id"]:
        return "missing_treatment_id"
    return "ok" if row["treatment_summary"] else "missing_summary"


def export_treatments(study_ids, client, output_dir, refresh=False, workers=1):
    """Write one CSV row per study/treatment plus status; return failure count.

    The client supplies ``get_text(url)`` (normally :class:`WorkbenchClient`).
    Failed studies retain a blank data row and an error in treatment_status.csv.
    Rerunning reuses valid cached HTML; ``refresh=True`` refetches every page.
    """
    if workers < 1:
        raise ValueError("workers must be positive")
    study_ids = parse_study_ids([{"study_id": study_id} for study_id in study_ids])
    output_dir = Path(output_dir)
    cache_dir = output_dir / ".cache" / "treatments"
    cache_dir.mkdir(parents=True, exist_ok=True)
    failures = row_count = 0
    with (output_dir / "treatments.csv").open("w", newline="", encoding="utf-8") as data, \
            (output_dir / "treatment_status.csv").open("w", newline="", encoding="utf-8") as statuses:
        writer = csv.DictWriter(data, fieldnames=TREATMENT_FIELDS)
        status_writer = csv.DictWriter(statuses, fieldnames=STATUS_FIELDS)
        writer.writeheader()
        status_writer.writeheader()
        for index, (study_id, (rows, source, error)) in enumerate(
                _ordered_results(study_ids, client, cache_dir, refresh, workers), 1):
            if rows is None:
                failures += 1
                row = dict.fromkeys(TREATMENT_FIELDS, "")
                row["study_id"] = study_id
                rows = [row]
                status = error.split(":", 1)[0]
            else:
                statuses_present = {_status(row) for row in rows}
                status = next(value for value in (
                    "missing_treatment_id", "missing_summary", "no_treatment", "ok",
                ) if value in statuses_present)
            treatment_count = sum(bool(row["treatment_id"] or row["treatment_summary"]) for row in rows)
            writer.writerows(rows)
            row_count += len(rows)
            status_writer.writerow({"study_id": study_id, "status": status, "treatment_count": treatment_count,
                                    "source": source,
                                    "source_url": treatment_url(study_id), "error": error})
            data.flush()
            statuses.flush()
            print(f"[{index}/{len(study_ids)}] {study_id}: {error or status} ({source})", file=sys.stderr)
    print(f"Wrote {row_count} rows for {len(study_ids)} studies to {output_dir / 'treatments.csv'} "
          f"({failures} failed studies)", file=sys.stderr)
    return failures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--studies", nargs="+", metavar="ST_ID", help="Only these studies (default: all public studies)")
    parser.add_argument("--output-dir", type=Path, default=Path("results/workbench/treatments"))
    parser.add_argument("--refresh", action="store_true", help="Refetch valid cached treatment pages")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent study workers (default: 4)")
    parser.add_argument("--interval", type=float, default=0.25, help="Shared minimum seconds between request starts (default: 0.25)")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--limit", type=int, help="Only the first N selected studies, for a pilot")
    args = parser.parse_args(argv)
    if (args.workers < 1 or args.interval < 0 or args.timeout <= 0 or args.retries < 0
            or args.limit is not None and args.limit < 1):
        parser.error("workers, timeout and limit must be positive; interval and retries nonnegative")
    started = utc_now()
    try:
        # Reject bad explicit IDs before making requests or writing files.
        study_ids = parse_study_ids([{"study_id": value} for value in args.studies]) if args.studies else None
        client = WorkbenchClient(args.timeout, args.retries, args.interval)
        listing_source = "explicit study IDs"
        args.output_dir.mkdir(parents=True, exist_ok=True)
        if study_ids is None:
            listing = client.get_json("study/study_id/ST/available")
            study_ids = parse_study_ids(listing)
            if not study_ids:
                raise ValueError("No public Workbench studies found in discovery response")
            cache_dir = args.output_dir / ".cache"
            cache_dir.mkdir(exist_ok=True)
            atomic_json(cache_dir / "study_listing.json", listing)
            listing_source = API_URL + "/study/study_id/ST/available"
        if args.limit:
            study_ids = study_ids[:args.limit]
        metadata = {"started_at": started, "listing_source": listing_source,
                    "study_count": len(study_ids), "study_ids": study_ids,
                    "workers": args.workers, "request_interval_seconds": args.interval,
                    "timeout_seconds": args.timeout, "retries": args.retries,
                    "refresh": args.refresh, "fields": list(TREATMENT_FIELDS),
                    "treatment_source": "public study Treatment tab HTML"}
        atomic_json(args.output_dir / "run_metadata.json", metadata)
        failures = export_treatments(study_ids, client, args.output_dir, args.refresh, args.workers)
        metadata.update(completed_at=utc_now(), failed_studies=failures)
        atomic_json(args.output_dir / "run_metadata.json", metadata)
    except (OSError, ValueError, HTTPException, csv.Error) as error:
        print(f"Treatment export failed: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted. Rerun the same command to reuse cached successful responses.", file=sys.stderr)
        return 130
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
