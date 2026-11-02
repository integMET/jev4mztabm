"""Build a fixed ontology allowlist using exact official OLS term metadata.

Only canonical HTTP EFO accessions with an explicit non-obsolete term record
and verified EFO ontology metadata can become automatic mapping candidates.
Catalog hashes detect changed content; they are integrity checks, not signatures.
"""

import csv
import hashlib
import io
import json
import re
import tempfile
import time
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


DEFAULT_ACCESSIONS_PATH = Path("results/ontology_analysis_efo_only/ontology_accession_frequencies.csv")
OLS_URL = "https://www.ebi.ac.uk/ols4/api/ontologies/efo"
EFO_ACCESSION = re.compile(r"http://www\.ebi\.ac\.uk/efo/EFO_[0-9]{7}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
STATUSES = {"eligible", "obsolete", "unresolved", "invalid", "out_of_namespace"}
TERM_FIELDS = {
    "ontology_accession", "label", "definition", "synonyms", "ontology_source",
    "ontology_version", "ontology_file", "is_obsolete", "status", "metadata_url",
}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _catalog_id(terms):
    ordered = sorted(terms, key=lambda term: term["ontology_accession"])
    return hashlib.sha256(_canonical(ordered)).hexdigest()


def _atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            handle.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _fetch_json(url):
    """Read public metadata, retrying throttling, server and connection errors."""
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "workbench-ontology-catalog/1.0"})
    for attempt in range(4):
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            transient = error.code == 429 or 500 <= error.code < 600
            error.close()
            if not transient or attempt == 3:
                raise
        except (OSError, HTTPException):
            if attempt == 3:
                raise
        time.sleep(2 ** attempt)


def _cache_path(url, cache_dir):
    return Path(cache_dir) / (hashlib.sha256(url.encode("utf-8")).hexdigest() + ".json")


def _cache_entry(url, cache_dir, release=None):
    try:
        cached = json.loads(_cache_path(url, cache_dir).read_text(encoding="utf-8"))
        if (isinstance(cached, dict) and type(cached.get("schema_version")) is int
                and cached["schema_version"] == 1 and cached.get("request_url") == url
                and isinstance(cached.get("fetched_at"), str) and "payload" in cached
                and cached.get("ontology_release") == release):
            return cached
    except (OSError, ValueError):
        pass
    return None


def _cached_json(url, cache_dir, refresh, fetcher, *, release=None, pending=None):
    cached = None if refresh else _cache_entry(url, cache_dir, release)
    if cached is not None:
        return cached["payload"]
    payload = fetcher(url)
    entry = {"schema_version": 1, "request_url": url,
             "fetched_at": datetime.now(timezone.utc).isoformat(), "payload": payload}
    if release is not None:
        entry["ontology_release"] = release
        pending[_cache_path(url, cache_dir)] = entry
    else:
        _atomic_json(_cache_path(url, cache_dir), entry)
    return payload


def _release_signature(payload):
    config = payload["config"]
    snapshot = {key: payload.get(key) for key in ("ontologyId", "loaded", "updated", "version", "fileHash", "status")}
    snapshot["config"] = {key: config.get(key) for key in ("id", "version", "versionIri", "fileLocation")}
    return hashlib.sha256(_canonical(snapshot)).hexdigest()


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def _bind_legacy_entries(urls, cache_dir, before, after, release):
    """Migrate only replies bracketed by matching official release snapshots."""
    if (before is None or after is None or _release_signature(before["payload"]) != release
            or not before["payload"].get("loaded") or not _ontology_metadata(before["payload"])[0]):
        return
    start, end = _timestamp(before["fetched_at"]), _timestamp(after["fetched_at"])
    if start is None or end is None or start > end:
        return
    for url in urls:
        entry = _cache_entry(url, cache_dir)
        fetched = _timestamp(entry["fetched_at"]) if entry else None
        if fetched is not None and start <= fetched <= end:
            entry.update(ontology_release=release, release_checked_at=after["fetched_at"])
            _atomic_json(_cache_path(url, cache_dir), entry)


def _public_url(value):
    try:
        parsed = urlsplit(value)
        return (parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                and not parsed.username and not parsed.password
                and not any(character.isspace() or ord(character) < 32 for character in value))
    except ValueError:
        return False


def _namespace_status(accession):
    if EFO_ACCESSION.fullmatch(accession):
        return None
    if not _public_url(accession) or accession.startswith("http://www.ebi.ac.uk/efo/EFO_"):
        return "invalid"
    return "out_of_namespace"


def _metadata_url(accession):
    return OLS_URL + "/terms?" + urlencode({"iri": accession})


def _optional_text(value, field):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"Expected text for {field}")
    return value.strip()


