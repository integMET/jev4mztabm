"""Checks for structural identifier filtering and frequency denominators."""

import csv
import io
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path

import analyze_ontology_accessions as analysis


class IdentifierValidationTests(unittest.TestCase):
    def test_valid_identifiers_include_named_terms_and_curie_forms(self):
        identifiers = (
            "http://www.ebi.ac.uk/efo/EFO_0000195",
            "https://www.ebi.ac.uk/efo/EFO_0000195",
            "http://purl.obolibrary.org/obo/NCIT_C17357",
            "http://xmlns.com/foaf/0.1/Group",
            "http://xmlns.com/foaf/0.1/gender",
            "http://www.w3.org/2006/time#DateTimeDescription",
            "http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#C43359",
            "https://example.org/ontology.owl#namedTerm",
            "https://example.org/ontology.owl#term%23part",
            "http://www.ebi.ac.uk./efo/EFO_0000195",
            "http://purl.obolibrary.org/obo/po#wholePlantDevelopmentStage",
            "http://purl.obolibrary.org/obo/NCBITaxon_species",
            "EFO:0005135",
        )
        for identifier in identifiers:
            with self.subTest(identifier=identifier):
                self.assertIsNone(analysis.identifier_rejection_reason(identifier))

    def test_blank_labels_and_placeholders_are_rejected(self):
        identifiers = (
            "", " ", "\t\n", "EFO", "NCIT", "Acc", "-", "EFO:",
            "http://www.ebi.ac.uk/metabolights/ontology/placeholder",
        )
        for identifier in identifiers:
            with self.subTest(identifier=identifier):
                self.assertTrue(analysis.identifier_rejection_reason(identifier))

    def test_ontology_documents_and_namespace_roots_are_rejected(self):
        identifiers = (
            "http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl",
            "http://ontology.apa.org/apaonto/termsonlyOUT%20(5).owl",
            "http://sweet.jpl.nasa.gov/2.3/matrFacility.owl",
            "http://purl.obolibrary.org/obo/",
            "http://www.ebi.ac.uk/efo/",
            "http://regenbase.org/ontology",
            "http://xmlns.com/foaf/0.1/",
        )
        for identifier in identifiers:
            with self.subTest(identifier=identifier):
                self.assertTrue(analysis.identifier_rejection_reason(identifier))

    def test_malformed_deposited_ontology_urls_are_rejected(self):
        identifiers = (
            "http://purl.obolibrary.org/http///www.ebi.ac.uk/efo/EFO_0000683",
            "http://purl.obolibrary.org/obo/zeco.owl/ZECO_0000188",
            "http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owlC45290",
            "http://ncicb.nci.nih.gov./xml/owl/EVS/Thesaurus.owlC45290",
            "http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl/C45290",
            "http://purl.org/obo/owlATOL_0001236",
        )
        for identifier in identifiers:
            with self.subTest(identifier=identifier):
                self.assertTrue(analysis.identifier_rejection_reason(identifier))

    def test_malformed_http_urls_and_percent_escapes_are_rejected(self):
        identifiers = (
            "http://", "https:///EFO_0000195", "http://bad host/EFO_0000195",
            "http://example.org:invalid/EFO_0000195",
            "https://example.org/term%", "https://example.org/term%2",
            "https://example.org/term%ZZ",
            "https://example.org/term##bad",
        )
        for identifier in identifiers:
            with self.subTest(identifier=identifier):
                self.assertTrue(analysis.identifier_rejection_reason(identifier))

    def test_local_and_private_http_targets_are_rejected(self):
        identifiers = (
            "http://localhost/EFO_0000195",
            "http://127.0.0.1/EFO_0000195",
            "http://127.0.0.1./Term",
            "http://10.0.0.1/EFO_0000195",
            "http://172.16.0.1/EFO_0000195",
            "http://192.168.1.12/EFO_0000195",
            "http://[::1]/EFO_0000195",
        )
        for identifier in identifiers:
            with self.subTest(identifier=identifier):
                self.assertTrue(analysis.identifier_rejection_reason(identifier))

    def test_partition_conserves_every_occurrence(self):
        raw = Counter({"EFO:0005135": 3, "": 2, "NCIT": 4})
        valid, excluded = analysis.partition_counts(raw)
        self.assertEqual(valid, Counter({"EFO:0005135": 3}))
        self.assertEqual(excluded, Counter({"": 2, "NCIT": 4}))
        self.assertEqual(valid + excluded, raw)
        self.assertEqual(sum(valid.values()) + sum(excluded.values()), 9)
        self.assertEqual(raw, Counter({"EFO:0005135": 3, "": 2, "NCIT": 4}))


