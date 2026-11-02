"""The reproduction driver plans offline and requires explicit paid execution."""

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jev4mztabm import reproduce


class PlanTests(unittest.TestCase):
    def test_default_plan_has_frozen_catalog_and_never_enables_jev(self):
        with tempfile.TemporaryDirectory() as directory:
            steps = reproduce.build_steps(directory)
            self.assertEqual([step.name for step in steps], ["metabolights", "analysis_structural", "analysis_efo_only", "workbench", "catalog", "contexts", "pilot"])
            self.assertFalse(any(step.billable for step in steps))
            catalog = next(step for step in steps if step.name == "catalog")
            vocabulary = Path(catalog.argv[catalog.argv.index("--accessions") + 1])
            self.assertTrue(vocabulary.is_absolute())
            self.assertEqual(vocabulary.name, "target_accessions.csv")
            self.assertNotIn("ontology_analysis_efo_only", str(vocabulary))
            self.assertEqual([step.to_dict() for step in steps], [step.to_dict() for step in reproduce.build_steps(directory)])

    def test_custom_root_no_plot_and_context_network_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            root = (Path(directory) / "custom output").resolve()
            steps = reproduce.build_steps(root, no_plot=True, fetch_study_context=True,
                                           metabolights_studies=["MTBLS1"], workbench_studies=["ST000001"])
            self.assertIn("MTBLS1", steps[0].argv)
            self.assertIn("ST000001", next(step for step in steps if step.name == "workbench").argv)
            for step in steps:
                if step.stage == "analysis":
                    self.assertIn("--no-plot", step.argv)
                    self.assertIn(str(root / "factors.csv"), step.argv)
            context = next(step for step in steps if step.stage == "contexts")
            self.assertEqual(context.network, "public_api")
            self.assertIn("--fetch-study-context", context.argv)
            self.assertIn(str(root / "workbench" / "jev" / ".cache" / "study"), context.argv)
            self.assertFalse(root.exists())

    def test_dry_run_imports_no_stage_and_writes_no_files(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            target = Path(directory) / "not created"
            with patch("jev4mztabm.reproduce.importlib.import_module", side_effect=AssertionError("must stay offline")):
                self.assertEqual(reproduce.main(["--output-dir", str(target), "--dry-run"]), 0)
            self.assertEqual(json.loads(output.getvalue())["mode"], "dry_run")
            self.assertFalse(target.exists())


class ExecutionTests(unittest.TestCase):
    def test_partial_export_stops_before_any_analysis(self):
        called = []
        def runner(step):
            called.append(step.name)
            return 1
        with redirect_stderr(io.StringIO()):
            result = reproduce.execute_steps(reproduce.build_steps("/tmp/reproduce-plan"), runner=runner)
        self.assertFalse(result["completed"])
        self.assertEqual(called, ["metabolights"])

    def test_billable_guard_runs_before_any_stage(self):
        called = []
        with self.assertRaisesRegex(ValueError, "--execute --with-jev"):
            reproduce.execute_steps(reproduce.build_steps("/tmp/reproduce-plan", with_jev=True), runner=lambda step: called.append(step))
        self.assertEqual(called, [])

    def test_explicit_jev_run_is_full_context_without_a_policy(self):
        step = reproduce.build_steps("/tmp/reproduce-plan", stage="jev")[0]
        self.assertTrue(step.billable)
        self.assertNotIn("--policy", step.argv)
        self.assertNotIn("pilot_contexts.jsonl", " ".join(step.argv))
        with redirect_stderr(io.StringIO()):
            result = reproduce.execute_steps([step], allow_billable=True, runner=lambda selected: 0)
        self.assertTrue(result["completed"])

    def test_cli_cannot_enable_paid_calls_implicitly(self):
        with redirect_stderr(io.StringIO()):
            for argv in (["--with-jev"], ["--stage", "jev", "--execute"]):
                with self.subTest(argv=argv), self.assertRaises(SystemExit):
                    reproduce.main(argv)

    def test_single_stage_uses_module_main_arguments(self):
        fake = SimpleNamespace(main=lambda argv: 0)
        with patch("jev4mztabm.reproduce.importlib.import_module", return_value=fake) as importer:
            with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                self.assertEqual(reproduce.main(["--stage", "metabolights", "--execute", "--output-dir", "/tmp/example"]), 0)
            importer.assert_called_once_with("jev4mztabm.metabolights_factors")


class ExistingPilotTests(unittest.TestCase):
    def setup_pilot(self, directory):
        step = reproduce.build_steps(directory, stage="pilot", pilot_size=2)[0]
        output = Path(step.argv[step.argv.index("--output") + 1]);labels = Path(step.argv[step.argv.index("--labels") + 1])
        output.parent.mkdir(parents=True)
        contexts = [{"study_id": "ST000001", "factor_name": "Treatment"}, {"study_id": "ST000002", "factor_name": "Age"}]
        expected = [dict(row, expected_accession="", split=split) for row, split in zip(contexts, ("tune", "test"))]
        output.write_text("\n".join(json.dumps(row) for row in contexts) + "\n", encoding="utf-8")
        Path(step.argv[step.argv.index("--contexts") + 1]).write_bytes(output.read_bytes())
        with labels.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=("study_id", "factor_name", "expected_accession", "split", "review_status"))
            writer.writeheader()
            writer.writerows(dict(row, review_status="reviewed") for row in expected)
        module = SimpleNamespace(load_contexts=lambda path: [json.loads(line) for line in Path(path).read_text().splitlines()],
                                 make_pilot=lambda loaded, size, seed: (contexts, expected),
                                 main=lambda argv: self.fail("existing reviewed pilot must not be rewritten"))
        return step, module, output, labels

    def test_matching_existing_reviewed_labels_are_preserved_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as directory:
            step, module, output, labels = self.setup_pilot(directory)
            before = output.read_bytes(), labels.read_bytes()
            with patch("jev4mztabm.reproduce.importlib.import_module", return_value=module), redirect_stderr(io.StringIO()):
                self.assertEqual(reproduce.run_step(step), 0)
            self.assertEqual((output.read_bytes(), labels.read_bytes()), before)

    def test_partial_or_inconsistent_pilot_fails_without_changing_reviews(self):
        with tempfile.TemporaryDirectory() as directory:
            step, module, output, labels = self.setup_pilot(directory)
            before = labels.read_bytes()
            output.unlink()
            with self.assertRaisesRegex(ValueError, "Only one pilot"):
                reproduce._reuse_pilot(step, module)
            self.assertEqual(labels.read_bytes(), before)

    def test_changed_pilot_context_is_detected_and_review_bytes_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            step, module, output, labels = self.setup_pilot(directory)
            before = labels.read_bytes()
            output.write_text(output.read_text().replace('"Treatment"', '"Different"'), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "differs from the current deterministic selection"):
                reproduce._reuse_pilot(step, module)
            self.assertEqual(labels.read_bytes(), before)
        with tempfile.TemporaryDirectory() as directory:
            step, module, output, labels = self.setup_pilot(directory)
            labels.write_text(labels.read_text().replace(",tune,", ",test,"), encoding="utf-8")
            before = labels.read_bytes()
            with self.assertRaisesRegex(ValueError, "pilot/split"):
                reproduce._reuse_pilot(step, module)
            self.assertEqual(labels.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
