#!/usr/bin/env python3
"""Validate a JEV acceptance policy using reviewed, study-disjoint labels."""

import argparse
import csv
import hashlib
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

from .analyze_ontology_accessions import identifier_rejection_reason


METRICS = (
    "selected_probability", "choice_confidence", "top_two_margin",
    "eligible_match_probability", "single_dimension_probability",
    "verification_probability",
)
AUDIT_FIELDS = (
    "study_id", "factor_name", "status", "selected_accession", *METRICS,
    "model", "prompt_version", "catalog_id", "request_id", "input_hash",
    "evidence", "acceptance_method", "error",
)
LABEL_FIELDS = ("study_id", "factor_name", "expected_accession", "split")
STUDY_ID = re.compile(r"ST[0-9]{6}\Z")
CONFIDENCE_CUTOFFS = (0.5, 0.7, 0.8, 0.9, 0.95, 0.98, 0.99)
MARGIN_CUTOFFS = (0.0, 0.05, 0.1, 0.2, 0.3)


def _read_csv(path, required):
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        if len(fields) != len(set(fields)):
            raise ValueError(f"{path}: duplicate column names")
        missing = set(required) - set(fields)
        if missing:
            raise ValueError(f"{path}: missing columns: {', '.join(sorted(missing))}")
        rows, seen = [], set()
        for row in reader:
            context = f"{path}: record ending at line {reader.line_num}"
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"{context}: malformed CSV record")
            key = _key(row, context)
            if key in seen:
                raise ValueError(f"{context}: duplicate study/factor key {key!r}")
            seen.add(key)
            rows.append(row)
    if not rows:
        raise ValueError(f"{path}: no data rows")
    return rows


def _key(row, context="Row"):
    study, name = row.get("study_id"), row.get("factor_name")
    if not isinstance(study, str) or not STUDY_ID.fullmatch(study):
        raise ValueError(f"{context}: invalid study_id")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{context}: factor_name must be nonblank text")
    return study, name


def load_audit(path):
    return _read_csv(path, AUDIT_FIELDS)


def load_labels(path):
    """Load explicit tune/test labels; a blank expected ID is a reviewed negative."""
    rows = _read_csv(path, LABEL_FIELDS)
    _validate_labels(rows)
    return rows


def _validate_labels(rows):
    studies = {"tune": set(), "test": set()}
    seen = set()
    for row in rows:
        key = _key(row, "Reviewed label")
        if key in seen:
            raise ValueError(f"Duplicate reviewed label key {key!r}")
        seen.add(key)
        if row.get("split") not in studies:
            raise ValueError(f"Label {key!r}: explicit split must be 'tune' or 'test'")
        if "review_status" in row and row["review_status"] not in {"reviewed", "approved"}:
            raise ValueError(f"Label {key!r}: review_status must be 'reviewed' or 'approved'; pending templates are not gold labels")
        expected = row.get("expected_accession")
        if not isinstance(expected, str):
            raise ValueError(f"Label {key!r}: expected_accession must be text, or blank for no eligible mapping")
        if expected and identifier_rejection_reason(expected) is not None:
            raise ValueError(f"Label {key!r}: invalid expected_accession")
        studies[row["split"]].add(key[0])
    if not all(studies.values()):
        raise ValueError("Reviewed labels must contain both tune and test studies")
    if studies["tune"] & studies["test"]:
        raise ValueError("Tune and test must be study-disjoint, even for different factor names")


def _candidate(row):
    """Return finite scores for an actual error-free proposal, never infer scores."""
    if row.get("error", "").strip():
        return None, "audit_error"
    accession = row.get("selected_accession")
    if not isinstance(accession, str) or not accession:
        return None, "no_selected_accession"
    if identifier_rejection_reason(accession) is not None:
        return None, "invalid_selected_accession"
    scores = {}
    for metric in METRICS:
        try:
            score = float(row.get(metric, ""))
        except (TypeError, ValueError):
            return None, "missing_or_invalid_score"
        if not math.isfinite(score) or not 0 <= score <= 1:
            return None, "missing_or_invalid_score"
        scores[metric] = score
    return scores, None


def _policies():
    for cutoff in CONFIDENCE_CUTOFFS:
        for margin in MARGIN_CUTOFFS:
            yield {metric: margin if metric == "top_two_margin" else cutoff for metric in METRICS}


def _evaluate(records, thresholds):
    accepted = [item for item in records if item["scores"] is not None
                and all(item["scores"][metric] >= thresholds[metric] for metric in METRICS)]
    correct = sum(item["audit"]["selected_accession"] == item["label"]["expected_accession"] for item in accepted)
    n = len(accepted)
    precision = correct / n if n else None
    interval = None
    if n:
        z = 1.959963984540054
        center = (precision + z * z / (2 * n)) / (1 + z * z / n)
        radius = z * math.sqrt(precision * (1 - precision) / n + z * z / (4 * n * n)) / (1 + z * z / n)
        interval = [max(0.0, center - radius), min(1.0, center + radius)]
    return {
        "total_rows": len(records),
        "eligible_rows": sum(item["scores"] is not None for item in records),
        "accepted": n, "correct": correct, "incorrect": n - correct,
        "precision": precision, "coverage": n / len(records),
        "study_count": len({item["label"]["study_id"] for item in records}),
        "accepted_studies": len({item["label"]["study_id"] for item in accepted}),
        "precision_wilson_95_interval": interval,
        "ineligible_reasons": dict(Counter(item["reason"] for item in records if item["reason"])),
    }


