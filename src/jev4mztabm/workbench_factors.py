#!/usr/bin/env python3
"""Export public Workbench study factors and their sample-level evidence."""

import argparse
import csv
import json
import re
import sys
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .metabolights_factors import FACTOR_FIELDS as BASE_FACTOR_FIELDS, _retry_delay
from .workbench_mapping import MAPPING_FIELDS, apply_mappings, load_mappings
from .workbench_structured import MWTAB_CACHE_FORMAT, decode_mwtab_json, merge_structured_factors


API_URL = "https://www.metabolomicsworkbench.org/rest"
FACTOR_FIELDS = BASE_FACTOR_FIELDS + ("factor_value_unique",)
STUDY_ID = re.compile(r"ST[0-9]{6}\Z")
ANALYSIS_ID = re.compile(r"AN[0-9]{6}\Z")
SAMPLE_FIELDS = (
    "study_id", "mb_sample_id", "local_sample_id", "sample_source",
    "factor_name", "factor_position", "factor_value", "raw_factors",
    "factor_source", "analysis_ids",
)
STATUS_FIELDS = (
    "study_id", "status", "factor_count", "sample_count", "sample_factor_count",
    "source", "factor_source", "warning_count", "error",
)
WARNING_FIELDS = ("study_id", "mb_sample_id", "local_sample_id", "warning")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def normalize_records(payload):
    """Handle the API's single-object, numeric-keyed-object and list responses."""
    if isinstance(payload, dict) and "study_id" in payload:
        records = [payload]
    elif isinstance(payload, dict) and all(str(key).isdigit() for key in payload):
        records = list(payload.values())
    elif isinstance(payload, list):
        records = payload
    else:
        raise ValueError("Expected study records, not an error or unknown response shape")
    if any(not isinstance(record, dict) for record in records):
        raise ValueError("Every study record must be a JSON object")
    return records


def parse_study_ids(payload):
    ids = set()
    for record in normalize_records(payload):
        study_id = record.get("study_id")
        if not isinstance(study_id, str) or not STUDY_ID.fullmatch(study_id):
            raise ValueError(f"Invalid Workbench study accession: {study_id!r}")
        ids.add(study_id)
    return sorted(ids)


def parse_factor_pairs(text):
    """Split NAME:VALUE pairs, preserving unspaced literal pipes in values.

    The documented separator is whitespace-surrounded ``|``. Compact separators
    are also accepted when a NAME:VALUE pair follows before the next pipe.
    Thus the deposited ethnicity ``Ju|Hoan`` remains a single value.
    """
    if not isinstance(text, str):
        raise ValueError("The factors field must be text")
    if not text.strip():
        return []
    if "||" in text:
        raise ValueError("Ambiguous consecutive factor separators")
    pairs = []
    for fragment in re.split(r"(?<!\S)\|(?!\S)|\|(?=[^|:]+:)", text):
        if not fragment.strip():
            pairs.append(("", ""))
            continue
        name, separator, value = fragment.partition(":")
        if not separator:
            raise ValueError(f"Malformed factor pair: {fragment!r}")
        pairs.append((name.strip(), value.strip()))
    return pairs


def _text_field(record, field, required=False):
    value = record.get(field, "")
    if not isinstance(value, str) or required and not value.strip():
        raise ValueError(f"Expected {'nonblank ' if required else ''}text for {field}")
    return value


def factor_values_unique(factor_rows, sample_rows):
    """Attach all exact observed values per study/name as sorted JSON arrays.

    Blank values are retained. Repeated factor positions contribute to the same
    set; values are not split, trimmed or otherwise normalized here.
    """
    rows = [dict(row) for row in factor_rows]
    values = {(row["study_id"], row["factor_name"]): set() for row in rows}
    if len(values) != len(rows):
        raise ValueError("Duplicate study/factor definitions")
    for sample in sample_rows:
        if not sample["factor_name"]:
            continue
        key = (sample["study_id"], sample["factor_name"])
        if key not in values:
            raise ValueError(f"Sample evidence has no matching factor definition: {key!r}")
        value = sample.get("factor_value")
        if not isinstance(value, str):
            raise ValueError(f"Expected a text factor_value for {key!r}")
        values[key].add(value)
    for row in rows:
        key = (row["study_id"], row["factor_name"])
        if not values[key]:
            raise ValueError(f"Factor has no matching sample evidence: {key!r}")
        row["factor_value_unique"] = json.dumps(sorted(values[key]), ensure_ascii=False)
    return rows


