"""Offline checks for the Metabolomics Workbench factor exporter."""

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import workbench_factors as workbench


def sample_record(sample_id="101", local_id="Sample 1", factors="Treatment: control", study_id="ST000001"):
    return {
        "study_id": study_id,
        "mb_sample_id": sample_id,
        "local_sample_id": local_id,
        "sample_source": "blood plasma",
        "factors": factors,
    }


def native_investigation(study_id, samples, analysis_id="AN000001"):
    return {
        "METABOLOMICS WORKBENCH": {"STUDY_ID": study_id, "ANALYSIS_ID": analysis_id},
        "SUBJECT_SAMPLE_FACTORS": [
            {"Sample ID": sample_id, "Factors": factor_values} for sample_id, factor_values in samples
        ],
    }


class RecordNormalizationTests(unittest.TestCase):
    def test_list_and_numeric_dictionary_shapes_are_supported(self):
        records = [sample_record(), sample_record("102", "Sample 2")]
        self.assertEqual(workbench.normalize_records(records), records)
        self.assertEqual(workbench.normalize_records({"1": records[0], "2": records[1]}), records)

    def test_single_flat_record_and_empty_responses_are_supported(self):
        record = sample_record()
        self.assertEqual(workbench.normalize_records(record), [record])
        self.assertEqual(workbench.normalize_records([]), [])
        self.assertEqual(workbench.normalize_records({}), [])

    def test_malformed_and_error_responses_are_not_empty_successes(self):
        for payload in (
            None, "No data", {"error": "Study not found"}, ["not a record"], {"1": "not a record"},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                workbench.normalize_records(payload)

    def test_listing_returns_unique_sorted_study_ids(self):
        payload = {
            "1": {"study_id": "ST000003", "study_title": "Third study"},
            "2": {"study_id": "ST000001", "study_title": "First study"},
            "3": {"study_id": "ST000001", "study_title": "First study"},
        }
        self.assertEqual(workbench.parse_study_ids(payload), ["ST000001", "ST000003"])

    def test_invalid_study_id_is_rejected(self):
        for study_id in ("ST1", "ST000001/path", "MTBLS1", "ST00000X"):
            with self.subTest(study_id=study_id), self.assertRaises(ValueError):
                workbench.parse_study_ids([{"study_id": study_id}])


class FactorPairTests(unittest.TestCase):
    def test_delimiters_trim_edges_and_preserve_value_content(self):
        text = " Treatment : control | Sex: female | Protocol: https://example.org/a:b?x=1 | Note: A; B "
        self.assertEqual(workbench.parse_factor_pairs(text), [
            ("Treatment", "control"),
            ("Sex", "female"),
            ("Protocol", "https://example.org/a:b?x=1"),
            ("Note", "A; B"),
        ])

    def test_blank_factors_are_allowed(self):
        self.assertEqual(workbench.parse_factor_pairs(""), [])
        self.assertEqual(workbench.parse_factor_pairs(" \t "), [])

    def test_missing_value_keeps_the_factor_name(self):
        self.assertEqual(workbench.parse_factor_pairs("Treatment: | Sex: female"), [
            ("Treatment", ""), ("Sex", "female"),
        ])

    def test_literal_ethnicity_pipe_preserves_five_factors_and_raw_evidence(self):
        for ethnicity in ("Ju|Hoan", "Kaukau/Ju|Hoan"):
            with self.subTest(ethnicity=ethnicity):
                raw = f"Categorization:Ju | Country:Botswana | Ethnicity:{ethnicity} | Occupation:Hunter-Gatherer | Sex:F"
                self.assertEqual(workbench.parse_factor_pairs(raw), [
                    ("Categorization", "Ju"), ("Country", "Botswana"), ("Ethnicity", ethnicity),
                    ("Occupation", "Hunter-Gatherer"), ("Sex", "F"),
                ])
                parsed = workbench.parse_study("ST000818", [sample_record(study_id="ST000818", factors=raw)])
                self.assertEqual(len(parsed["factors"]), 5)
                evidence = {row["factor_name"]: row for row in parsed["samples"]}
                self.assertEqual(evidence["Ethnicity"]["factor_value"], ethnicity)
                self.assertTrue(all(row["raw_factors"] == raw for row in evidence.values()))

    def test_compact_factor_separators_remain_supported(self):
        self.assertEqual(workbench.parse_factor_pairs("A:1|B:2"), [("A", "1"), ("B", "2")])

    def test_spaced_empty_slot_is_retained_for_native_recovery(self):
        self.assertEqual(workbench.parse_factor_pairs("Time point:Baseline | | Therapy:Control"), [
            ("Time point", "Baseline"), ("", ""), ("Therapy", "Control"),
        ])

    def test_unnamed_colon_delimited_slots_preserve_values(self):
        self.assertEqual(workbench.parse_factor_pairs(": control"), [("", "control")])
        self.assertEqual(workbench.parse_factor_pairs("Treatment:Control | :"), [("Treatment", "Control"), ("", "")])

    def test_malformed_nonempty_fragments_fail(self):
        for text in ("Treatment control", "Sex: female | malformed", "A:1 | broken", "A:1||B:2"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                workbench.parse_factor_pairs(text)


class StudyParsingTests(unittest.TestCase):
    def test_study_factors_are_unique_and_ontology_fields_remain_blank(self):
        parsed = workbench.parse_study("ST000001", [
            sample_record(factors=" Treatment : control | Sex: female"),
            sample_record("102", "Sample 2", "Treatment: treated | Sex: male"),
        ])
        self.assertEqual(parsed["sample_count"], 2)
        rows = parsed["factors"]
        self.assertEqual({row["factor_name"] for row in rows}, {"Treatment", "Sex"})
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(set(row), {
                "study_id", "factor_name", "factor_type", "ontology_source",
                "ontology_accession", "ontology_version", "ontology_file", "factor_value_unique",
            })
            self.assertEqual(row["study_id"], "ST000001")
            for field in ("factor_type", "ontology_source", "ontology_accession", "ontology_version", "ontology_file"):
                with self.subTest(field=field):
                    self.assertEqual(row[field], "")

    def test_unique_values_are_sorted_exact_json_strings_including_blanks_and_literal_characters(self):
        values = ["β", 'He said "hi"', "A|B", "A;B", "A__B", "A  B", "A B", "", "A|B"]
        records = [
            sample_record(str(101 + index), f"Sample {index}", "Treatment:" + value)
            for index, value in enumerate(values)
        ]
        parsed = workbench.parse_study("ST000001", records)
        self.assertEqual(len(parsed["factors"]), 1)
        encoded = parsed["factors"][0]["factor_value_unique"]
        self.assertIsInstance(encoded, str)
        self.assertEqual(json.loads(encoded), sorted(set(values)))

    def test_sample_evidence_preserves_raw_factors_and_values(self):
        raw = "Protocol: https://example.org/a:b | Note: A; B"
        parsed = workbench.parse_study("ST000001", [sample_record(factors=raw)])
        self.assertEqual(len(parsed["samples"]), 2)
        indexed = {row["factor_name"]: row for row in parsed["samples"]}
        self.assertEqual(indexed["Protocol"]["factor_value"], "https://example.org/a:b")
        self.assertEqual(indexed["Note"]["factor_value"], "A; B")
        self.assertEqual(indexed["Protocol"]["factor_position"], 1)
        self.assertEqual(indexed["Note"]["factor_position"], 2)
        for row in parsed["samples"]:
            self.assertEqual(row["study_id"], "ST000001")
            self.assertEqual(row["mb_sample_id"], "101")
            self.assertEqual(row["local_sample_id"], "Sample 1")
            self.assertEqual(row["sample_source"], "blood plasma")
            self.assertEqual(row["raw_factors"], raw)

    def test_repeated_sample_factor_value_is_deduplicated(self):
        record = sample_record()
        parsed = workbench.parse_study("ST000001", [record, dict(record)])
        self.assertEqual(parsed["sample_count"], 1)
        self.assertEqual(len(parsed["factors"]), 1)
        self.assertEqual(len(parsed["samples"]), 1)

    def test_conflicting_values_for_same_sample_and_factor_fail(self):
        with self.assertRaises(ValueError):
            workbench.parse_study("ST000001", [sample_record(), sample_record(factors="Treatment: treated")])

    def test_repeated_factor_names_within_sample_preserve_positions_and_values(self):
        raw = "Replicate:1 | Treatment:Control | Treatment:0_day"
        parsed = workbench.parse_study("ST001861", [sample_record(study_id="ST001861", factors=raw)])
        self.assertEqual(parsed["sample_count"], 1)
        self.assertEqual({row["factor_name"] for row in parsed["factors"]}, {"Replicate", "Treatment"})
        self.assertEqual(len(parsed["factors"]), 2)
        self.assertEqual([
            (row["factor_position"], row["factor_name"], row["factor_value"]) for row in parsed["samples"]
        ], [(1, "Replicate", "1"), (2, "Treatment", "Control"), (3, "Treatment", "0_day")])
        self.assertTrue(parsed["warnings"])
        self.assertTrue(all(row["raw_factors"] == raw for row in parsed["samples"]))
        factor_values = {row["factor_name"]: json.loads(row["factor_value_unique"]) for row in parsed["factors"]}
        self.assertEqual(factor_values, {"Replicate": ["1"], "Treatment": ["0_day", "Control"]})

    def test_identical_repeated_factor_values_keep_distinct_positions_and_deduplicate_records(self):
        record = sample_record(factors="Treatment:Control | Treatment:Control")
        parsed = workbench.parse_study("ST000001", [record, dict(record)])
        self.assertEqual(parsed["sample_count"], 1)
        self.assertEqual(len(parsed["factors"]), 1)
        self.assertEqual([
            (row["factor_position"], row["factor_value"]) for row in parsed["samples"]
        ], [(1, "Control"), (2, "Control")])
        self.assertTrue(parsed["warnings"])
        self.assertEqual(json.loads(parsed["factors"][0]["factor_value_unique"]), ["Control"])

    def test_conflicting_names_at_same_sample_position_across_records_fail(self):
        with self.assertRaises(ValueError):
            workbench.parse_study("ST000001", [sample_record(), sample_record(factors="Sex: female")])

    def test_foreign_study_records_are_rejected(self):
        with self.assertRaises(ValueError):
            workbench.parse_study("ST000001", [sample_record(study_id="ST000002")])

    def test_conflicting_source_for_same_sample_is_rejected(self):
        first = sample_record()
        second = dict(first, sample_source="urine")
        with self.assertRaises(ValueError):
            workbench.parse_study("ST000001", [first, second])

    def test_blank_factor_record_counts_as_sample_without_factor_definitions(self):
        parsed = workbench.parse_study("ST000001", [sample_record(factors="")])
        self.assertEqual(parsed["sample_count"], 1)
        self.assertEqual(parsed["factors"], [])
        self.assertEqual(len(parsed["samples"]), 1)
        self.assertEqual(parsed["samples"][0]["factor_name"], "")
        self.assertEqual(parsed["samples"][0]["factor_value"], "")
        self.assertEqual(parsed["samples"][0]["factor_position"], 0)
        self.assertTrue(parsed["warnings"])

    def test_missing_factor_value_keeps_factor_and_sample_evidence_with_warning(self):
        parsed = workbench.parse_study("ST000001", [sample_record(factors="Treatment:")])
        self.assertEqual(parsed["sample_count"], 1)
        self.assertEqual([row["factor_name"] for row in parsed["factors"]], ["Treatment"])
        self.assertEqual(parsed["samples"][0]["factor_name"], "Treatment")
        self.assertEqual(parsed["samples"][0]["factor_value"], "")
        self.assertTrue(parsed["warnings"])


class UniqueValueAggregationTests(unittest.TestCase):
    def test_helper_groups_exact_keys_preserves_values_and_copies_factor_rows(self):
        factor_rows = []
        for study_id, name in (("ST000001", "Treatment"), ("ST000002", "Treatment"), ("ST000001", "treatment")):
            row = dict.fromkeys(workbench.FACTOR_FIELDS, "")
            row.update(study_id=study_id, factor_name=name)
            factor_rows.append(row)
        sample_rows = [
            {"study_id": "ST000001", "factor_name": "Treatment", "factor_value": value}
            for value in (" left ", "left", "", "A|B;C__D", "left")
        ] + [
            {"study_id": "ST000002", "factor_name": "Treatment", "factor_value": "right"},
            {"study_id": "ST000001", "factor_name": "treatment", "factor_value": "case"},
        ]
        enriched = workbench.factor_values_unique(factor_rows, sample_rows)
        indexed = {(row["study_id"], row["factor_name"]): json.loads(row["factor_value_unique"]) for row in enriched}
        self.assertEqual(indexed, {
            ("ST000001", "Treatment"): ["", " left ", "A|B;C__D", "left"],
            ("ST000002", "Treatment"): ["right"],
            ("ST000001", "treatment"): ["case"],
        })
        self.assertTrue(all(row["factor_value_unique"] == "" for row in factor_rows))
        self.assertIsNot(enriched[0], factor_rows[0])

    def test_missing_evidence_for_named_factor_fails(self):
        rows = [{"study_id": "ST000001", "factor_name": "Treatment"}]
        with self.assertRaises(ValueError):
            workbench.factor_values_unique(rows, [
                {"study_id": "ST000002", "factor_name": "Treatment", "factor_value": "Control"},
            ])

    @patch("workbench_factors.WorkbenchClient", side_effect=AssertionError("Offline update must not create a network client"))
    def test_offline_update_preserves_rows_other_columns_and_sample_file(self, client):
        rows = []
        keys = (("ST000002", "Treatment"), ("ST000001", "Treatment"), ("ST000001", "Sex"))
        for study_id, name in keys:
            row = dict.fromkeys(workbench.BASE_FACTOR_FIELDS, "")
            row.update(study_id=study_id, factor_name=name, factor_type="existing label", curation_note='Keep "this", β')
            rows.append(row)
        exact_value = 'C|"D";__β'
        samples = [
            ("ST000001", "Treatment", "Control"), ("ST000002", "Treatment", "Treated"),
            ("ST000001", "Treatment", ""), ("ST000001", "Treatment", exact_value),
            ("ST000001", "Treatment", "Control"), ("ST000001", "Sex", "female"),
            ("ST000001", "", "unnamed evidence remains untouched"),
        ]
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            output = Path(directory)
            factor_path, sample_path = output / "factors.csv", output / "sample_factors.csv"
            fields = [*workbench.BASE_FACTOR_FIELDS, "curation_note"]
            with factor_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            with sample_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(("study_id", "factor_name", "factor_value"))
                writer.writerows(samples)
            original_samples = sample_path.read_bytes()
            self.assertEqual(workbench.main(["--output-dir", str(output), "--update-factor-values"]), 0)
            client.assert_not_called()
            self.assertEqual(sample_path.read_bytes(), original_samples)
            self.assertFalse((output / ".cache").exists())
            with factor_path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                updated = list(reader)
                self.assertEqual(reader.fieldnames, [*fields, "factor_value_unique"])
            self.assertEqual([{key: value for key, value in row.items() if key != "factor_value_unique"} for row in updated], rows)
            self.assertEqual([json.loads(row["factor_value_unique"]) for row in updated], [
                ["Treated"], ["", "Control", exact_value], ["female"],
            ])
            updated_bytes = factor_path.read_bytes()
            self.assertEqual(workbench.main(["--output-dir", str(output), "--update-factor-values"]), 0)
            self.assertEqual(factor_path.read_bytes(), updated_bytes)


class WorkbenchClientTests(unittest.TestCase):
    @patch("workbench_factors.time.sleep")
    @patch("workbench_factors.urlopen")
    def test_network_and_http_200_non_json_errors_are_retried(self, urlopen, sleep):
        urlopen.side_effect = [URLError("offline"), io.BytesIO(b"<html>Error</html>"), io.BytesIO(b"[]")]
        self.assertEqual(workbench.WorkbenchClient(retries=2, interval=0).get_json("study/study_id/ST000001/factors"), [])
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    @patch("workbench_factors.time.sleep")
    @patch("workbench_factors.urlopen")
    def test_missing_study_http_error_is_not_retried(self, urlopen, sleep):
        urlopen.side_effect = HTTPError("https://example.org", 404, "Not found", {}, io.BytesIO(b"Not found"))
        with self.assertRaises(HTTPError):
            workbench.WorkbenchClient(retries=3, interval=0).get_json("study/study_id/ST000001/factors")
        urlopen.assert_called_once()
        sleep.assert_not_called()


class ExportTests(unittest.TestCase):
    def read_csv(self, path):
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def test_unnamed_empty_and_nonempty_slots_keep_sample_positions_without_inventing_factors(self):
        note = "For CE, DG, PC, PE, TG, Cholesterol lipid class"
        raw_values = ["Treatment:Control | :", "Treatment:Treated | :" + note]
        client = Mock()
        flat_records = [
            sample_record(study_id="ST002402", factors=raw_values[0]),
            sample_record("102", "Sample 2", raw_values[1], study_id="ST002402"),
        ]
        replies = {
            "study/study_id/ST002402/factors": flat_records,
            "study/study_id/ST002402/mwtab": native_investigation("ST002402", [
                ("Sample 1", {"Treatment": "Control", "": ""}),
                ("Sample 2", {"Treatment": "Treated", "": note}),
            ]),
        }
        client.get_json.side_effect = replies.__getitem__
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            output = Path(directory)
            self.assertEqual(workbench.export_factors(["ST002402"], client, output), 0)
            self.assertEqual([row["factor_name"] for row in self.read_csv(output / "factors.csv")], ["Treatment"])
            evidence = self.read_csv(output / "sample_factors.csv")
            self.assertEqual(len(evidence), 4)
            self.assertEqual([
                (row["mb_sample_id"], row["factor_position"], row["factor_name"], row["factor_value"]) for row in evidence
            ], [("101", "1", "Treatment", "Control"), ("101", "2", "", ""),
                ("102", "1", "Treatment", "Treated"), ("102", "2", "", note)])
            self.assertEqual([row["raw_factors"] for row in evidence], [raw_values[0], raw_values[0], raw_values[1], raw_values[1]])
            status = self.read_csv(output / "study_status.csv")[0]
            self.assertEqual(status["status"], "ok")
            self.assertEqual(status["factor_count"], "1")
            self.assertEqual(status["sample_count"], "2")
            self.assertEqual(status["sample_factor_count"], "2")
            self.assertEqual(status["warning_count"], "2")
            warnings = self.read_csv(output / "parsing_warnings.csv")
            self.assertEqual(len(warnings), 2)
            self.assertTrue(all("Missing factor name" in row["warning"] for row in warnings))

    def test_export_keeps_study_factors_and_evidence_consistent_and_reuses_cache(self):
        payloads = {
            "study/study_id/ST000001/factors": [sample_record()],
            "study/study_id/ST000002/factors": [sample_record(study_id="ST000002", factors="Treatment: treated")],
            "study/study_id/ST000003/factors": [sample_record(study_id="ST000003", factors="")],
            "study/study_id/ST000003/mwtab": native_investigation("ST000003", [("Sample 1", {})]),
        }
        client = Mock()
        client.get_json.side_effect = payloads.__getitem__
        selected = ["ST000001", "ST000002", "ST000003"]
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            output = Path(directory)
            self.assertEqual(workbench.export_factors(selected, client, output, workers=2), 0)
            factor_rows = self.read_csv(output / "factors.csv")
            evidence = self.read_csv(output / "sample_factors.csv")
            statuses = self.read_csv(output / "study_status.csv")
            self.assertEqual([(row["study_id"], row["factor_name"]) for row in factor_rows], [
                ("ST000001", "Treatment"), ("ST000002", "Treatment"),
            ])
            self.assertEqual({row["study_id"]: json.loads(row["factor_value_unique"]) for row in factor_rows}, {
                "ST000001": ["control"], "ST000002": ["treated"],
            })
            self.assertNotIn("factor_value_unique", self.read_csv(output / "ontology_mapping_template.csv")[0])
            self.assertEqual([row["study_id"] for row in statuses], selected)
            self.assertEqual([row["status"] for row in statuses], ["ok", "ok", "no_factors"])
            self.assertEqual([row["sample_count"] for row in statuses], ["1", "1", "1"])
            self.assertEqual(sum(int(row["sample_factor_count"]) for row in statuses), sum(bool(row["factor_name"]) for row in evidence))
            self.assertEqual(len(evidence), 3)
            self.assertEqual(evidence[-1]["factor_name"], "")
            self.assertEqual([row["factor_position"] for row in evidence], ["1", "1", "0"])
            positions = {(row["study_id"], row["mb_sample_id"], row["factor_position"]) for row in evidence}
            self.assertEqual(len(positions), len(evidence))
            self.assertEqual(len(self.read_csv(output / "parsing_warnings.csv")), 1)
            for row in factor_rows:
                self.assertTrue(any(item["study_id"] == row["study_id"] and item["factor_name"] == row["factor_name"] for item in evidence))
                self.assertEqual(row["ontology_accession"], "")
            client.get_json.reset_mock()
            self.assertEqual(workbench.export_factors(selected, client, output, workers=2), 0)
            client.get_json.assert_not_called()
            self.assertEqual(self.read_csv(output / "factors.csv"), factor_rows)
            self.assertEqual(self.read_csv(output / "sample_factors.csv"), evidence)
            self.assertEqual({row["source"] for row in self.read_csv(output / "study_status.csv")}, {"cache"})

    def test_parse_and_fetch_failures_are_distinguished_without_partial_study_rows(self):
        def response(path):
            if path == "study/study_id/ST000001/mwtab":
                return {"error": "Malformed native source metadata"}
            if path == "study/study_id/ST000001/factors":
                return [sample_record(), sample_record("102", "Sample 2", "Malformed factors")]
            if path == "study/study_id/ST000002/factors":
                raise URLError("offline")
            if path == "study/study_id/ST000002/mwtab":
                raise URLError("native source also offline")
            if path == "study/study_id/ST000003/factors":
                return [sample_record(study_id="ST000003")]
            raise AssertionError("Unexpected API route: " + path)

        client = Mock()
        client.get_json.side_effect = response
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            output = Path(directory)
            self.assertEqual(workbench.export_factors(["ST000001", "ST000002", "ST000003"], client, output, workers=2), 2)
            statuses = self.read_csv(output / "study_status.csv")
            self.assertEqual([row["status"] for row in statuses], ["parse_error", "fetch_error", "ok"])
            self.assertTrue(all(row["error"] for row in statuses[:2]))
            self.assertEqual({row["study_id"] for row in self.read_csv(output / "factors.csv")}, {"ST000003"})
            self.assertEqual({row["study_id"] for row in self.read_csv(output / "sample_factors.csv")}, {"ST000003"})
            self.assertTrue((output / ".cache" / "ST000001.json").exists())
            self.assertFalse((output / ".cache" / "ST000002.json").exists())

    def test_native_fallback_restores_omitted_sample_source_and_reuses_both_caches(self):
        study_id = "ST003205"
        baseline = {"Time point": "Baseline", "Sample source": "Serum", "Therapy": "Control"}
        cases = (
            ("Time point:Baseline | | Therapy:Control", baseline),
            ("Malformed factor text", baseline),
            ("Time point:After 30 Days (30D) (30D) | | Therapy:Control",
             {**baseline, "Time point": "After 30 Days (30D)  (30D)"}),
        )
        for raw, native_factors in cases:
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
                output = Path(directory)
                client = Mock()
                replies = {
                    f"study/study_id/{study_id}/factors": [sample_record(study_id=study_id, factors=raw)],
                    f"study/study_id/{study_id}/mwtab": [
                        native_investigation(study_id, [("Sample 1", native_factors)], "AN005257"),
                        native_investigation(study_id, [("Sample 1", native_factors)], "AN005258"),
                    ],
                }
                client.get_json.side_effect = replies.__getitem__
                self.assertEqual(workbench.export_factors([study_id], client, output), 0)
                factors = self.read_csv(output / "factors.csv")
                self.assertEqual({row["factor_name"] for row in factors}, set(native_factors))
                self.assertEqual(len(factors), 3)
                self.assertTrue(all(row["ontology_accession"] == "" for row in factors))
                evidence = self.read_csv(output / "sample_factors.csv")
                self.assertEqual({row["factor_name"]: row["factor_value"] for row in evidence}, native_factors)
                self.assertEqual({row["factor_source"] for row in evidence}, {"mwtab"})
                self.assertEqual({row["analysis_ids"] for row in evidence}, {"AN005257;AN005258"})
                self.assertTrue(all(row["raw_factors"] == raw for row in evidence))
                self.assertTrue(all(row["mb_sample_id"] == "101" and row["sample_source"] == "blood plasma" for row in evidence))
                status = self.read_csv(output / "study_status.csv")[0]
                self.assertEqual(status["factor_source"], "mwtab")
                self.assertEqual(status["factor_count"], "3")
                self.assertEqual(status["sample_count"], "1")
                native_cache = output / ".cache" / "mwtab" / (study_id + ".json")
                self.assertTrue(native_cache.exists())
                self.assertTrue(native_cache.with_suffix(".metadata.json").exists())
                client.get_json.reset_mock()
                self.assertEqual(workbench.export_factors([study_id], client, output), 0)
                client.get_json.assert_not_called()
                self.assertEqual(self.read_csv(output / "sample_factors.csv"), evidence)
                cached_status = self.read_csv(output / "study_status.csv")[0]
                self.assertEqual(cached_status["source"], "cache")
                self.assertEqual(cached_status["factor_source"], "mwtab")

    def test_native_fallback_rejects_missing_samples_or_conflicting_named_facts(self):
        study_id = "ST003205"
        raw = "Time point:Baseline | | Therapy:Control"
        expected = {"Time point": "Baseline", "Sample source": "Serum", "Therapy": "Control"}
        cases = (
            (native_investigation(study_id, [("Different Sample", expected)]), "cover"),
            (native_investigation(study_id, [("Sample 1", dict(expected, Therapy="Changed"))]), "discard"),
            ([native_investigation(study_id, [("Sample 1", expected)], "AN005257"),
              native_investigation(study_id, [("Sample 1", dict(expected, Therapy="Changed"))], "AN005258")], "Conflicting native"),
        )
        for native, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
                output = Path(directory)
                client = Mock()
                replies = {
                    f"study/study_id/{study_id}/factors": [sample_record(study_id=study_id, factors=raw)],
                    f"study/study_id/{study_id}/mwtab": native,
                }
                client.get_json.side_effect = replies.__getitem__
                self.assertEqual(workbench.export_factors([study_id], client, output), 1)
                status = self.read_csv(output / "study_status.csv")[0]
                self.assertEqual(status["status"], "parse_error")
                self.assertIn(message, status["error"])
                self.assertEqual(self.read_csv(output / "factors.csv"), [])
                self.assertEqual(self.read_csv(output / "sample_factors.csv"), [])

    def test_native_fallback_cannot_collapse_repeated_identical_flat_facts(self):
        study_id = "ST003205"
        raw = "Treatment:Control | Treatment:Control | | Dose:0"
        replies = {
            f"study/study_id/{study_id}/factors": [sample_record(study_id=study_id, factors=raw)],
            f"study/study_id/{study_id}/mwtab": native_investigation(study_id, [
                ("Sample 1", {"Treatment": "Control", "Sample source": "Serum", "Dose": "0"}),
            ]),
        }
        client = Mock()
        client.get_json.side_effect = replies.__getitem__
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            output = Path(directory)
            self.assertEqual(workbench.export_factors([study_id], client, output), 1)
            status = self.read_csv(output / "study_status.csv")[0]
            self.assertEqual(status["status"], "parse_error")
            self.assertIn("discard", status["error"])
            self.assertEqual(self.read_csv(output / "factors.csv"), [])
            self.assertEqual(self.read_csv(output / "sample_factors.csv"), [])

    def test_native_recovery_preserves_underscore_values_after_known_flat_serialization_bug(self):
        cases = (
            ("ST004112", "Cell type:Neuron | Drug:Picrotin | :Picrotoxinin",
             {"Cell type": "Neuron", "Drug": "Picrotin__Picrotoxinin"}),
            ("ST004776", "Annotation:B.breve | Target:chat3k | Sample source:ACh | Special:in vitro | :None",
             {"Annotation": "B.breve__chat3k", "Target": "ACh", "Sample source": "in vitro", "Special": "None"}),
        )
        for study_id, raw, native_factors in cases:
            with self.subTest(study_id=study_id), tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
                output = Path(directory)
                client = Mock()
                replies = {
                    f"study/study_id/{study_id}/factors": [sample_record(study_id=study_id, factors=raw)],
                    f"study/study_id/{study_id}/mwtab": native_investigation(study_id, [("Sample 1", native_factors)]),
                }
                client.get_json.side_effect = replies.__getitem__
                self.assertEqual(workbench.export_factors([study_id], client, output), 0)
                evidence = self.read_csv(output / "sample_factors.csv")
                self.assertEqual({row["factor_name"]: row["factor_value"] for row in evidence}, native_factors)
                self.assertTrue(all(row["raw_factors"] == raw and row["factor_source"] == "mwtab" for row in evidence))
                self.assertEqual({row["factor_name"] for row in self.read_csv(output / "factors.csv")}, set(native_factors))
                warnings = self.read_csv(output / "parsing_warnings.csv")
                self.assertTrue(any("split '__'" in row["warning"] for row in warnings))

    def test_underscore_serialization_recovery_rejects_near_matches(self):
        cases = (
            ("ST004112", "Cell type:Neuron | Drug:Picrotin | :Picrotoxinin changed",
             {"Cell type": "Neuron", "Drug": "Picrotin__Picrotoxinin"}),
            ("ST004776", "Annotation:B.breve | Target:chat3k | Sample source:ACh | Special:in vitro | :Other",
             {"Annotation": "B.breve__chat3k", "Target": "ACh", "Sample source": "in vitro", "Special": "None"}),
        )
        for study_id, raw, native_factors in cases:
            with self.subTest(study_id=study_id), tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
                output = Path(directory)
                client = Mock()
                replies = {
                    f"study/study_id/{study_id}/factors": [sample_record(study_id=study_id, factors=raw)],
                    f"study/study_id/{study_id}/mwtab": native_investigation(study_id, [("Sample 1", native_factors)]),
                }
                client.get_json.side_effect = replies.__getitem__
                self.assertEqual(workbench.export_factors([study_id], client, output), 1)
                status = self.read_csv(output / "study_status.csv")[0]
                self.assertEqual(status["status"], "parse_error")
                self.assertIn("discard", status["error"])
                self.assertEqual(self.read_csv(output / "sample_factors.csv"), [])

    def test_current_analysis_listing_filters_obsolete_cache_and_refetches_only_current_analyses(self):
        study_id, current_id = "ST003205", "AN005257"
        raw = "Treatment:Control | | Dose:0"
        current_factors = {"Treatment": "Control", "Sample source": "Serum", "Dose": "0"}
        current = native_investigation(study_id, [("Sample 1", current_factors)], current_id)
        obsolete = native_investigation(study_id, [("Sample 1", {**current_factors, "NativeOld": "obsolete"})], "AN000001")
        for cached_native, refetch in (([obsolete, current], False), ([obsolete], True)):
            with self.subTest(refetch=refetch), tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
                output = Path(directory)
                native_dir = output / ".cache" / "mwtab"
                native_dir.mkdir(parents=True)
                (output / ".cache" / (study_id + ".json")).write_text(
                    json.dumps([sample_record(study_id=study_id, factors=raw)]), encoding="utf-8",
                )
                (native_dir / (study_id + ".json")).write_text(json.dumps(cached_native), encoding="utf-8")
                (native_dir / (study_id + ".metadata.json")).write_text(
                    json.dumps({"cache_format": workbench.MWTAB_CACHE_FORMAT}), encoding="utf-8",
                )
                client = workbench.WorkbenchClient(interval=0)
                client.register_analyses([{"study_id": study_id, "analysis_id": current_id}])

                def response(path):
                    if path == f"study/analysis_id/{current_id}/mwtab":
                        return current
                    raise AssertionError("Unexpected API route: " + path)

                client.get_json = Mock(side_effect=response)
                self.assertEqual(workbench.export_factors([study_id], client, output), 0)
                factors = self.read_csv(output / "factors.csv")
                self.assertEqual({row["factor_name"] for row in factors}, set(current_factors))
                self.assertNotIn("NativeOld", {row["factor_name"] for row in factors})
                evidence = self.read_csv(output / "sample_factors.csv")
                self.assertEqual({row["analysis_ids"] for row in evidence}, {current_id})
                self.assertEqual({row["factor_name"]: row["factor_value"] for row in evidence}, current_factors)
                if refetch:
                    client.get_json.assert_called_once_with(f"study/analysis_id/{current_id}/mwtab")
                else:
                    client.get_json.assert_not_called()

    def test_failed_flat_fetch_uses_native_only_samples_then_reuses_native_cache(self):
        study_id = "ST004741"
        native = native_investigation(study_id, [
            ("Native sample A", {"Treatment": "Control", "Dose": "0"}),
            ("Native sample B", {"Treatment": "Treated", "Dose": "1"}),
        ], "AN008018")

        def response(path):
            if path == f"study/study_id/{study_id}/factors":
                raise TimeoutError("Flat factors endpoint timed out")
            if path == f"study/study_id/{study_id}/mwtab":
                return native
            raise AssertionError("Unexpected API route: " + path)

        client = Mock()
        client.get_json.side_effect = response
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            output = Path(directory)
            self.assertEqual(workbench.export_factors([study_id], client, output), 0)
            factors = self.read_csv(output / "factors.csv")
            self.assertEqual({row["factor_name"] for row in factors}, {"Treatment", "Dose"})
            evidence = self.read_csv(output / "sample_factors.csv")
            self.assertEqual(len(evidence), 4)
            self.assertEqual({row["local_sample_id"] for row in evidence}, {"Native sample A", "Native sample B"})
            self.assertEqual({row["factor_source"] for row in evidence}, {"mwtab"})
            self.assertEqual({row["analysis_ids"] for row in evidence}, {"AN008018"})
            self.assertTrue(all(row["mb_sample_id"] == "" and row["sample_source"] == "" and row["raw_factors"] == "" for row in evidence))
            status = self.read_csv(output / "study_status.csv")[0]
            self.assertEqual(status["status"], "ok")
            self.assertEqual(status["source"], "mwtab_api")
            self.assertEqual(status["factor_source"], "mwtab")
            self.assertEqual(status["sample_count"], "2")
            self.assertFalse((output / ".cache" / (study_id + ".json")).exists())
            metadata_path = output / ".cache" / "mwtab" / (study_id + ".metadata.json")
            self.assertEqual(json.loads(metadata_path.read_text())["cache_format"], workbench.MWTAB_CACHE_FORMAT)
            client.get_json.reset_mock()
            self.assertEqual(workbench.export_factors([study_id], client, output), 0)
            client.get_json.assert_not_called()
            self.assertEqual(self.read_csv(output / "sample_factors.csv"), evidence)
            self.assertEqual(self.read_csv(output / "study_status.csv")[0]["source"], "native_cache")

    def test_invalid_cache_is_refetched_and_refresh_replaces_valid_cache(self):
        client = Mock()
        client.get_json.return_value = [sample_record(factors="Treatment: control")]
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            output = Path(directory)
            cache = output / ".cache"
            cache.mkdir()
            path = cache / "ST000001.json"
            path.write_text(json.dumps([sample_record(study_id="ST000002")]), encoding="utf-8")
            self.assertEqual(workbench.export_factors(["ST000001"], client, output), 0)
            client.get_json.assert_called_once_with("study/study_id/ST000001/factors")
            client.get_json.reset_mock()
            client.get_json.return_value = [sample_record(factors="Sex: female")]
            self.assertEqual(workbench.export_factors(["ST000001"], client, output, refresh=True), 0)
            client.get_json.assert_called_once_with("study/study_id/ST000001/factors")
            self.assertEqual([row["factor_name"] for row in self.read_csv(output / "factors.csv")], ["Sex"])
            self.assertEqual(json.loads(path.read_text())[0]["factors"], "Sex: female")

    def test_enrichment_retains_extra_provenance_and_audits_unmatched_mappings(self):
        raw_rows = []
        for study_id in ("ST000001", "ST000002"):
            row = dict.fromkeys(workbench.FACTOR_FIELDS, "")
            row.update(study_id=study_id, factor_name="Treatment")
            row["factor_value_unique"] = json.dumps(["Control", "Treated"] if study_id == "ST000001" else ["Other"])
            raw_rows.append(row)
        approved = {
            "study_id": "ST000001", "factor_name": "Treatment", "factor_type": "treatment",
            "ontology_source": "EFO", "ontology_accession": "http://www.ebi.ac.uk/efo/EFO_0000727",
            "ontology_version": "", "ontology_file": "", "mapping_method": "manual review",
            "evidence": "Study design reviewed", "review_status": "approved", "reviewer": "A. Reviewer",
        }
        unmatched = dict(approved, study_id="ST999999", factor_name="Unmatched factor", confidence="high")
        unmatched.pop("reviewer")
        mappings = {
            ("ST000001", "Treatment"): approved,
            ("ST999999", "Unmatched factor"): unmatched,
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            raw_path = output / "factors.csv"
            with raw_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=workbench.FACTOR_FIELDS)
                writer.writeheader()
                writer.writerows(raw_rows)
            original_bytes = raw_path.read_bytes()
            self.assertEqual(workbench.write_enrichment(output, mappings), 1)
            self.assertEqual(raw_path.read_bytes(), original_bytes)

            enriched = self.read_csv(output / "factors_enriched.csv")
            self.assertEqual(enriched[0]["ontology_accession"], approved["ontology_accession"])
            self.assertEqual(enriched[0]["factor_type"], "treatment")
            self.assertEqual(enriched[0]["factor_value_unique"], raw_rows[0]["factor_value_unique"])
            self.assertEqual(enriched[1], raw_rows[1])
            self.assertNotIn("reviewer", enriched[0])

            provenance = self.read_csv(output / "mapping_provenance.csv")
            indexed = {(row["study_id"], row["factor_name"]): row for row in provenance}
            self.assertEqual(set(indexed), set(mappings))
            applied = indexed[("ST000001", "Treatment")]
            self.assertEqual(applied["application_status"], "applied")
            self.assertEqual(applied["reviewer"], "A. Reviewer")
            self.assertEqual(applied["confidence"], "")
            audited = indexed[("ST999999", "Unmatched factor")]
            self.assertEqual(audited["application_status"], "unmatched")
            self.assertEqual(audited["confidence"], "high")
            self.assertEqual(audited["reviewer"], "")


if __name__ == "__main__":
    unittest.main()
