"""Recover factor pairs from authoritative Workbench mwTab sample metadata.

The flat factors endpoint can omit named fields or render literal delimiters
ambiguously. Native mwTab JSON can also contain repeated keys in Factors or
Additional sample data objects. ``decode_mwtab_json`` retains those ordered
pairs in distinct reserved markers within ordinary, cache-serializable JSON.
Tag caches with MWTAB_CACHE_FORMAT; older JSON decoders may already have
discarded repeated factor values.

The merge retains flat records and raw strings while attaching native factor
pairs for the caller to parse. It performs no ontology mapping.
"""

import json


FACTOR_PAIRS_MARKER = "__mwtab_factor_pairs__"
ADDITIONAL_PAIRS_MARKER = "__mwtab_additional_pairs__"
MWTAB_CACHE_FORMAT = "lossless-factor-pairs-v1"


class _RawObject:
    def __init__(self, pairs):
        self.pairs = pairs


def decode_mwtab_json(raw):
    """Decode source mwTab JSON without discarding repeated sample fields.

    Repeated keys are accepted only in a sample's native ``Factors`` or
    ``Additional sample data`` object. Such objects retain ordinary fields
    plus the corresponding reserved marker, listing every original
    ``[name, value]`` pair in order. Reserved marker names in source JSON and
    duplicate keys elsewhere are rejected. The resulting dictionaries/lists
    can be cached with ordinary ``json.dump``/``json.load``. This decoder is
    for source responses, not already marked cache documents.
    """
    if not isinstance(raw, (bytes, str)):
        raise ValueError("Expected source mwTab JSON bytes or text")

    def reject_constant(value):
        raise ValueError(f"Invalid JSON constant: {value}")

    payload = json.loads(raw, object_pairs_hook=_RawObject, parse_constant=reject_constant)

    def convert(value, path=()):
        if isinstance(value, _RawObject):
            names = [name for name, item in value.pairs]
            for marker in (FACTOR_PAIRS_MARKER, ADDITIONAL_PAIRS_MARKER):
                if marker in names:
                    raise ValueError(f"Source JSON uses reserved marker {marker!r}")
            duplicates = len(set(names)) != len(names)
            # Only direct sample fields of a root investigation (or a root
            # investigation list) may contain repeated descriptive keys.
            sample_path = path[1:] if len(path) == 4 and isinstance(path[0], int) else path
            is_sample_field = (
                len(sample_path) == 3 and sample_path[0] == "SUBJECT_SAMPLE_FACTORS"
                and isinstance(sample_path[1], int)
            )
            marker = None
            if is_sample_field:
                marker = {
                    "Factors": FACTOR_PAIRS_MARKER,
                    "Additional sample data": ADDITIONAL_PAIRS_MARKER,
                }.get(sample_path[2])
            if duplicates and marker is None:
                raise ValueError(f"Duplicate JSON keys outside native sample descriptive objects at {path!r}")
            pairs = [(name, convert(item, path + (name,))) for name, item in value.pairs]
            result = dict(pairs)
            if duplicates:
                result[marker] = [[name, item] for name, item in pairs]
            return result
        if isinstance(value, list):
            return [convert(item, path + (index,)) for index, item in enumerate(value)]
        return value

    return convert(payload)