class FrequencyExportTests(unittest.TestCase):
    def read_csv(self, path):
        with Path(path).open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def write_input(self, path, accessions):
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(("study_id", "ontology_accession"))
            for accession in accessions:
                writer.writerow(("MTBLS1", accession))

    def test_mixed_csv_preserves_uri_variants_and_uses_separate_denominators(self):
        original = "http://www.ebi.ac.uk/efo/EFO_0000195"
        secure = "https://www.ebi.ac.uk/efo/EFO_0000195"
        case_variant = "http://www.ebi.ac.uk/efo/efo_0000195"
        accessions = [original, original, secure, case_variant, "EFO:0005135", "", "", "NCIT"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "factors.csv"
            self.write_input(source, accessions)
            raw = analysis.read_accession_counts(source)
            valid, excluded = analysis.partition_counts(raw)
            self.assertEqual(analysis.read_counts(source), valid)
            self.assertEqual(raw[""], 2)
            self.assertEqual(sum(valid.values()), 5)
            self.assertEqual(set(valid), {original, secure, case_variant, "EFO:0005135"})
            analysis.write_tables(valid, root, excluded=excluded)

            rows = self.read_csv(root / "ontology_accession_frequencies.csv")
            self.assertEqual(set(rows[0]), {
                "ontology_accession", "frequency", "percent_of_valid_factors", "percent_of_all_factors",
            })
            indexed = {row["ontology_accession"]: row for row in rows}
            self.assertEqual(set(indexed), set(valid))
            self.assertEqual(indexed[original]["frequency"], "2")
            self.assertAlmostEqual(float(indexed[original]["percent_of_valid_factors"]), 40.0)
            self.assertAlmostEqual(float(indexed[original]["percent_of_all_factors"]), 25.0)
            self.assertAlmostEqual(sum(float(row["percent_of_valid_factors"]) for row in rows), 100.0)

            excluded_rows = self.read_csv(root / "excluded_ontology_accessions.csv")
            self.assertEqual(set(excluded_rows[0]), {
                "ontology_accession", "frequency", "reason", "percent_of_all_factors",
            })
            excluded_index = {row["ontology_accession"]: row for row in excluded_rows}
            self.assertEqual(set(excluded_index), {"", "NCIT"})
            self.assertEqual(excluded_index[""]["frequency"], "2")
            self.assertAlmostEqual(float(excluded_index[""]["percent_of_all_factors"]), 25.0)
            self.assertTrue(all(row["reason"] for row in excluded_rows))
            self.assertAlmostEqual(
                sum(float(row["percent_of_all_factors"]) for row in rows + excluded_rows), 100.0,
            )

            distribution = self.read_csv(root / "ontology_accession_frequency_distribution.csv")
            self.assertEqual({int(row["frequency"]): int(row["number_of_accessions"]) for row in distribution}, {1: 3, 2: 1})

    def test_all_invalid_input_still_writes_exclusions_and_header_only_valid_table(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            root = Path(directory)
            source = root / "factors.csv"
            output = root / "analysis"
            self.write_input(source, ["", "NCIT", "-"])
            self.assertEqual(analysis.main(["--input", str(source), "--output-dir", str(output), "--no-plot"]), 0)
            self.assertEqual(self.read_csv(output / "ontology_accession_frequencies.csv"), [])
            self.assertEqual(self.read_csv(output / "ontology_accession_frequency_distribution.csv"), [])
            self.assertEqual(sum(int(row["frequency"]) for row in self.read_csv(output / "excluded_ontology_accessions.csv")), 3)


if __name__ == "__main__":
    unittest.main()