def _ontology_metadata(payload):
    if not isinstance(payload, dict) or payload.get("ontologyId") != "efo":
        raise ValueError("OLS ontology metadata does not identify efo")
    config = payload.get("config")
    if not isinstance(config, dict) or config.get("id") != "efo":
        raise ValueError("OLS ontology configuration does not identify efo")
    if payload.get("status", "LOADED") != "LOADED":
        raise ValueError("OLS ontology is not fully loaded")
    version, top_version = _optional_text(config.get("version"), "ontology version"), _optional_text(payload.get("version"), "ontology version")
    if version and top_version and version != top_version:
        raise ValueError("OLS ontology version fields disagree")
    version = version or top_version
    file_url = _optional_text(config.get("versionIri"), "version IRI") or _optional_text(config.get("fileLocation"), "ontology file")
    if file_url and not _public_url(file_url):
        raise ValueError("OLS ontology file is not an HTTP(S) URL")
    return version, file_url


def _term_metadata(payload, accession):
    if not isinstance(payload, dict):
        raise ValueError("Expected an OLS term response object")
    embedded = payload.get("_embedded")
    terms = embedded.get("terms") if isinstance(embedded, dict) else None
    if not isinstance(terms, list) or any(not isinstance(term, dict) for term in terms):
        raise ValueError("Expected an OLS terms list")
    matches = [term for term in terms if term.get("iri") == accession]
    if len(matches) != 1:
        raise ValueError("OLS did not return exactly one term matching the requested IRI")
    term = matches[0]
    if term.get("ontology_name", "efo") != "efo":
        raise ValueError("OLS term does not belong to the requested EFO resource")
    label = _optional_text(term.get("label"), "canonical label")
    if not label:
        raise ValueError("OLS term has no canonical label")
    obsolete = term.get("is_obsolete")
    if type(obsolete) is not bool:
        raise ValueError("OLS term has no explicit boolean is_obsolete flag")
    definitions = term.get("description") or []
    if isinstance(definitions, str):
        definitions = [definitions]
    synonyms = term.get("synonyms") or []
    if (not isinstance(definitions, list) or not isinstance(synonyms, list)
            or any(not isinstance(value, str) for value in definitions + synonyms)):
        raise ValueError("OLS definitions and synonyms must be text lists")
    return label, "\n\n".join(value.strip() for value in definitions if value.strip()), sorted(set(synonyms)), obsolete


def build_catalog(accessions_path, cache_dir, *, refresh=False, fetch_json=None):
    """Resolve the fixed CSV allowlist; injected ``fetch_json(url)`` avoids I/O.

    Failed or incomplete lookups remain unresolved. Non-EFO URLs are retained
    without network lookups. Cached replies are keyed by their exact URL and
    carry request URL/timestamp provenance plus the verified ontology release.
    New term replies are staged until a second ontology lookup confirms the
    release did not change. Cached-only rebuilds retain their prior snapshot.
    Use refresh to replace cached data. Release changes fail the entire build.
    """
    raw = Path(accessions_path).read_bytes()
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    if (not reader.fieldnames or "ontology_accession" not in reader.fieldnames
            or len(set(reader.fieldnames)) != len(reader.fieldnames)):
        raise ValueError("Accession CSV must contain ontology_accession")
    accessions = []
    for row in reader:
        accession = row.get("ontology_accession")
        if not isinstance(accession, str) or None in row:
            raise ValueError("Malformed accession CSV record")
        accessions.append(accession)
    if not accessions or len(set(accessions)) != len(accessions):
        raise ValueError("Accession CSV must contain a nonempty, unique allowlist")
    fetcher = fetch_json or _fetch_json
    ontology_info, ontology_error, release = None, "", None
    pending = {}
    term_urls = [_metadata_url(accession) for accession in accessions if EFO_ACCESSION.fullmatch(accession)]
    if term_urls:
        try:
            before = _cache_entry(OLS_URL, cache_dir)
            ontology_payload = _cached_json(OLS_URL, cache_dir, refresh, fetcher)
            ontology_info = _ontology_metadata(ontology_payload)
            release = _release_signature(ontology_payload)
            if not refresh and before is not None and any(_cache_entry(url, cache_dir, release) is None for url in term_urls):
                ontology_payload = _cached_json(OLS_URL, cache_dir, True, fetcher)
                ontology_info, release = _ontology_metadata(ontology_payload), _release_signature(ontology_payload)
                _bind_legacy_entries(term_urls, cache_dir, before, _cache_entry(OLS_URL, cache_dir), release)
        except (OSError, ValueError, HTTPException) as error:
            ontology_info, release = None, None
            ontology_error = "Ontology metadata: " + str(error)
    terms = []
    for accession in sorted(accessions):
        namespace_status = _namespace_status(accession)
        term = dict(ontology_accession=accession, label="", definition="", synonyms=[],
                    ontology_source="", ontology_version="", ontology_file="", is_obsolete=None,
                    status=namespace_status or "unresolved", metadata_url="")
        if namespace_status:
            terms.append(term)
            continue
        term["metadata_url"] = _metadata_url(accession)
        try:
            details = _term_metadata(_cached_json(term["metadata_url"], cache_dir, refresh, fetcher,
                                                 release=release, pending=pending), accession)
            term["label"], term["definition"], term["synonyms"], term["is_obsolete"] = details
            term["ontology_source"] = "EFO"
            if ontology_info is not None:
                term["ontology_version"], term["ontology_file"] = ontology_info
            if term["is_obsolete"]:
                term["status"] = "obsolete"
            elif ontology_info is not None:
                term["status"] = "eligible"
            else:
                term["error"] = ontology_error
        except (OSError, ValueError, HTTPException) as error:
            term["error"] = str(error)
        terms.append(term)
    if pending:
        try:
            final_payload = _cached_json(OLS_URL, cache_dir, True, fetcher)
            _ontology_metadata(final_payload)
            if _release_signature(final_payload) != release:
                raise ValueError("OLS release changed while collecting term metadata; rerun the catalog build")
        except (OSError, ValueError, HTTPException) as error:
            raise ValueError("Cannot verify a consistent OLS release: " + str(error)) from error
        for path, entry in pending.items():
            _atomic_json(path, entry)
    catalog = {"schema_version": 1, "catalog_id": _catalog_id(terms),
               "source_sha256": hashlib.sha256(raw).hexdigest(), "terms": terms}
    _validate_catalog(catalog)
    return catalog


