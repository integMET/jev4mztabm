"""Held-out policy validation must not silently approve unreviewed decisions."""

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from calibrate_workbench_jev import AUDIT_FIELDS, LABEL_FIELDS, METRICS, calibrate, load_audit, load_labels, main


TERM = "http://www.ebi.ac.uk/efo/EFO_0000727"
OTHER = "http://www.ebi.ac.uk/efo/EFO_0000246"


def decision(number, score=0.97, margin=0.4, selected=TERM):
    row = dict.fromkeys(AUDIT_FIELDS, "")
    row.update(study_id=f"ST{number:06d}", factor_name="Treatment", status="needs_review",
               selected_accession=selected, model="jev-1.14.0",
               prompt_version="workbench-factors-v1", catalog_id="catalog-test",
               request_id=f"request-{number}", input_hash=f"input-{number}", evidence="Test fixture only")
    row.update({metric: str(margin if metric == "top_two_margin" else score) for metric in METRICS})
    return row


def label(number, split, expected=TERM, **extra):
    return dict(study_id=f"ST{number:06d}", factor_name="Treatment", expected_accession=expected, split=split, **extra)


class PolicyTests(unittest.TestCase):
    def test_validated_policy_uses_both_splits_and_keeps_observed_precision_distinct(self):
        audit = [decision(1), decision(2), decision(3, score=0.55, margin=0.01),
                 decision(4), decision(5), decision(6, score=0.55, margin=0.01)]
        labels = [label(1, "tune"), label(2, "tune"), label(3, "tune", ""),
                  label(4, "test"), label(5, "test"), label(6, "test", "")]
        config = calibrate(audit, labels, target_precision=1, min_accepted=2)
        self.assertTrue(config["calibrated"])
        self.assertEqual(set(config["thresholds"]), set(METRICS))
        self.assertEqual(config["diagnostics"]["tune"]["accepted"], 2)
        self.assertEqual(config["diagnostics"]["test"]["accepted"], 2)
        self.assertEqual(config["diagnostics"]["test"]["precision"], 1)
        self.assertIn("not calibrated probabilities", config["diagnostics"]["score_interpretation"])
        self.assertLess(config["diagnostics"]["test"]["precision_wilson_95_interval"][0], 1)

    def test_failed_holdout_does_not_select_a_different_policy_using_test(self):
        audit = [decision(1), decision(2), decision(3, score=0.7), decision(4, score=0.7),
                 decision(5), decision(6), decision(7, score=0.7), decision(8, score=0.7)]
        labels = [label(number, "tune") for number in (1, 2, 3, 4)]
        labels += [label(5, "test"), label(6, "test"), label(7, "test", OTHER), label(8, "test", OTHER)]
        with self.assertRaisesRegex(ValueError, "failed held-out test"):
            calibrate(audit, labels, target_precision=1, min_accepted=2)

    def test_studies_must_be_disjoint_even_when_factor_names_differ(self):
        labels = [label(1, "tune"), dict(label(1, "test"), factor_name="Age")]
        with self.assertRaisesRegex(ValueError, "study-disjoint"):
            calibrate([decision(1)], labels, min_accepted=1)

    def test_pending_templates_are_not_ground_truth_including_blank_labels(self):
        for status in ("pending", "", "Reviewed"):
            labels = [label(1, "tune", "", review_status=status), label(2, "test", review_status="reviewed")]
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "review_status"):
                calibrate([decision(1), decision(2)], labels, min_accepted=1)

    def test_errors_abstentions_and_invalid_scores_never_become_accepted(self):
        for metric in METRICS:
            for bad in ("", "NaN", "Infinity", "-0.1", "1.1"):
                audit = [decision(1), decision(2)]
                audit[0][metric] = bad
                with self.subTest(metric=metric, value=bad), self.assertRaisesRegex(ValueError, "No tune policy"):
                    calibrate(audit, [label(1, "tune"), label(2, "test")], min_accepted=1)
        for changes in ({"error": "verification failed"}, {"selected_accession": ""}, {"selected_accession": "EFO"}):
            audit = [dict(decision(1), **changes), decision(2)]
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "No tune policy"):
                calibrate(audit, [label(1, "tune"), label(2, "test")], min_accepted=1)

    def test_duplicate_missing_and_invalid_keys_or_labels_fail(self):
        audit, labels = [decision(1), decision(2)], [label(1, "tune"), label(2, "test")]
        cases = [
            (audit + [decision(1)], labels, "Duplicate audit"),
            (audit, labels + [label(1, "tune")], "Duplicate reviewed"),
            (audit, [label(1, "tune"), label(3, "test")], "no exact matching"),
            (audit, [dict(label(1, "tune"), study_id="ST1"), label(2, "test")], "invalid study_id"),
            (audit, [dict(label(1, "tune"), factor_name=" "), label(2, "test")], "factor_name"),
            (audit, [label(1, "tune", "EFO"), label(2, "test")], "invalid expected_accession"),
            (audit, [dict(label(1, "tune"), split=""), label(2, "test")], "explicit split"),
        ]
        for a, gold, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                calibrate(a, gold, min_accepted=1)

    def test_model_prompt_and_catalog_are_bound_to_one_profile(self):
        labels = [label(1, "tune"), label(2, "test")]
        for field in ("model", "prompt_version", "catalog_id"):
            audit = [decision(1), decision(2)]; audit[1][field] = "other"
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                calibrate(audit, labels, min_accepted=1)
        with self.assertRaisesRegex(ValueError, "requested catalog_id"):
            calibrate([decision(1), decision(2)], labels, min_accepted=1, catalog_id="wrong")

    def test_test_count_requirement_is_independent_from_tune_count(self):
        with self.assertRaisesRegex(ValueError, "failed held-out test"):
            calibrate([decision(1), decision(2), decision(3)],
                      [label(1, "tune"), label(2, "tune"), label(3, "test")], min_accepted=2)


class CsvCliTests(unittest.TestCase):
    def write(self, path, rows, fields):
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader(); writer.writerows(rows)

    def test_explicit_split_column_is_required(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "labels.csv"
            self.write(path, [{"study_id": "ST000001", "factor_name": "Treatment", "expected_accession": TERM}], LABEL_FIELDS[:-1])
            with self.assertRaisesRegex(ValueError, "missing columns: split"):
                load_labels(path)

    def test_cli_writes_only_validated_policy_with_source_hashes(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            base = Path(directory); audit = base / "audit.csv"; labels = base / "labels.csv"; output = base / "policy.json"
            self.write(audit, [decision(number) for number in range(1, 5)], AUDIT_FIELDS)
            self.write(labels, [label(1, "tune"), label(2, "tune"), label(3, "test"), label(4, "test")], LABEL_FIELDS)
            command = ["--audit", str(audit), "--labels", str(labels), "--output", str(output), "--min-accepted", "2"]
            self.assertEqual(main(command), 0)
            policy = json.loads(output.read_text())
            self.assertEqual(policy["label_source_sha256"], hashlib.sha256(labels.read_bytes()).hexdigest())
            self.assertEqual(policy["audit_source_sha256"], hashlib.sha256(audit.read_bytes()).hexdigest())
            failed_output = base / "failed.json"
            self.assertEqual(main(command[:5] + [str(failed_output)] + ["--min-accepted", "20"]), 1)
            self.assertFalse(failed_output.exists())


if __name__ == "__main__":
    unittest.main()
