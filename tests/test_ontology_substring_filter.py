"""Literal substring selection and full-row export checks."""

import csv
import io
import json
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path

import analyze_ontology_accessions as analysis


PURL = "http://purl.obolibrary.org/obo/"
EFO = "http://www.ebi.ac.uk/efo/"
SUBSTRINGS = [PURL, EFO]
PURL_TERM = PURL + "NCIT_C17357"
EFO_TERM = EFO + "EFO_0000195"
ODD_TERM = PURL + "zeco.owl/ZECO_0000188"
EMBEDDED_TERM = "prefix " + EFO_TERM + " suffix"
HTTPS_TERM = "https://purl.obolibrary.org/obo/NCIT_C17357"
UPPERCASE_TERM = "http://PURL.OBOLIBRARY.ORG/obo/NCIT_C17357"


class SubstringPolicyTests(unittest.TestCase):
    def test_selection_is_case_sensitive_literal_contains_with_or_semantics(self):
        raw = Counter({
            PURL_TERM: 3, EFO_TERM: 2, ODD_TERM: 1, EMBEDDED_TERM: 1,
            HTTPS_TERM: 4, UPPERCASE_TERM: 5, "": 6,
        })
        selected, excluded = analysis.partition_counts(raw, accession_contains=SUBSTRINGS)
        self.assertEqual(selected, Counter({PURL_TERM: 3, EFO_TERM: 2, ODD_TERM: 1, EMBEDDED_TERM: 1}))
        self.assertEqual(excluded, Counter({HTTPS_TERM: 4, UPPERCASE_TERM: 5, "": 6}))
        self.assertEqual(selected + excluded, raw)
        self.assertEqual(selected[PURL_TERM], 3)
        self.assertNotIn(HTTPS_TERM, selected)
        self.assertNotIn(UPPERCASE_TERM, selected)

    def test_custom_selection_overrides_validation_and_default_policy_remains_structural(self):
        foaf = "http://xmlns.com/foaf/0.1/Group"
        raw = Counter({PURL_TERM: 1, EFO_TERM: 1, ODD_TERM: 1, EMBEDDED_TERM: 1, HTTPS_TERM: 1, foaf: 1, "": 1})
        default, rejected = analysis.partition_counts(raw)
        self.assertEqual(default, Counter({PURL_TERM: 1, EFO_TERM: 1, HTTPS_TERM: 1, foaf: 1}))
        self.assertEqual(rejected, Counter({ODD_TERM: 1, EMBEDDED_TERM: 1, "": 1}))
        selected, _ = analysis.partition_counts(raw, accession_contains=SUBSTRINGS)
        self.assertIn(ODD_TERM, selected)
        self.assertIn(EMBEDDED_TERM, selected)
        self.assertNotIn(foaf, selected)
        self.assertEqual(analysis.partition_counts(raw)[0], default)


