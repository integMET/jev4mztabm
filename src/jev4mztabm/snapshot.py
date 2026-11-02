"""Archive result fingerprints and replay recorded Jev proposals offline."""

import argparse
import csv
import hashlib
import json
import math
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import __version__


ARTIFACTS = (
    "factors.csv", "study_status.csv",
    "workbench/factors.csv", "workbench/sample_factors.csv",
    "workbench/study_status.csv", "workbench/parsing_warnings.csv",
    "workbench/factor_catalog.csv", "workbench/ontology_mapping_template.csv",
    "workbench/run_metadata.json",
)
ANALYSIS_FILES = (
    "factors_filtered.csv", "ontology_accession_frequencies.csv",
    "ontology_accession_frequency_distribution.csv", "excluded_ontology_accessions.csv",
    "ontology_accession_frequency_distribution.png", "ontology_accession_frequency_distribution.svg",
)
JEV_FILES = (
    "ontology_target_catalog.json", "contexts.jsonl", "pilot_contexts.jsonl",
    "reviewed_labels.csv", "mapping_decisions.csv", "accepted_mappings.csv",
    "factors_enriched.csv", "run_metadata.json", "acceptance_policy.json",
)
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def file_hash(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _inside(root, path):
    root, path = Path(root).resolve(), Path(path).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError(f"Snapshot path escapes results directory: {path}") from error
    return path


def _archive_path(root, path):
    """Keep archive inputs inside the tree and reject redirected file names."""
    root, path = Path(root).resolve(), Path(path)
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise ValueError("Archive input is outside the results directory") from error
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Archive inputs must not be symbolic links: {current}")
    return _inside(root, path)


def artifact_paths(results_dir, include_cache=False):
    """Select known public result artifacts, never arbitrary project files."""
    root = Path(results_dir).resolve()
    if not root.is_dir():
        raise ValueError("Results directory does not exist")
    selected = {root / name for name in ARTIFACTS}
    for directory in root.glob("ontology_analysis*"):
        selected.update(directory / name for name in ANALYSIS_FILES)
    for directory in (root / "workbench/jev", root / "workbench/jev/pilot_run"):
        selected.update(directory / name for name in JEV_FILES)
    if include_cache:
        for directory in (root / ".cache", root / "workbench/.cache", root / "workbench/jev/.cache"):
            if directory.is_dir():
                selected.update(directory.rglob("*.json"))
    return sorted((_archive_path(root, path) for path in selected if path.is_file()),
                  key=lambda path: path.relative_to(root).as_posix())


def create_manifest(results_dir, include_cache=False):
    root = Path(results_dir).resolve()
    files = [{"path": path.relative_to(root).as_posix(), "size": path.stat().st_size,
              "sha256": file_hash(path)} for path in artifact_paths(root, include_cache)]
    if not files:
        raise ValueError("No known result artifacts found")
    return {"schema_version": 1, "package_version": __version__,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "includes_cache": bool(include_cache), "files": files}


def verify_manifest(results_dir, manifest):
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
            or not isinstance(manifest.get("files"), list) or not manifest["files"]):
        raise ValueError("Unsupported or empty snapshot manifest")
    root, seen, failures = Path(results_dir).resolve(), set(), []
    for item in manifest["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
            raise ValueError("Invalid manifest file record")
        relative = item["path"]
        if (not isinstance(relative, str) or not relative or Path(relative).is_absolute()
                or ".." in Path(relative).parts or relative in seen
                or type(item["size"]) is not int or item["size"] < 0
                or not isinstance(item["sha256"], str) or not SHA256.fullmatch(item["sha256"])):
            raise ValueError("Invalid, duplicate or unsafe manifest path/hash")
        seen.add(relative)
        path = _archive_path(root, root / relative)
        if not path.is_file():
            failures.append({"path": relative, "reason": "missing"})
        elif path.stat().st_size != item["size"] or file_hash(path) != item["sha256"]:
            failures.append({"path": relative, "reason": "content changed"})
    return {"verified": not failures, "files_checked": len(seen), "failures": failures}


def _cached_response(cache_dir, request_id, expected_request, jev):
    if not isinstance(request_id, str) or not SHA256.fullmatch(request_id):
        raise ValueError("Recorded decision has an invalid request fingerprint")
    expected_id = jev.digest({"prompt_version": jev.PROMPT_VERSION,
                              "sdk_version": jev.SDK_VERSION, "request": expected_request})
    if request_id != expected_id:
        raise ValueError("Recorded request does not match frozen context/catalog/model")
    path = _archive_path(cache_dir, cache_dir / (request_id + ".json"))
    envelope = json.loads(path.read_text(encoding="utf-8"))
    if (envelope.get("schema_version") != 1 or envelope.get("request_id") != request_id
            or envelope.get("request") != expected_request):
        raise ValueError("Cached response has inconsistent request provenance")
    return envelope.get("response")


def replay_jev(results_dir, output_dir):
    """Validate cached decisions, then regenerate a proposal-only recorded run.

    Contexts retain their original path strings for stable request fingerprints.
    Actual archived raw/context hashes are verified instead of consulting paths
    on the machine that originally ran the model. Recorded failures stay failed;
    replay never requests missing model responses or approves annotations.
    """
    from . import workbench_jev as jev
    from .workbench_mapping import MAPPING_FIELDS
    from .workbench_ontology_catalog import load_catalog

    root, output = Path(results_dir).resolve(), Path(output_dir).resolve()
    run = _archive_path(root, root / "workbench/jev")
    source = _archive_path(root, root / "workbench/factors.csv")
    audit_path = _archive_path(root, run / "mapping_decisions.csv")
    contexts_path = _archive_path(root, run / "contexts.jsonl")
    for path in (source, audit_path, contexts_path, run / "ontology_target_catalog.json",
                 run / "accepted_mappings.csv", run / "run_metadata.json"):
        _archive_path(root, path)
        if path.parent == output:
            raise ValueError("Replay output must be separate from all source artifacts")
    fields, factors = jev.factor_rows(source)
    raw = {jev.key_of(row): row for row in factors}
    contexts = {jev.key_of(row): row for row in jev.load_contexts(contexts_path)}
    catalog = load_catalog(run / "ontology_target_catalog.json")
    terms = jev.eligible_terms(catalog)
    audit_fields, decisions = jev.read_csv(audit_path, jev.AUDIT_FIELDS)
    accepted_fields, accepted = jev.read_csv(run / "accepted_mappings.csv", MAPPING_FIELDS)
    metadata_path = run / "run_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if accepted or metadata.get("automatic_acceptance") is not False or metadata.get("policy_sha256") is not None:
        raise ValueError("Offline replay currently supports proposal-only runs without accepted mappings")
    raw_hash, context_hash = file_hash(source), file_hash(contexts_path)
    if (metadata.get("input_sha256") != raw_hash or metadata.get("contexts_sha256") != context_hash
            or metadata.get("catalog_id") != catalog["catalog_id"] or metadata.get("model") != jev.MODEL
            or metadata.get("prompt_version") != jev.PROMPT_VERSION or metadata.get("sdk_version") != jev.SDK_VERSION):
        raise ValueError("Archived inputs or pinned model/prompt/catalog differ from run metadata")
    counts, seen = Counter(), set()
    cache_dir = _archive_path(root, run / ".cache/jev")
    for row in decisions:
        key = jev.key_of(row)
        if key in seen or key not in raw or key not in contexts:
            raise ValueError("Duplicate or unmatched recorded decision")
        seen.add(key)
        context = contexts[key]
        if (context.get("source_sha256") != raw_hash
                or context["factor_values"] != json.loads(raw[key]["factor_value_unique"])
                or row["input_hash"] != jev.digest(context)
                or row["evidence"] != jev.make_evidence(context)
                or row["catalog_id"] != catalog["catalog_id"] or row["model"] != jev.MODEL
                or row["prompt_version"] != jev.PROMPT_VERSION):
            raise ValueError(f"Recorded evidence/provenance changed for {key!r}")
        status = row["status"]
        if status not in {"needs_review", "mixed_factor", "unmapped", "ambiguous", "error"}:
            raise ValueError("Unsupported recorded status")
        if (status == "error") != bool(row["error"]):
            raise ValueError("Recorded error status and message disagree")
        if not row["request_id"]:
            if status != "error" or row["selected_accession"] or row["verification_request_id"]:
                raise ValueError("Only a recorded selection failure can lack a cached request")
            counts[status] += 1
            continue
        request = jev.build_selection_request(context, catalog)
        selection = jev.validate_selection(_cached_response(cache_dir, row["request_id"], request, jev), request)
        for field in ("selected_accession", "choice", "model"):
            if row[field] != selection[field]:
                raise ValueError("Recorded selection differs from validated cached response")
        if json.loads(row["probabilities"]) != json.loads(selection["probabilities"]):
            raise ValueError("Recorded probabilities differ from cached response")
        for field in jev.METRICS[:-1]:
            actual = jev.probability(float(row[field]), field)
            if not math.isclose(actual, selection[field], abs_tol=1e-9):
                raise ValueError("Recorded metrics differ from cached response")
        if row["selected_accession"]:
            term = terms[row["selected_accession"]]
            if row["verification_request_id"]:
                request = jev.build_verification_request(context, term)
                answer = jev.validate_verification(_cached_response(cache_dir, row["verification_request_id"], request, jev))
                if not math.isclose(jev.probability(float(row["verification_probability"]), "verification"), answer, abs_tol=1e-9):
                    raise ValueError("Recorded verification differs from cached response")
            elif status != "error" or row["verification_probability"]:
                raise ValueError("A complete proposal requires a cached verification answer")
            if status not in {"error", "needs_review"}:
                raise ValueError("Selected term has an inconsistent proposal status")
        else:
            expected = {"UNMAPPED": "unmapped", "AMBIGUOUS": "ambiguous", "MIXED_FACTOR": "mixed_factor"}.get(selection["choice"])
            if status != expected or row["verification_request_id"] or row["verification_probability"]:
                raise ValueError("Recorded abstention differs from cached response")
        counts[status] += 1
    if (metadata.get("rows_processed") != len(decisions) or metadata.get("rows_in_export") != len(factors)
            or metadata.get("rows_unprocessed") != len(factors) - len(decisions)
            or metadata.get("status_counts") != dict(counts)):
        raise ValueError("Recorded run counts differ from validated decisions")
    output_names = ("mapping_decisions.csv", "accepted_mappings.csv", "factors_enriched.csv", "run_metadata.json", "replay_metadata.json")
    if any((output / name).exists() for name in output_names):
        raise ValueError("Replay output already exists; choose a new output directory")
    jev.write_csv(output / "mapping_decisions.csv", audit_fields, decisions)
    jev.write_csv(output / "accepted_mappings.csv", accepted_fields, [])
    jev.write_csv(output / "factors_enriched.csv", fields, factors)
    jev.atomic_text(output / "run_metadata.json", metadata_path.read_text(encoding="utf-8"))
    report = {"schema_version": 1, "package_version": __version__, "offline": True,
              "rows_replayed": len(decisions), "status_counts": counts,
              "source_audit_sha256": file_hash(audit_path), "source_contexts_sha256": context_hash,
              "catalog_id": catalog["catalog_id"], "model": jev.MODEL, "new_api_calls": 0}
    jev.atomic_json(output / "replay_metadata.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="Fingerprint known result artifacts without copying data")
    create.add_argument("--results-dir", type=Path, default=Path("results"))
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--include-cache", action="store_true", help="Also hash raw/source and Jev response caches")
    verify = commands.add_parser("verify", help="Verify archived results against a saved manifest")
    verify.add_argument("--results-dir", type=Path, default=Path("results"))
    verify.add_argument("--manifest", type=Path, required=True)
    replay = commands.add_parser("replay-jev", help="Regenerate a recorded proposal-only Jev run without the API or SDK")
    replay.add_argument("--results-dir", type=Path, default=Path("results"))
    replay.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            from .workbench_jev import atomic_json
            if args.output.resolve() in set(artifact_paths(args.results_dir, args.include_cache)):
                raise ValueError("Manifest output cannot overwrite a result artifact")
            manifest = create_manifest(args.results_dir, args.include_cache)
            atomic_json(args.output, manifest)
            report = {"manifest": str(args.output.resolve()), "files": len(manifest["files"]),
                      "includes_cache": manifest["includes_cache"]}
        elif args.command == "verify":
            report = verify_manifest(args.results_dir, json.loads(args.manifest.read_text(encoding="utf-8")))
        else:
            report = replay_jev(args.results_dir, args.output_dir)
    except (OSError, ValueError, TypeError, KeyError, csv.Error) as error:
        print(f"Snapshot operation failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0 if report.get("verified", True) else 1


if __name__ == "__main__":
    sys.exit(main())
