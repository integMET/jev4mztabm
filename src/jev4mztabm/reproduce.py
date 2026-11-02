"""Plan or execute the public-data workflow, with billable Jev explicitly opt-in."""

import argparse
import csv
import importlib
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


STAGES = ("all", "metabolights", "analysis", "workbench", "catalog", "contexts", "pilot", "jev")
EFO_SUBSTRING = "http://www.ebi.ac.uk/efo/"


@dataclass(frozen=True)
class Step:
    name: str
    stage: str
    module: str
    argv: tuple
    network: str = "none"
    billable: bool = False

    def to_dict(self):
        result = asdict(self)
        result["argv"] = list(self.argv)
        return result


def build_steps(output_dir="results", *, stage="all", with_jev=False,
                fetch_study_context=False, no_plot=False, pilot_size=250, seed=42,
                expected_catalog_count=156, target_accessions=None,
                metabolights_studies=None, workbench_studies=None):
    """Build an ordered, deterministic plan without reading data or importing clients."""
    if stage not in STAGES:
        raise ValueError(f"Unknown workflow stage: {stage}")
    if pilot_size < 2 or expected_catalog_count < 1:
        raise ValueError("pilot_size must be at least 2 and expected_catalog_count positive")
    root = Path(output_dir).expanduser().resolve()
    wb = root / "workbench"
    jev = wb / "jev"
    targets = Path(target_accessions).expanduser().resolve() if target_accessions is not None else (
        Path(__file__).resolve().parent / "data" / "target_accessions.csv"
    )
    mt_args = ["--output-dir", str(root)]
    wb_args = ["--output-dir", str(wb)]
    for study_ids, argv in ((metabolights_studies, mt_args), (workbench_studies, wb_args)):
        if study_ids is not None:
            if not study_ids:
                raise ValueError("An explicit study selection must not be empty")
            argv.extend(["--studies", *study_ids])
    analysis = ["--input", str(root / "factors.csv"), "--output-dir", str(root / "ontology_analysis"), "--top", "255"]
    efo = ["--input", str(root / "factors.csv"), "--output-dir", str(root / "ontology_analysis_efo_only"),
           "--accession-contains", EFO_SUBSTRING, "--top", "255"]
    if no_plot:
        analysis.append("--no-plot")
        efo.append("--no-plot")
    contexts = ["prepare", "--input", str(wb / "factors.csv"), "--sample-factors", str(wb / "sample_factors.csv"),
                "--workbench-cache", str(wb / ".cache"), "--context-cache", str(jev / ".cache" / "study"),
                "--output", str(jev / "contexts.jsonl")]
    if fetch_study_context:
        contexts.append("--fetch-study-context")
    steps = [
        Step("metabolights", "metabolights", "jev4mztabm.metabolights_factors", tuple(mt_args), "public_api"),
        Step("analysis_structural", "analysis", "jev4mztabm.analyze_ontology_accessions", tuple(analysis)),
        Step("analysis_efo_only", "analysis", "jev4mztabm.analyze_ontology_accessions", tuple(efo)),
        Step("workbench", "workbench", "jev4mztabm.workbench_factors", tuple(wb_args), "public_api"),
        Step("catalog", "catalog", "jev4mztabm.workbench_jev", (
            "catalog", "--accessions", str(targets), "--output", str(jev / "ontology_target_catalog.json"),
            "--cache-dir", str(jev / ".cache" / "ols"), "--expected-count", str(expected_catalog_count),
        ), "public_api"),
        Step("contexts", "contexts", "jev4mztabm.workbench_jev", tuple(contexts),
             "public_api" if fetch_study_context else "none"),
        Step("pilot", "pilot", "jev4mztabm.workbench_jev", (
            "pilot", "--contexts", str(jev / "contexts.jsonl"), "--size", str(pilot_size), "--seed", str(seed),
            "--output", str(jev / "pilot_contexts.jsonl"), "--labels", str(jev / "reviewed_labels.csv"),
        )),
        Step("jev", "jev", "jev4mztabm.workbench_jev", (
            "run", "--input", str(wb / "factors.csv"), "--contexts", str(jev / "contexts.jsonl"),
            "--catalog", str(jev / "ontology_target_catalog.json"), "--output-dir", str(jev),
        ), "typesafe_api", True),
    ]
    if stage == "all":
        return [step for step in steps if not step.billable or with_jev]
    return [step for step in steps if step.stage == stage]


def _value(argv, flag):
    return argv[argv.index(flag) + 1]