def update_factor_value_unique(output_dir):
    """Update only factors.csv from existing sample evidence, without requests."""
    output_dir = Path(output_dir)
    path = output_dir / "factors.csv"
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        if not {"study_id", "factor_name"}.issubset(fields):
            raise ValueError("factors.csv must contain study_id and factor_name")
        factors = list(reader)
    with (output_dir / "sample_factors.csv").open(newline="", encoding="utf-8-sig") as handle:
        rows = factor_values_unique(factors, csv.DictReader(handle))
    if "factor_value_unique" not in fields:
        fields.append("factor_value_unique")
    temporary = path.with_suffix(".csv.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)
    return len(rows)


def parse_study(study_id, payload):
    """Derive unique study factors and lossless factor-pair sample evidence."""
    if not STUDY_ID.fullmatch(study_id):
        raise ValueError(f"Invalid study ID: {study_id!r}")
    factors = {}
    evidence = {}
    samples = {}
    warnings = []
    repeated_names_warned = set()
    for record in normalize_records(payload):
        if record.get("study_id") != study_id:
            raise ValueError(f"Requested {study_id}, received record for {record.get('study_id')!r}")
        local_id = _text_field(record, "local_sample_id", required=True)
        mb_id = _text_field(record, "mb_sample_id")
        source = _text_field(record, "sample_source")
        if "factors" not in record:
            raise ValueError(f"Sample {local_id!r} has no factors field")
        raw_factors = record["factors"]
        if not isinstance(raw_factors, str):
            raise ValueError("The factors field must be text")
        if "_factor_pairs" in record:
            pairs = record["_factor_pairs"]
            if not isinstance(pairs, list) or any(
                    not isinstance(pair, (list, tuple)) or len(pair) != 2
                    or any(not isinstance(value, str) for value in pair) for pair in pairs):
                raise ValueError("Expected structured factor name/value pairs")
        else:
            pairs = parse_factor_pairs(raw_factors)
        sample_key = ("mb", mb_id) if mb_id else ("local", local_id)
        identity = (mb_id, local_id, source)
        if sample_key in samples and samples[sample_key] != identity:
            raise ValueError(f"Conflicting identifiers or sample source for sample {local_id!r}")
        samples[sample_key] = identity
        base = {
            "study_id": study_id, "mb_sample_id": mb_id, "local_sample_id": local_id,
            "sample_source": source, "raw_factors": raw_factors,
            "factor_source": "mwtab" if "_factor_pairs" in record else "factors_api",
            "analysis_ids": record.get("_analysis_ids", ""),
        }
        if not pairs:
            key = (sample_key, 0)
            if key not in evidence:
                evidence[key] = dict(base, factor_name="", factor_position=0, factor_value="")
                warnings.append({"study_id": study_id, "mb_sample_id": mb_id,
                                 "local_sample_id": local_id, "warning": "Blank factors field"})
        for name, frequency in Counter(name for name, _ in pairs).items():
            if name and frequency > 1 and (sample_key, name) not in repeated_names_warned:
                warnings.append({"study_id": study_id, "mb_sample_id": mb_id,
                                 "local_sample_id": local_id,
                                 "warning": f"Repeated factor name {name!r}; preserved by factor_position"})
                repeated_names_warned.add((sample_key, name))
        for position, (name, value) in enumerate(pairs, 1):
            key = (sample_key, position)
            if key in evidence:
                if evidence[key]["factor_name"] != name or evidence[key]["factor_value"] != value:
                    raise ValueError(f"Conflicting factors for sample {local_id!r} at position {position}")
                continue
            evidence[key] = dict(base, factor_name=name, factor_position=position, factor_value=value)
            if not name:
                warnings.append({"study_id": study_id, "mb_sample_id": mb_id,
                                 "local_sample_id": local_id,
                                 "warning": f"Missing factor name at position {position}; value preserved"})
                continue
            # These are free-text factor names. No ontology annotation is inferred.
            factors.setdefault(name, dict.fromkeys(FACTOR_FIELDS, ""))
            factors[name].update(study_id=study_id, factor_name=name)
            if not value:
                warnings.append({"study_id": study_id, "mb_sample_id": mb_id,
                                 "local_sample_id": local_id,
                                 "warning": f"Blank value for factor {name!r}"})
    return {
        "factors": factor_values_unique([factors[name] for name in sorted(factors)], evidence.values()),
        "samples": list(evidence.values()), "sample_count": len(samples),
        "warnings": warnings,
        "factor_source": "mwtab" if any(row["factor_source"] == "mwtab" for row in evidence.values()) else "factors_api",
    }


class WorkbenchClient:
    """Public API client with a shared request-start limit across worker threads."""

    def __init__(self, timeout=30, retries=3, interval=0.25):
        self.timeout, self.retries, self.interval = timeout, retries, interval
        self._lock = threading.Lock()
        self._last_request = None
        self._not_before = 0
        self.study_analyses = {}

    def register_analyses(self, payload):
        for record in normalize_records(payload):
            study_id, analysis_id = record.get("study_id"), record.get("analysis_id")
            if not isinstance(study_id, str) or not STUDY_ID.fullmatch(study_id):
                raise ValueError("Invalid study in analysis listing")
            if not isinstance(analysis_id, str) or not ANALYSIS_ID.fullmatch(analysis_id):
                raise ValueError("Invalid analysis in available-study listing")
            self.study_analyses.setdefault(study_id, set()).add(analysis_id)

    def current_analysis_ids(self, study_id):
        if study_id not in self.study_analyses:
            listing = self.get_json(f"study/study_id/{study_id}/available")
            self.register_analyses(listing)
        ids = self.study_analyses.get(study_id)
        if not ids:
            raise ValueError(f"No current public analyses listed for {study_id}")
        return sorted(ids)

    def _wait_for_slot(self):
        with self._lock:
            target = self._not_before
            if self._last_request is not None:
                target = max(target, self._last_request + self.interval)
            delay = target - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._last_request = time.monotonic()

    def get_json(self, path):
        request = Request(API_URL + "/" + path.lstrip("/"), headers={
            "Accept": "application/json", "User-Agent": "workbench-factor-export/1.0",
        })
        for attempt in range(self.retries + 1):
            self._wait_for_slot()
            try:
                timeout = max(120, self.timeout) if path.rstrip("/").endswith("/mwtab") else self.timeout
                with urlopen(request, timeout=timeout) as response:
                    # JSON parsing also rejects HTML/text errors returned with HTTP 200.
                    if path.rstrip("/").endswith("/mwtab"):
                        return decode_mwtab_json(response.read())
                    return json.load(response)
            except HTTPError as error:
                delay = _retry_delay(error, attempt)
                error.close()
                if error.code != 429 and not 500 <= error.code < 600 or attempt == self.retries:
                    raise
                if error.code == 429:
                    with self._lock:
                        self._not_before = max(self._not_before, time.monotonic() + delay)
                else:
                    time.sleep(delay)
            except (URLError, OSError, HTTPException, ValueError):
                if attempt == self.retries:
                    raise
                time.sleep(min(30, 2 ** attempt))


def atomic_json(path, payload):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _matches_underscore_serialization(flat_pairs, native_pairs):
    """Recognize a demonstrated flat-API bug without changing native values.

    The flat endpoint splits ``__`` inside values, shifts the resulting pieces
    onto subsequent names, and leaves surplus pieces unnamed. Require the
    complete ordered flat response to match that exact transformation.
    """
    if not any("__" in value for _, value in native_pairs):
        return False
    names = [name for name, _ in native_pairs]
    values = [part for _, value in native_pairs for part in value.split("__")]
    projected = [(names[index] if index < len(names) else "", value)
                 for index, value in enumerate(values)]
    normalize = lambda pairs: [(name.strip(), " ".join(value.split())) for name, value in pairs]
    return normalize(flat_pairs) == normalize(projected)


def _parse_response(study_id, payload, client, cache_dir, refresh=False):
    """Recover missing/malformed flat fields using authoritative mwTab metadata."""
    records = normalize_records(payload)
    if any(record.get("study_id") != study_id for record in records):
        raise ValueError("Response contains a different study ID")
    try:
        parsed = parse_study(study_id, payload)
    except ValueError:
        parsed = None
    needs_native = (
        parsed is None or not parsed["factors"]
        or any(not row["factor_name"] for row in parsed["samples"])
        or any(isinstance(record.get("factors"), str)
               and re.search(r"\|\s*\|", record["factors"]) for record in records)
    )
    if not needs_native:
        return parsed

    native_dir = cache_dir / "mwtab"
    native_dir.mkdir(exist_ok=True)
    native_path = native_dir / (study_id + ".json")
    current_ids = client.current_analysis_ids(study_id) if isinstance(client, WorkbenchClient) else None

    def current_investigations(native):
        if not current_ids:
            return native
        investigations = native if isinstance(native, list) else [native]
        selected, covered = [], set()
        for investigation in investigations:
            if not isinstance(investigation, dict):
                raise ValueError("Expected a native investigation object")
            metadata = investigation.get("METABOLOMICS WORKBENCH", {})
            if not isinstance(metadata, dict) or metadata.get("STUDY_ID") != study_id:
                raise ValueError("Foreign study in native metadata")
            analysis_id = metadata.get("ANALYSIS_ID")
            if analysis_id in current_ids:
                selected.append(investigation)
                covered.add(analysis_id)
        if covered != set(current_ids):
            raise ValueError("Native metadata does not cover the current public analyses")
        return selected

    def parse_native(native):
        merged = merge_structured_factors(study_id, records, current_investigations(native))
        serialization_recoveries = {}
        # Native recovery must retain every named flat fact that can be parsed.
        for record in merged:
            try:
                flat_pairs = parse_factor_pairs(record["factors"])
            except ValueError:
                continue
            # The flat endpoint collapses formatting whitespace in some values.
            # Compare whitespace-equivalent facts, retaining native text in output
            # and the untouched flat string in raw_factors.
            named_pairs = Counter((name.strip(), " ".join(value.split())) for name, value in flat_pairs if name)
            native_pairs = Counter((name.strip(), " ".join(value.split())) for name, value in record["_factor_pairs"])
            if any(count > native_pairs[pair] for pair, count in named_pairs.items()):
                if not _matches_underscore_serialization(flat_pairs, record["_factor_pairs"]):
                    raise ValueError(f"mwTab recovery would discard a flat factor for sample {record['local_sample_id']!r}")
                key = (record.get("mb_sample_id", ""), record["local_sample_id"])
                serialization_recoveries[key] = {
                    "study_id": study_id, "mb_sample_id": key[0], "local_sample_id": key[1],
                    "warning": "Flat API split '__' inside a factor value; recovered exact native factors",
                }
        result = parse_study(study_id, merged)
        result["warnings"].extend(serialization_recoveries.values())
        result["factor_source"] = "mwtab"
        return result

    if native_path.exists() and not refresh:
        try:
            metadata = json.loads((native_dir / (study_id + ".metadata.json")).read_text(encoding="utf-8"))
            if metadata.get("cache_format") == MWTAB_CACHE_FORMAT:
                return parse_native(json.loads(native_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    if current_ids:
        # Study-level aggregates can include obsolete, syntactically broken
        # investigations. Request only analyses in the current public listing.
        native = []
        source_urls = []
        for analysis_id in current_ids:
            path = f"study/analysis_id/{analysis_id}/mwtab"
            item = client.get_json(path)
            native.extend(item if isinstance(item, list) else [item])
            source_urls.append(API_URL + "/" + path)
    else:
        path = f"study/study_id/{study_id}/mwtab"
        native = client.get_json(path)
        source_urls = [API_URL + "/" + path]
    atomic_json(native_path, native)
    atomic_json(native_dir / (study_id + ".metadata.json"), {
        "source_urls": source_urls, "fetched_at": utc_now(), "analysis_ids": current_ids,
        "cache_format": MWTAB_CACHE_FORMAT,
    })
    return parse_native(native)


def _study_result(study_id, client, cache_dir, refresh):
    path = cache_dir / (study_id + ".json")
    payload = None
    source = "api"
    if not path.exists() and not refresh and (cache_dir / "mwtab" / (study_id + ".json")).exists():
        try:
            parsed = _parse_response(study_id, [], client, cache_dir)
            return parsed, "native_cache", ""
        except (OSError, ValueError, HTTPException):
            pass
    if path.exists() and not refresh:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            records = normalize_records(cached)
            if any(record.get("study_id") != study_id or not isinstance(record.get("factors"), str)
                   or not isinstance(record.get("local_sample_id"), str) for record in records):
                raise ValueError("Invalid cached sample metadata")
            payload, source = cached, "cache"
        except (OSError, ValueError):
            pass  # Refetch incomplete/invalid caches; preserve raw replies for diagnosis.
    if payload is None:
        try:
            payload = client.get_json(f"study/study_id/{study_id}/factors")
        except (OSError, ValueError, HTTPException) as error:
            try:
                parsed = _parse_response(study_id, [], client, cache_dir, refresh)
                return parsed, "mwtab_api", ""
            except (OSError, ValueError, HTTPException) as native_error:
                return None, "api", f"fetch_error: {error}; mwTab fallback: {native_error}"
        try:
            atomic_json(path, payload)
        except OSError as error:
            return None, "api", "cache_error: " + str(error)
    try:
        return _parse_response(study_id, payload, client, cache_dir, refresh), source, ""
    except (OSError, HTTPException) as error:
        return None, source, "fetch_error: mwTab fallback: " + str(error)
    except ValueError as error:
        return None, source, "parse_error: " + str(error)


def _ordered_results(study_ids, client, cache_dir, refresh, workers):
    """Bound pending results so large sample tables cannot accumulate in memory."""
    iterator = iter(study_ids)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = deque()
        for study_id in list(study_ids)[:workers * 2]:
            next(iterator)
            pending.append((study_id, pool.submit(_study_result, study_id, client, cache_dir, refresh)))
        while pending:
            study_id, future = pending.popleft()
            yield study_id, future.result()
            following = next(iterator, None)
            if following is not None:
                pending.append((following, pool.submit(_study_result, following, client, cache_dir, refresh)))


def _write_review_outputs(output_dir, factors, catalog):
    with (output_dir / "factor_catalog.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=("factor_name", "study_count", "sample_factor_rows", "example_values_json"))
        writer.writeheader()
        for name in sorted(catalog):
            item = catalog[name]
            writer.writerow({"factor_name": name, "study_count": len(item["studies"]),
                             "sample_factor_rows": item["count"],
                             "example_values_json": json.dumps(sorted(item["values"]), ensure_ascii=False)})
    with (output_dir / "ontology_mapping_template.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MAPPING_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in factors:
            item = dict(row)
            item.update(mapping_method="", evidence="", review_status="pending")
            writer.writerow(item)


def export_factors(study_ids, client, output_dir, refresh=False, workers=1):
    """Export one study-factor row per name and sample evidence; return failures."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = output_dir / ".cache"
    cache_dir.mkdir(exist_ok=True)
    failures = 0
    all_factors = []
    catalog = {}
    filenames = ("factors.csv", "sample_factors.csv", "study_status.csv", "parsing_warnings.csv")
    fields = (FACTOR_FIELDS, SAMPLE_FIELDS, STATUS_FIELDS, WARNING_FIELDS)
    from contextlib import ExitStack
    with ExitStack() as stack:
        handles = [stack.enter_context((output_dir / name).open("w", newline="", encoding="utf-8")) for name in filenames]
        writers = [csv.DictWriter(handle, fieldnames=columns) for handle, columns in zip(handles, fields)]
        factors_writer, samples_writer, status_writer, warnings_writer = writers
        for writer in writers:
            writer.writeheader()
        for index, (study_id, (parsed, source, error)) in enumerate(
                _ordered_results(study_ids, client, cache_dir, refresh, workers), 1):
            if parsed is None:
                failures += 1
                status = error.split(":", 1)[0]
                status_writer.writerow({"study_id": study_id, "status": status, "source": source, "error": error})
                print(f"[{index}/{len(study_ids)}] {study_id}: {error}", file=sys.stderr)
            else:
                rows, evidence = parsed["factors"], parsed["samples"]
                factors_writer.writerows(rows)
                samples_writer.writerows(evidence)
                warnings_writer.writerows(parsed["warnings"])
                all_factors.extend(rows)
                for row in rows:
                    item = catalog.setdefault(row["factor_name"], {"studies": set(), "count": 0, "values": set()})
                    item["studies"].add(study_id)
                for row in evidence:
                    if row["factor_name"]:
                        item = catalog[row["factor_name"]]
                        item["count"] += 1
                        if row["factor_value"] and len(item["values"]) < 10:
                            item["values"].add(row["factor_value"])
                status_writer.writerow({
                    "study_id": study_id, "status": "ok" if rows else "no_factors",
                    "factor_count": len(rows), "sample_count": parsed["sample_count"],
                    "sample_factor_count": sum(bool(row["factor_name"]) for row in evidence),
                    "source": source, "warning_count": len(parsed["warnings"]), "error": "",
                    "factor_source": parsed["factor_source"],
                })
                print(f"[{index}/{len(study_ids)}] {study_id}: {len(rows)} factors, {parsed['sample_count']} sample records ({source})", file=sys.stderr)
            for handle in handles:
                handle.flush()
    _write_review_outputs(output_dir, all_factors, catalog)
    print(f"Wrote {len(all_factors)} factors from {len(study_ids) - failures}/{len(study_ids)} studies to {output_dir}", file=sys.stderr)
    return failures


def write_enrichment(output_dir, mappings):
    """Write an explicitly derived table, leaving deposited factors.csv intact."""
    with (output_dir / "factors.csv").open(newline="", encoding="utf-8") as handle:
        factors = list(csv.DictReader(handle))
    keys = {(row["study_id"], row["factor_name"]) for row in factors}
    with (output_dir / "factors_enriched.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FACTOR_FIELDS)
        writer.writeheader()
        writer.writerows(apply_mappings(factors, mappings))
    with (output_dir / "mapping_provenance.csv").open("w", newline="", encoding="utf-8") as handle:
        extra_fields = sorted({field for row in mappings.values() for field in row}
                              - set(MAPPING_FIELDS) - {"application_status"})
        writer = csv.DictWriter(handle, fieldnames=tuple(MAPPING_FIELDS) + tuple(extra_fields) + ("application_status",))
        writer.writeheader()
        for key, row in sorted(mappings.items()):
            writer.writerow(dict(row, application_status="applied" if key in keys else "unmatched"))
    return len(set(mappings) - keys)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--studies", nargs="+", metavar="ST_ID", help="Only these studies (default: all public studies)")
    parser.add_argument("--output-dir", type=Path, default=Path("results/workbench"))
    parser.add_argument("--refresh", action="store_true", help="Refetch valid caches")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent study workers (default: 4)")
    parser.add_argument("--interval", type=float, default=0.25, help="Shared minimum seconds between request starts (default: 0.25)")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--limit", type=int, help="Only the first N selected studies, for a pilot")
    parser.add_argument("--mappings", type=Path, help="CSV containing only explicitly approved ontology mappings")
    parser.add_argument("--update-factor-values", action="store_true",
                        help="Update factor_value_unique from existing CSVs without network access")
    args = parser.parse_args(argv)
    if args.workers < 1 or args.interval < 0 or args.timeout <= 0 or args.retries < 0 or args.limit is not None and args.limit < 1:
        parser.error("workers, timeout and limit must be positive; interval and retries nonnegative")
    started = utc_now()
    try:
        if args.update_factor_values:
            count = update_factor_value_unique(args.output_dir)
            print(f"Updated factor_value_unique for {count} study factors in {args.output_dir / 'factors.csv'}")
            return 0
        mappings = load_mappings(args.mappings) if args.mappings else None
        args.output_dir.mkdir(parents=True, exist_ok=True)
        cache_dir = args.output_dir / ".cache"
        cache_dir.mkdir(exist_ok=True)
        client = WorkbenchClient(args.timeout, args.retries, args.interval)
        if args.studies:
            study_ids = parse_study_ids([{"study_id": value} for value in args.studies])
            listing_source = "explicit study IDs"
        else:
            listing = client.get_json("study/study_id/ST/available")
            study_ids = parse_study_ids(listing)
            client.register_analyses(listing)
            atomic_json(cache_dir / "study_listing.json", listing)
            atomic_json(cache_dir / "listing_manifest.json", {
                "source_url": API_URL + "/study/study_id/ST/available", "fetched_at": utc_now(),
                "analysis_records": len(normalize_records(listing)), "study_count": len(study_ids), "study_ids": study_ids,
            })
            listing_source = API_URL + "/study/study_id/ST/available"
        if args.limit:
            study_ids = study_ids[:args.limit]
        metadata = {"started_at": started, "listing_source": listing_source, "study_count": len(study_ids),
                    "study_ids": study_ids, "workers": args.workers, "request_interval_seconds": args.interval,
                    "factor_ontology_annotations": "not inferred; approved mappings exported separately"}
        atomic_json(args.output_dir / "run_metadata.json", metadata)
        print(f"Exporting {len(study_ids)} unique Workbench studies", file=sys.stderr)
        failures = export_factors(study_ids, client, args.output_dir, args.refresh, args.workers)
        unmatched = write_enrichment(args.output_dir, mappings) if mappings is not None else 0
        metadata.update(completed_at=utc_now(), failed_studies=failures, unmatched_mappings=unmatched)
        atomic_json(args.output_dir / "run_metadata.json", metadata)
    except (OSError, ValueError, HTTPException, csv.Error) as error:
        print(f"Export failed: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted. Rerun the same command to reuse cached successful responses.", file=sys.stderr)
        return 130
    return 1 if failures or unmatched else 0


if __name__ == "__main__":
    sys.exit(main())
