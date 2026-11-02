"""Review approval and exact matching checks for Workbench enrichment."""

import csv
import tempfile
import unittest
from pathlib import Path

from workbench_mapping import ANNOTATION_FIELDS, MAPPING_FIELDS, apply_mappings, load_mappings


def approved_mapping(**updates):
    row = {
        "study_id": "ST000001",
        "factor_name": "Treatment",
        "factor_type": "treatment",
        "ontology_source": "EFO",
        "ontology_accession": "http://www.ebi.ac.uk/efo/EFO_0000727",
        "ontology_version": "",
        "ontology_file": "",
        "mapping_method": "manual review",
        "evidence": "Study design and factor documentation reviewed",
        "review_status": "approved",
    }
    row.update(updates)
    return row


class LoadMappingsTests(unittest.TestCase):
    def write_mapping_csv(self, directory, rows, fields=MAPPING_FIELDS):
        path = Path(directory) / "mappings.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def test_reviewed_annotations_and_provenance_are_preserved(self):
        row = approved_mapping(reviewer="reviewer initials")
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_mapping_csv(directory, [row], (*MAPPING_FIELDS, "reviewer"))
            loaded = load_mappings(path)
        self.assertEqual(loaded, {("ST000001", "Treatment"): row})
        self.assertEqual(loaded[("ST000001", "Treatment")]["ontology_version"], "")

    def test_incomplete_or_unapproved_mappings_are_rejected(self):
        invalid = [
            approved_mapping(study_id="ST1"),
            approved_mapping(study_id="ST000001/other"),
            approved_mapping(study_id="ST１２３４５６"),
            approved_mapping(review_status="pending"),
            approved_mapping(review_status="Approved"),
        ]
        for field in ("factor_name", "factor_type", "ontology_source", "ontology_accession", "mapping_method", "evidence"):
            invalid.append(approved_mapping(**{field: "  "}))
        for row in invalid:
            with self.subTest(row=row), tempfile.TemporaryDirectory() as directory:
                path = self.write_mapping_csv(directory, [row])
                with self.assertRaises(ValueError):
                    load_mappings(path)

    def test_invalid_accession_is_rejected_but_curie_is_preserved(self):
        for accession in ("EFO", "http://www.ebi.ac.uk/metabolights/ontology/placeholder"):
            with self.subTest(accession=accession), tempfile.TemporaryDirectory() as directory:
                path = self.write_mapping_csv(directory, [approved_mapping(ontology_accession=accession)])
                with self.assertRaisesRegex(ValueError, "invalid ontology_accession"):
                    load_mappings(path)
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_mapping_csv(directory, [approved_mapping(ontology_accession="EFO:0000727")])
            self.assertEqual(load_mappings(path)[("ST000001", "Treatment")]["ontology_accession"], "EFO:0000727")

    def test_duplicate_keys_are_rejected_even_with_identical_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_mapping_csv(directory, [approved_mapping(), approved_mapping()])
            with self.assertRaisesRegex(ValueError, "duplicate mapping"):
                load_mappings(path)

    def test_missing_columns_and_malformed_records_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mappings.csv"
            path.write_text("study_id,factor_name\nST000001,Treatment\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing columns"):
                load_mappings(path)
            path = self.write_mapping_csv(directory, [approved_mapping()])
            with path.open("a", encoding="utf-8") as handle:
                handle.write("ST000002,Treatment\n")
            with self.assertRaisesRegex(ValueError, "malformed CSV record"):
                load_mappings(path)


class ApplyMappingsTests(unittest.TestCase):
    def test_only_exact_matches_change_annotation_fields_without_mutation(self):
        factors = [
            {"study_id": "ST000001", "factor_name": "Treatment", "factor_type": "raw label", "sample_count": 6},
            {"study_id": "ST000002", "factor_name": "Treatment", "factor_type": "unmapped label"},
            {"study_id": "ST000001", "factor_name": "treatment", "factor_type": "case differs"},
        ]
        mapping = approved_mapping()
        mappings = {
            ("ST000001", "Treatment"): mapping,
            ("ST999999", "Unmatched factor"): approved_mapping(study_id="ST999999", factor_name="Unmatched factor"),
        }
        enriched = apply_mappings(factors, mappings)
        self.assertIsNot(enriched[0], factors[0])
        self.assertEqual(factors[0]["factor_type"], "raw label")
        for field in ANNOTATION_FIELDS:
            self.assertEqual(enriched[0][field], mapping[field])
        self.assertEqual(enriched[0]["study_id"], "ST000001")
        self.assertEqual(enriched[0]["factor_name"], "Treatment")
        self.assertEqual(enriched[0]["sample_count"], 6)
        self.assertNotIn("mapping_method", enriched[0])
        self.assertEqual(enriched[1:], factors[1:])
        self.assertIn(("ST999999", "Unmatched factor"), mappings)

    def test_direct_unapproved_or_miskeyed_mappings_cannot_apply(self):
        factors = [{"study_id": "ST000001", "factor_name": "Treatment"}]
        for mapping in (approved_mapping(review_status="pending"), approved_mapping(study_id="ST000002")):
            with self.subTest(mapping=mapping), self.assertRaises(ValueError):
                apply_mappings(factors, {("ST000001", "Treatment"): mapping})


if __name__ == "__main__":
    unittest.main()