def _reuse_pilot(step, module):
    """Reuse a matching pilot while leaving all review edits byte-for-byte intact."""
    output, labels_path = Path(_value(step.argv, "--output")), Path(_value(step.argv, "--labels"))
    if not output.exists() and not labels_path.exists():
        return False
    if not output.exists() or not labels_path.exists():
        raise ValueError("Only one pilot/review file exists; preserve it and choose another output directory")
    current = module.load_contexts(Path(_value(step.argv, "--contexts")))
    expected_contexts, expected_labels = module.make_pilot(
        current, int(_value(step.argv, "--size")), int(_value(step.argv, "--seed")),
    )
    if module.load_contexts(output) != expected_contexts:
        raise ValueError("Existing pilot differs from the current deterministic selection; preserve reviews and choose another output directory")
    expected_splits = {(row["study_id"], row["factor_name"]): row["split"] for row in expected_labels}
    with labels_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if (not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames)
                or {"study_id", "factor_name", "expected_accession", "split"} - set(reader.fieldnames)):
            raise ValueError("Existing review labels have an incomplete header; the file was preserved")
        seen = set()
        for row in reader:
            key = (row.get("study_id"), row.get("factor_name"))
            if None in row or any(value is None for value in row.values()) or key in seen:
                raise ValueError("Existing review labels are malformed or duplicated; the file was preserved")
            seen.add(key)
            if key not in expected_splits or row["split"] != expected_splits[key]:
                raise ValueError("Existing review labels do not match the current pilot/split; the file was preserved")
        if seen != set(expected_splits):
            raise ValueError("Existing review labels omit pilot rows; the file was preserved")
    return True


def run_step(step):
    """Import a stage lazily; planning never imports optional plotting or SDK code."""
    module = importlib.import_module(step.module)
    if step.name == "pilot" and _reuse_pilot(step, module):
        print("Reusing existing pilot and preserving reviewed labels", file=sys.stderr)
        return 0
    return module.main(list(step.argv))


def execute_steps(steps, *, allow_billable=False, runner=None):
    """Stop at the first failed or partial stage; never automatically calibrate."""
    if any(step.billable for step in steps) and not allow_billable:
        raise ValueError("Billable Jev execution requires --execute --with-jev")
    results = []
    for step in steps:
        print(f"Running {step.name}", file=sys.stderr)
        code = (runner or run_step)(step)
        if code is None:
            code = 0
        if type(code) is not int:
            raise ValueError(f"{step.name}: stage returned an invalid exit code")
        results.append({"name": step.name, "exit_code": code})
        if code:
            return {"completed": False, "exit_code": code, "steps": results}
    return {"completed": True, "exit_code": 0, "steps": results}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("results"))
    parser.add_argument("--stage", choices=STAGES, default="all")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="Execute planned stages; otherwise print a dry-run plan")
    mode.add_argument("--dry-run", action="store_true", help="Print the deterministic plan without executing anything (default)")
    parser.add_argument("--with-jev", action="store_true", help="Explicitly allow billable full-context Jev proposals; requires --execute")
    parser.add_argument("--fetch-study-context", action="store_true", help="Opt in to fetching public study context during preparation")
    parser.add_argument("--no-plot", action="store_true", help="Produce analysis CSVs without Matplotlib charts")
    parser.add_argument("--pilot-size", type=int, default=250)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--expected-catalog-count", type=int, default=156)
    parser.add_argument("--target-accessions", type=Path, help="Explicit alternative vocabulary CSV (default: bundled frozen 156-IRI list)")
    parser.add_argument("--metabolights-studies", nargs="+", metavar="MTBLS_ID")
    parser.add_argument("--workbench-studies", nargs="+", metavar="ST_ID")
    args = parser.parse_args(argv)
    if args.with_jev and not args.execute:
        parser.error("--with-jev requires --execute; use --stage jev to inspect a billable stage without executing")
    if args.execute and args.stage == "jev" and not args.with_jev:
        parser.error("Executing the Jev stage requires --with-jev")
    try:
        steps = build_steps(
            args.output_dir, stage=args.stage, with_jev=args.with_jev,
            fetch_study_context=args.fetch_study_context, no_plot=args.no_plot,
            pilot_size=args.pilot_size, seed=args.seed, expected_catalog_count=args.expected_catalog_count,
            target_accessions=args.target_accessions, metabolights_studies=args.metabolights_studies,
            workbench_studies=args.workbench_studies,
        )
        if not args.execute:
            print(json.dumps({"schema_version": 1, "mode": "dry_run", "output_dir": str(args.output_dir.expanduser().resolve()),
                              "steps": [step.to_dict() for step in steps]}, indent=2, sort_keys=True))
            return 0
        result = execute_steps(steps, allow_billable=args.with_jev)
        print(json.dumps(result, indent=2, sort_keys=True))
        return result["exit_code"]
    except (OSError, ValueError, ImportError, csv.Error) as error:
        print(f"Reproduction stopped: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Reproduction interrupted; successful source/model caches are retained", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