def _validate_catalog(catalog):
    if (not isinstance(catalog, dict) or type(catalog.get("schema_version")) is not int
            or catalog["schema_version"] != 1 or set(catalog) != {"schema_version", "catalog_id", "source_sha256", "terms"}):
        raise ValueError("Unsupported catalog schema")
    for field in ("catalog_id", "source_sha256"):
        if not isinstance(catalog.get(field), str) or not SHA256.fullmatch(catalog[field]):
            raise ValueError(f"Invalid catalog {field}")
    terms = catalog.get("terms")
    if not isinstance(terms, list) or not terms:
        raise ValueError("Catalog terms must be a nonempty list")
    seen = set()
    for term in terms:
        if not isinstance(term, dict) or not TERM_FIELDS <= set(term) or set(term) - TERM_FIELDS - {"error"}:
            raise ValueError("Invalid catalog term schema")
        for field in TERM_FIELDS - {"synonyms", "is_obsolete"}:
            if not isinstance(term[field], str):
                raise ValueError(f"Invalid term {field}")
        accession = term["ontology_accession"]
        if accession in seen:
            raise ValueError("Duplicate catalog accession")
        seen.add(accession)
        if (not isinstance(term["synonyms"], list) or any(not isinstance(value, str) for value in term["synonyms"])
                or term["is_obsolete"] is not None and type(term["is_obsolete"]) is not bool
                or "error" in term and not isinstance(term["error"], str)):
            raise ValueError("Invalid term annotations")
        status = term["status"]
        expected_namespace = _namespace_status(accession)
        if status not in STATUSES or expected_namespace and status != expected_namespace:
            raise ValueError("Term status does not match its accession namespace")
        if not expected_namespace:
            if status in {"invalid", "out_of_namespace"} or term["metadata_url"] != _metadata_url(accession):
                raise ValueError("Invalid EFO metadata provenance")
        elif term["metadata_url"]:
            raise ValueError("Out-of-namespace terms cannot claim EFO metadata provenance")
        if status in {"eligible", "obsolete"}:
            if (not term["label"].strip() or term["ontology_source"] != "EFO"
                    or term["is_obsolete"] is not (status == "obsolete")):
                raise ValueError("Eligible/obsolete term requires verified positive metadata")
        if status == "eligible" and term.get("error"):
            raise ValueError("Eligible term cannot have unresolved metadata errors")
        if term["ontology_file"] and not _public_url(term["ontology_file"]):
            raise ValueError("Invalid ontology file URL")
    if catalog["catalog_id"] != _catalog_id(terms):
        raise ValueError("Catalog content hash does not match its terms")


def load_catalog(path):
    """Load a catalog only after checking its schema, eligibility and self-hash."""
    catalog = json.loads(Path(path).read_text(encoding="utf-8"))
    _validate_catalog(catalog)
    return catalog


def save_catalog(path, catalog):
    """Validate and atomically save a catalog; never silently recompute its hash."""
    _validate_catalog(catalog)
    _atomic_json(path, catalog)
