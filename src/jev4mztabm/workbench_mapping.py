"""Apply explicit, accepted ontology annotations to Workbench study factors.

Mappings are study-specific and never inferred by this module. Provenance should
distinguish individual review from acceptance under a validated model policy.
Callers should retain the raw factor export and write enriched rows separately.
"""

import csv
import re
from pathlib import Path

from .analyze_ontology_accessions import identifier_rejection_reason


MAPPING_FIELDS = (
    "study_id", "factor_name", "factor_type", "ontology_source",
    "ontology_accession", "ontology_version", "ontology_file",
    "mapping_method", "evidence", "review_status",
)
ANNOTATION_FIELDS = (
    "factor_type", "ontology_source", "ontology_accession",
    "ontology_version", "ontology_file",
)
_REQUIRED_VALUES = (
    "factor_name", "factor_type", "ontology_source", "ontology_accession",
    "mapping_method", "evidence",
)
_STUDY_ID = re.compile(r"ST[0-9]{6}\Z")


def _validate_mapping(row, context, allowed_accessions=None):
    """Validate approvals and annotation structure without changing spelling."""
    for field in MAPPING_FIELDS:
        if not isinstance(row.get(field), str):
            raise ValueError(f"{context}: missing or invalid {field}")
    if not _STUDY_ID.fullmatch(row["study_id"]):
        raise ValueError(f"{context}: study_id must be an exact ST plus six digits")
    if row["review_status"] != "approved":
        raise ValueError(f"{context}: review_status must be 'approved'")
    for field in _REQUIRED_VALUES:
        if not row[field].strip():
            raise ValueError(f"{context}: {field} must be nonblank")
    reason = identifier_rejection_reason(row["ontology_accession"])
    if reason is not None:
        raise ValueError(f"{context}: invalid ontology_accession ({reason})")
    if allowed_accessions is not None and row["ontology_accession"] not in allowed_accessions:
        raise ValueError(f"{context}: ontology_accession is outside the allowed accession list")


def load_mappings(path, *, allowed_accessions=None):
    """Read approved CSV rows keyed by exact ``(study_id, factor_name)``.

    All ``MAPPING_FIELDS`` columns are required; ontology version and file may
    be empty. Extra columns are preserved for provenance. Invalid, unapproved,
    malformed, or duplicate rows raise ValueError rather than being skipped.
    Identifier validation checks structure, not term existence or whether the
    accepted annotation is scientifically appropriate. When supplied,
    ``allowed_accessions`` requires exact membership in that fixed vocabulary.
    """
    mappings = {}
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("Mapping CSV must contain a header")
        if len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError("Mapping CSV contains duplicate column names")
        missing = set(MAPPING_FIELDS) - set(reader.fieldnames)
        if missing:
            raise ValueError("Mapping CSV is missing columns: " + ", ".join(sorted(missing)))
        try:
            for row in reader:
                context = f"Mapping CSV record ending at line {reader.line_num}"
                if None in row or any(value is None for value in row.values()):
                    raise ValueError(f"{context}: malformed CSV record")
                _validate_mapping(row, context, allowed_accessions)
                key = (row["study_id"], row["factor_name"])
                if key in mappings:
                    raise ValueError(f"{context}: duplicate mapping for {key!r}")
                mappings[key] = dict(row)
        except csv.Error as error:
            raise ValueError(f"Invalid mapping CSV at line {reader.line_num}: {error}") from error
    return mappings


def apply_mappings(factor_rows, mappings, *, allowed_accessions=None):
    """Return copied factor rows with exact approved annotations applied.

    Only ``ANNOTATION_FIELDS`` may change. Keys with no exact factor match are
    unused; callers can audit them with ``set(mappings) - matched_factor_keys``.
    Neither the input factors nor mappings are mutated. Directly supplied
    mappings receive the same approval and identifier checks as CSV mappings.
    """
    enriched = []
    for factor in factor_rows:
        row = dict(factor)
        key = (row["study_id"], row["factor_name"])
        mapping = mappings.get(key)
        if mapping is not None:
            _validate_mapping(mapping, f"Mapping for {key!r}", allowed_accessions)
            if (mapping["study_id"], mapping["factor_name"]) != key:
                raise ValueError(f"Mapping key does not match its row: {key!r}")
            for field in ANNOTATION_FIELDS:
                row[field] = mapping[field]
        enriched.append(row)
    return enriched