def _row_hash(rows):
    raw = json.dumps(rows, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def calibrate(audit_rows, labels, target_precision=0.98, min_accepted=20,
              model="jev-1.14.0", catalog_id=None, label_source_sha256=None,
              audit_source_sha256=None):
    """Select on tune only, then evaluate the frozen policy once on held-out test.

    Passing policies meet the requested *observed* precision and count on both
    splits. This does not convert model-reported confidence features into
    calibrated correctness probabilities or guarantee future precision.
    """
    if not math.isfinite(target_precision) or not 0 < target_precision <= 1:
        raise ValueError("target_precision must be finite and between 0 and 1")
    if type(min_accepted) is not int or min_accepted < 1:
        raise ValueError("min_accepted must be a positive integer")
    audit_rows, labels = list(audit_rows), list(labels)
    _validate_labels(labels)
    audit = {}
    for row in audit_rows:
        key = _key(row, "Audit row")
        if key in audit:
            raise ValueError(f"Duplicate audit key {key!r}")
        audit[key] = row
    if not audit:
        raise ValueError("Audit must contain decisions")
    profile = {}
    for field in ("model", "prompt_version", "catalog_id"):
        values = {row.get(field) for row in audit_rows}
        if len(values) != 1 or any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError(f"Audit must have one nonblank {field}")
        profile[field] = values.pop()
    if profile["model"] != model:
        raise ValueError("Audit model does not match requested model")
    if catalog_id is not None and profile["catalog_id"] != catalog_id:
        raise ValueError("Audit catalog_id does not match requested catalog_id")
    records = {"tune": [], "test": []}
    for label in labels:
        key = _key(label)
        if key not in audit:
            raise ValueError(f"Reviewed label has no exact matching audit row: {key!r}")
        scores, reason = _candidate(audit[key])
        records[label["split"]].append({"label": label, "audit": audit[key], "scores": scores, "reason": reason})

    passing = []
    for thresholds in _policies():
        metrics = _evaluate(records["tune"], thresholds)
        if metrics["accepted"] >= min_accepted and metrics["precision"] >= target_precision:
            passing.append((thresholds, metrics))
    if not passing:
        raise ValueError("No tune policy meets target precision and min_accepted; no calibration config produced")
    thresholds, tune = min(passing, key=lambda item: (
        -item[1]["accepted"], -item[1]["precision"],
        sum(item[0].values()), tuple(item[0][metric] for metric in METRICS),
    ))
    test = _evaluate(records["test"], thresholds)
    if test["accepted"] < min_accepted or test["precision"] < target_precision:
        raise ValueError(f"Frozen tune policy failed held-out test: accepted={test['accepted']}, precision={test['precision']}; no calibration config produced")
    return {
        "schema_version": 1, **profile, "calibrated": True,
        "thresholds": thresholds,
        "label_source_sha256": label_source_sha256 or _row_hash(labels),
        "audit_source_sha256": audit_source_sha256 or _row_hash(audit_rows),
        "diagnostics": {
            "target_precision": target_precision, "min_accepted": min_accepted,
            "tune": tune, "test": test,
            "reviewed_rows": len(labels), "audit_rows": len(audit_rows),
            "study_disjoint": True, "policies_considered": len(CONFIDENCE_CUTOFFS) * len(MARGIN_CUTOFFS),
            "policy_selection_split": "tune", "heldout_evaluations": 1,
            "selection_rule": "maximum tune coverage, then precision, then lower thresholds",
            "score_interpretation": "Model-reported features, not calibrated probabilities of correctness",
            "precision_interpretation": "Observed reviewed-sample precision; not a guarantee of future precision. Wilson intervals are descriptive and do not gate acceptance.",
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target-precision", type=float, default=0.98)
    parser.add_argument("--min-accepted", type=int, default=20)
    parser.add_argument("--model", default="jev-1.14.0")
    parser.add_argument("--catalog-id")
    args = parser.parse_args(argv)
    try:
        if args.output.resolve() in {args.audit.resolve(), args.labels.resolve()}:
            raise ValueError("Calibration output cannot overwrite the audit or reviewed labels")
        config = calibrate(
            load_audit(args.audit), load_labels(args.labels), args.target_precision,
            args.min_accepted, args.model, args.catalog_id,
            hashlib.sha256(args.labels.read_bytes()).hexdigest(),
            hashlib.sha256(args.audit.read_bytes()).hexdigest(),
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        temporary.replace(args.output)
    except (OSError, ValueError, csv.Error) as error:
        print(f"Calibration failed: {error}. No config was written.", file=sys.stderr)
        return 1
    print(json.dumps(config["diagnostics"], indent=2, allow_nan=False))
    print(f"Validated acceptance policy: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