class SubstringCliTests(unittest.TestCase):
    def read_csv(self, path):
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            return reader.fieldnames, list(reader)

    def write_input(self, path, accessions):
        fields = [
            "study_id", "factor_name", "factor_type", "ontology_source", "ontology_accession",
            "ontology_version", "ontology_file", "note",
        ]
        rows = []
        for index, accession in enumerate(accessions, 1):
            row = dict.fromkeys(fields, "")
            row.update(study_id=f"MTBLS{index}", factor_name=f"Factor {index}", ontology_accession=accession,
                       note=f'Preserve "quoted", β metadata {index}')
            rows.append(row)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        return fields, rows

    def run_cli(self, source, output):
        capture = io.StringIO()
        with redirect_stdout(capture):
            status = analysis.main([
                "--input", str(source), "--output-dir", str(output), "--no-plot",
                "--accession-contains", PURL, "--accession-contains", EFO,
            ])
        return status, capture.getvalue()

    def test_cli_keeps_full_rows_and_reports_selected_denominators_and_literal_exclusions(self):
        accessions = [PURL_TERM, HTTPS_TERM, EFO_TERM, PURL_TERM, ODD_TERM, UPPERCASE_TERM, EMBEDDED_TERM, ""]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "factors.csv", root / "analysis"
            original_fields, original_rows = self.write_input(source, accessions)
            original_bytes = source.read_bytes()
            self.assertEqual(analysis.read_counts(source, accession_contains=SUBSTRINGS), Counter({PURL_TERM: 2, EFO_TERM: 1, ODD_TERM: 1, EMBEDDED_TERM: 1}))
            status, printed = self.run_cli(source, output)
            self.assertEqual(status, 0)
            self.assertEqual(source.read_bytes(), original_bytes)
            fields, filtered = self.read_csv(output / "factors_filtered.csv")
            self.assertEqual(fields, original_fields)
            self.assertEqual(filtered, [original_rows[index] for index in (0, 2, 3, 4, 6)])

            fields, frequencies = self.read_csv(output / "ontology_accession_frequencies.csv")
            self.assertIn("percent_of_selected_factors", fields)
            self.assertNotIn("percent_of_valid_factors", fields)
            self.assertEqual(sum(int(row["frequency"]) for row in frequencies), 5)
            indexed = {row["ontology_accession"]: row for row in frequencies}
            self.assertEqual(set(indexed), {PURL_TERM, EFO_TERM, ODD_TERM, EMBEDDED_TERM})
            self.assertEqual(indexed[PURL_TERM]["frequency"], "2")
            self.assertAlmostEqual(float(indexed[PURL_TERM]["percent_of_selected_factors"]), 40.0)
            self.assertAlmostEqual(float(indexed[PURL_TERM]["percent_of_all_factors"]), 25.0)
            self.assertAlmostEqual(sum(float(row["percent_of_selected_factors"]) for row in frequencies), 100.0)

            _, exclusions = self.read_csv(output / "excluded_ontology_accessions.csv")
            self.assertEqual({row["ontology_accession"] for row in exclusions}, {HTTPS_TERM, UPPERCASE_TERM, ""})
            self.assertEqual({row["reason"] for row in exclusions}, {"not_matching_requested_substring"})
            self.assertEqual(sum(int(row["frequency"]) for row in exclusions), 3)
            self.assertAlmostEqual(sum(float(row["percent_of_all_factors"]) for row in frequencies + exclusions), 100.0)

            _, distribution = self.read_csv(output / "ontology_accession_frequency_distribution.csv")
            self.assertEqual({int(row["frequency"]): int(row["number_of_accessions"]) for row in distribution}, {1: 3, 2: 1})
            summary = json.JSONDecoder().raw_decode(printed.lstrip())[0]
            self.assertEqual(summary["input_factor_rows"], 8)
            self.assertEqual(summary["included_factor_rows"], 5)
            self.assertEqual(summary["excluded_factor_rows"], 3)
            self.assertEqual(summary["distinct_selected_accessions"], 4)
            self.assertEqual(summary["filter_mode"], "exact_case_sensitive_substring_or")
            self.assertEqual(summary["accession_contains"], SUBSTRINGS)

    def test_no_matching_rows_writes_empty_selected_tables_with_original_filtered_header(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "factors.csv", root / "analysis"
            original_fields, _ = self.write_input(source, [HTTPS_TERM, UPPERCASE_TERM, ""])
            status, printed = self.run_cli(source, output)
            self.assertEqual(status, 0)
            self.assertEqual(self.read_csv(output / "factors_filtered.csv"), (original_fields, []))
            self.assertEqual(self.read_csv(output / "ontology_accession_frequencies.csv")[1], [])
            self.assertEqual(self.read_csv(output / "ontology_accession_frequency_distribution.csv")[1], [])
            self.assertEqual(sum(int(row["frequency"]) for row in self.read_csv(output / "excluded_ontology_accessions.csv")[1]), 3)
            summary = json.JSONDecoder().raw_decode(printed.lstrip())[0]
            self.assertEqual(summary["distinct_selected_accessions"], 0)


if __name__ == "__main__":
    unittest.main()
