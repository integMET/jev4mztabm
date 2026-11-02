"""Offline integration checks for the Jev command and native SDK boundary."""

import copy
import csv
import io
import json
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import workbench_jev as jev
from calibrate_workbench_jev import AUDIT_FIELDS, METRICS, calibrate
from workbench_ontology_catalog import OLS_URL, build_catalog, save_catalog


TREATMENT = "http://www.ebi.ac.uk/efo/EFO_0000727"
AGE = "http://www.ebi.ac.uk/efo/EFO_0000246"
ONTOLOGY_FILE = "http://www.ebi.ac.uk/efo/releases/3.94.0/efo.owl"
FIELDS = (
    "study_id", "factor_name", "factor_type", "ontology_source", "ontology_accession",
    "ontology_version", "ontology_file", "factor_value_unique", "note",
)


def write_csv(path, fields, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def fake_sdk(calls):
    module = types.ModuleType("typesafe_sdk")

    class Primitive:
        def __init__(self, **values):
            self.__dict__.update(values)

    class Choice(Primitive):
        pass

    class Noul(Primitive):
        pass

    class RetryPolicy(Primitive):
        pass

    class Response:
        def __init__(self, answers, request_id):
            self.answers = answers
            self.request_id = request_id

        def model_dump(self, *, mode):
            if mode != "json":
                raise AssertionError("Expected native SDK JSON-mode serialization")
            return {"model": "jev-1.14.0", "answers": copy.deepcopy(self.answers),
                    "usage": {"input_tokens": 123, "output_tokens": 12}}

    class AsyncTypeSafeClient:
        def __init__(self, **options):
            self.options = options

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def system_one(self, **request):
            calls.append(request)
            questions = request["questions"]
            if "accession" in questions:
                if not isinstance(questions["accession"], Choice):
                    raise AssertionError("Selection must use the SDK Choice primitive")
                if any(not isinstance(questions[name], Noul) for name in ("eligible_match", "single_dimension")):
                    raise AssertionError("Gates must use SDK Noul primitives")
                selected = AGE if request["state"]["factor"]["factor_name"] == "Age" else TREATMENT
                choices = questions["accession"].criteria
                answers = {
                    "accession": {"type": "choice", "choice": selected, "confidence": 0.99,
                                  "probabilities": {key: 0.99 if key == selected else 0.01 / (len(choices) - 1)
                                                    for key in choices}},
                    "eligible_match": {"type": "noul", "noul": 0.99},
                    "single_dimension": {"type": "noul", "noul": 0.99},
                }
            else:
                if set(questions) != {"selected_term_supported"} or not isinstance(questions["selected_term_supported"], Noul):
                    raise AssertionError("Verification must use a separate SDK Noul question")
                answers = {"selected_term_supported": {"type": "noul", "noul": 0.99}}
            return Response(answers, f"fake-provider-request-{len(calls)}")

    module.AsyncTypeSafeClient = AsyncTypeSafeClient
    module.Choice, module.Noul, module.RetryPolicy = Choice, Noul, RetryPolicy
    return module


class JevCliTests(unittest.TestCase):
    def read_csv(self, path):
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def fixtures(self, root):
        accessions = root / "accessions.csv"
        write_csv(accessions, ("ontology_accession",), [{"ontology_accession": iri} for iri in (AGE, TREATMENT)])

        def ols_fixture(url):
            if url == OLS_URL:
                return {"ontologyId": "efo", "config": {"id": "efo", "version": "3.94.0", "versionIri": ONTOLOGY_FILE}}
            iri = parse_qs(urlsplit(url).query)["iri"][0]
            label = "age" if iri == AGE else "treatment"
            return {"_embedded": {"terms": [{"iri": iri, "label": label, "description": [f"Experimental {label}"],
                                                "synonyms": [], "ontology_name": "efo", "is_obsolete": False}]}}

        catalog = build_catalog(accessions, root / "ols_cache", fetch_json=ols_fixture)
        catalog_path = root / "catalog.json"
        save_catalog(catalog_path, catalog)
        rows = []
        for study_id, name, values in (("ST000002", "Age", ["12", "24"]),
                                      ("ST000001", "Treatment", ["Control", 'Drug "β"'])):
            row = dict.fromkeys(FIELDS, "")
            row.update(study_id=study_id, factor_name=name, factor_value_unique=json.dumps(values, ensure_ascii=False),
                       note="Keep raw metadata and row order")
            rows.append(row)
        source = root / "factors.csv"
        write_csv(source, FIELDS, rows)
        contexts_path = root / "contexts.jsonl"
        contexts_path.write_text("".join(jev.canonical_json(item) + "\n" for item in jev.prepare_contexts(source)), encoding="utf-8")
        return catalog, catalog_path, source, contexts_path, rows

    def test_run_wires_calibrated_acceptance_cache_and_unreviewed_proposals_without_changing_sources(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            root = Path(directory)
            catalog, catalog_path, source, contexts_path, original_rows = self.fixtures(root)
            audit, labels = [], []
            for index in range(1, 41):
                study_id = f"ST{100000 + index:06d}"
                selected = AGE if index % 2 else TREATMENT
                row = dict.fromkeys(AUDIT_FIELDS, "")
                row.update(study_id=study_id, factor_name="Age" if selected == AGE else "Treatment",
                           status="needs_review", selected_accession=selected, model=jev.MODEL,
                           prompt_version=jev.PROMPT_VERSION, catalog_id=catalog["catalog_id"],
                           request_id=f"calibration-request-{index}", input_hash=f"calibration-input-{index}",
                           evidence="Synthetic offline integration fixture")
                row.update({metric: "0.97" for metric in METRICS})
                audit.append(row)
                labels.append({"study_id": study_id, "factor_name": row["factor_name"], "expected_accession": selected,
                               "split": "tune" if index <= 20 else "test", "review_status": "reviewed"})
            policy = calibrate(audit, labels, min_accepted=20, catalog_id=catalog["catalog_id"])
            policy_path = root / "policy.json"
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
            output = root / "mapped"
            command = ["run", "--input", str(source), "--contexts", str(contexts_path),
                       "--catalog", str(catalog_path), "--output-dir", str(output), "--policy", str(policy_path)]
            original_source, original_contexts = source.read_bytes(), contexts_path.read_bytes()
            calls = []
            with patch.dict("sys.modules", {"typesafe_sdk": fake_sdk(calls)}), \
                    patch("workbench_jev.importlib.metadata.version", return_value="0.7.5"), \
                    patch("workbench_jev.sys.version_info", (3, 12, 0)), \
                    patch.dict("os.environ", {"TYPESAFE_API_KEY": "offline-test-key"}):
                self.assertEqual(jev.main(command), 0)
                self.assertEqual(len(calls), 4)
                accepted = self.read_csv(output / "accepted_mappings.csv")
                self.assertEqual({(row["study_id"], row["factor_name"]): row["ontology_accession"] for row in accepted}, {
                    ("ST000002", "Age"): AGE, ("ST000001", "Treatment"): TREATMENT,
                })
                self.assertTrue(all(row["ontology_source"] == "EFO" and row["ontology_version"] == "3.94.0"
                                    and row["ontology_file"] == ONTOLOGY_FILE and row["review_status"] == "approved"
                                    for row in accepted))
                enriched = self.read_csv(output / "factors_enriched.csv")
                self.assertEqual([(row["study_id"], row["factor_name"]) for row in enriched],
                                 [(row["study_id"], row["factor_name"]) for row in original_rows])
                self.assertEqual([row["factor_type"] for row in enriched], ["age", "treatment"])
                self.assertEqual([row["factor_value_unique"] for row in enriched], [row["factor_value_unique"] for row in original_rows])
                self.assertEqual([row["note"] for row in enriched], [row["note"] for row in original_rows])
                self.assertEqual(jev.main(command), 0)
                self.assertEqual(len(calls), 4, "Second run must reuse both selection and verification caches")
                proposal_output = root / "proposals"
                without_policy = command[:-2]
                without_policy[without_policy.index("--output-dir") + 1] = str(proposal_output)
                self.assertEqual(jev.main(without_policy), 0)
                self.assertEqual(self.read_csv(proposal_output / "accepted_mappings.csv"), [])
                self.assertEqual(self.read_csv(proposal_output / "factors_enriched.csv"), original_rows)
                self.assertEqual({row["status"] for row in self.read_csv(proposal_output / "mapping_decisions.csv")}, {"needs_review"})
            self.assertEqual(source.read_bytes(), original_source)
            self.assertEqual(contexts_path.read_bytes(), original_contexts)

    def test_default_dry_run_needs_neither_sdk_nor_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, catalog_path, source, contexts_path, _ = self.fixtures(root)
            captured = io.StringIO()
            with redirect_stdout(captured), patch("workbench_jev.importlib.metadata.version", side_effect=AssertionError("No SDK needed")), \
                    patch.dict("os.environ", {"TYPESAFE_API_KEY": ""}):
                result = jev.main(["run", "--input", str(source), "--contexts", str(contexts_path),
                                   "--catalog", str(catalog_path), "--output-dir", str(root / "dry"), "--dry-run"])
            self.assertEqual(result, 0)
            summary = json.loads(captured.getvalue())
            self.assertEqual(summary["jev_calls"], 0)
            self.assertFalse(summary["automatic_acceptance"])


if __name__ == "__main__":
    unittest.main()