def _scalar_text(value):
    """Preserve text; represent other JSON scalars as text, with null blank."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (bool, int, float)):
        try:
            return json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (ValueError, OverflowError) as error:
            raise ValueError("Native factor values must be finite JSON scalars") from error
    raise ValueError("Native factor values must be text or JSON scalars")


def _investigations(payload):
    if payload is None:
        return []
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return payload
    raise ValueError("Expected a mwTab investigation object or list")


def _factor_pairs(factors):
    """Validate a marked cache object before trusting its ordered factor pairs."""
    if FACTOR_PAIRS_MARKER not in factors:
        return list(factors.items())
    pairs = factors[FACTOR_PAIRS_MARKER]
    if not isinstance(pairs, list):
        raise ValueError("Native factor pair marker must be a list")
    projection = {}
    for pair in pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError("Native factor pair marker must contain name/value pairs")
        name, value = pair
        if not isinstance(name, str) or name == FACTOR_PAIRS_MARKER:
            raise ValueError("Invalid factor name in native factor pair marker")
        projection[name] = _scalar_text(value)
    fields = {name: _scalar_text(value) for name, value in factors.items() if name != FACTOR_PAIRS_MARKER}
    if len(projection) == len(pairs) or projection != fields:
        raise ValueError("Native factor pair marker does not match its duplicated Factors fields")
    return pairs


def merge_structured_factors(study_id, flat_records, mwtab_payload):
    """Attach native factor pairs to copied flat records, adding native extras.

    Native samples are joined by exact ``Sample ID``/``local_sample_id``.
    Repeated samples across analyses may contribute additional factor names or
    occurrences, but shared name/occurrence pairs must have identical scalar
    values. All known flat samples must be covered; an empty native result can
    only accompany empty flat data.

    Original flat ``factors`` strings and other metadata remain untouched.
    ``_factor_pairs`` preserves native names, including empty names, and
    ``_analysis_ids`` records the analyses providing each sample. Native-only
    samples receive blank identifiers/source fields that mwTab does not supply.
    """
    if not isinstance(study_id, str) or not study_id:
        raise ValueError("Expected a nonblank requested study ID")
    try:
        flat_rows = list(flat_records)
    except TypeError as error:
        raise ValueError("Expected a sequence of flat sample records") from error

    flat_ids = set()
    for record in flat_rows:
        if not isinstance(record, dict):
            raise ValueError("Every flat sample record must be an object")
        if record.get("study_id") != study_id:
            raise ValueError(f"Requested {study_id}, received flat record for {record.get('study_id')!r}")
        sample_id = record.get("local_sample_id")
        if not isinstance(sample_id, str) or not sample_id.strip():
            raise ValueError("Every flat sample must have a nonblank local_sample_id")
        flat_ids.add(sample_id)

    native_pairs = {}
    native_analyses = {}
    for investigation in _investigations(mwtab_payload):
        if not isinstance(investigation, dict):
            raise ValueError("Every mwTab investigation must be an object")
        metadata = investigation.get("METABOLOMICS WORKBENCH")
        if not isinstance(metadata, dict) or metadata.get("STUDY_ID") != study_id:
            received = metadata.get("STUDY_ID") if isinstance(metadata, dict) else None
            raise ValueError(f"Requested {study_id}, received mwTab study {received!r}")
        analysis_id = metadata.get("ANALYSIS_ID", "")
        if analysis_id is None:
            analysis_id = ""
        if not isinstance(analysis_id, str):
            raise ValueError("Native ANALYSIS_ID must be text when present")
        if "SUBJECT_SAMPLE_FACTORS" not in investigation:
            raise ValueError("mwTab investigation is missing SUBJECT_SAMPLE_FACTORS")
        samples = investigation["SUBJECT_SAMPLE_FACTORS"]
        if not isinstance(samples, list):
            raise ValueError("Native SUBJECT_SAMPLE_FACTORS must be a list")
        for sample in samples:
            if not isinstance(sample, dict):
                raise ValueError("Every native sample must be an object")
            sample_id = sample.get("Sample ID")
            if not isinstance(sample_id, str) or not sample_id.strip():
                raise ValueError("Every native sample must have a nonblank Sample ID")
            factors = sample.get("Factors")
            if not isinstance(factors, dict):
                raise ValueError(f"Native sample {sample_id!r} must contain a Factors object")
            pairs = native_pairs.setdefault(sample_id, {})
            analyses = native_analyses.setdefault(sample_id, {})
            if analysis_id:
                analyses[analysis_id] = None
            occurrences = {}
            for name, value in _factor_pairs(factors):
                if not isinstance(name, str):
                    raise ValueError("Native factor names must be text")
                text = _scalar_text(value)
                occurrence = occurrences.get(name, 0) + 1
                occurrences[name] = occurrence
                key = (name, occurrence)
                if key in pairs and pairs[key] != text:
                    raise ValueError(f"Conflicting native values for sample {sample_id!r}, factor {name!r}")
                pairs[key] = text

    missing = flat_ids - set(native_pairs)
    if missing:
        examples = ", ".join(repr(sample_id) for sample_id in sorted(missing)[:5])
        raise ValueError(f"Native metadata does not cover {len(missing)} flat samples: {examples}")

    def attach(record, sample_id):
        result = dict(record)
        result["_factor_pairs"] = [(name, value) for (name, occurrence), value in native_pairs[sample_id].items()]
        result["_analysis_ids"] = ";".join(native_analyses[sample_id])
        return result

    merged = [attach(record, record["local_sample_id"]) for record in flat_rows]
    for sample_id in native_pairs:
        if sample_id not in flat_ids:
            record = {
                "study_id": study_id,
                "mb_sample_id": "",
                "local_sample_id": sample_id,
                "sample_source": "",
                "factors": "",
                "raw_factors": "",
            }
            merged.append(attach(record, sample_id))
    return merged
