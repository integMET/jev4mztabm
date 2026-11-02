"""Source evidence and study-disjoint pilot checks for Workbench Jev inputs."""

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import workbench_jev as jev


FACTOR_FIELDS = (
    "study_id", "factor_name", "factor_type", "ontology_source", "ontology_accession",
    "ontology_version", "ontology_file", "factor_value_unique",
)


def write_csv(path, fields, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


class PreparedEvidenceTests(unittest.TestCase):
    def fixtures(self, root):
        factor_path, sample_path = root / "factors.csv", root / "sample_factors.csv"
        genotype_values = ["WT", *[f"Gene{index}-KO" for index in range(1, 15)]]
        specifications = [
            ("ST000001", "Group", ["WT", "GeneX-KO", "vehicle", "drug"]),
            ("ST000002", "Genotype", genotype_values),
        ]
        factors = []
        samples = []
        for study_id, name, values in specifications:
            row = dict.fromkeys(FACTOR_FIELDS, "")
            row.update(study_id=study_id, factor_name=name, factor_value_unique=json.dumps(values))
            factors.append(row)
            for index, value in enumerate(values):
                samples.append({
                    "study_id": study_id, "factor_name": name, "factor_value": value,
                    "factor_position": "2" if study_id == "ST000001" and index >= 2 else "1",
                    "sample_source": "blood plasma",
                })
        write_csv(factor_path, FACTOR_FIELDS, factors)
        write_csv(sample_path, ("study_id", "factor_name", "factor_value", "factor_position", "sample_source"), samples)
        return factor_path, sample_path, specifications

    def test_full_values_and_distinct_positions_preserve_genotype_and_mixed_row_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            factor_path, sample_path, specifications = self.fixtures(root)
            original_factors, original_samples = factor_path.read_bytes(), sample_path.read_bytes()
            contexts = jev.prepare_contexts(factor_path, sample_path)
            indexed = {(item["study_id"], item["factor_name"]): item for item in contexts}
            for study_id, name, values in specifications:
                self.assertEqual(indexed[(study_id, name)]["factor_values"], values)
            self.assertEqual(len(indexed[("ST000002", "Genotype")]["factor_values"]), 15)
            mixed = indexed[("ST000001", "Group")]
            self.assertEqual(mixed["sample_evidence"]["positions"], [
                {"factor_position": 1, "factor_values": ["GeneX-KO", "WT"]},
                {"factor_position": 2, "factor_values": ["drug", "vehicle"]},
            ])
            self.assertEqual(mixed["sample_evidence"]["sample_sources"], ["blood plasma"])
            self.assertEqual(mixed["source_sha256"], jev.file_hash(factor_path))
            self.assertEqual(mixed["sample_evidence"]["source_sha256"], jev.file_hash(sample_path))
            self.assertEqual(factor_path.read_bytes(), original_factors)
            self.assertEqual(sample_path.read_bytes(), original_samples)

    def test_sample_evidence_mismatch_is_rejected_instead_of_changing_export_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            factor_path, sample_path, _ = self.fixtures(root)
            original_factors = factor_path.read_bytes()
            sample_path.write_text(sample_path.read_text().replace("GeneX-KO", "Different genotype"), encoding="utf-8")
            with self.assertRaises(ValueError):
                jev.prepare_contexts(factor_path, sample_path)
            self.assertEqual(factor_path.read_bytes(), original_factors)

    def test_native_study_context_retains_source_text_and_omits_sample_and_contact_sections(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            factor_path, sample_path, _ = self.fixtures(root)
            cache = root / "workbench_cache"
            (cache / "mwtab").mkdir(parents=True)
            native_path = cache / "mwtab" / "ST000001.json"
            native_path.write_text(json.dumps({
                "METABOLOMICS WORKBENCH": {"STUDY_ID": "ST000001", "ANALYSIS_ID": "AN000001"},
                "STUDY": {"STUDY_TITLE": "Genotype and treatment comparison", "STUDY_SUMMARY": "WT and GeneX-KO mice received vehicle or drug.", "CONTACT_EMAIL": "omit@example.org"},
                "TREATMENT": {"TREATMENT_PROTOCOL": "Preserve exact protocol: dose 1; day 2."},
                "SUBJECT_SAMPLE_FACTORS": [{"Sample ID": "Individual sample identifier"}],
                "CONTACT": {"NAME": "Contact name", "EMAIL": "omit@example.org"},
            }), encoding="utf-8")
            prepared = jev.prepare_contexts(factor_path, sample_path, workbench_cache=cache)
            native = prepared[0]["study_context"][0]
            encoded = json.dumps(native["content"])
            self.assertIn("WT and GeneX-KO mice received vehicle or drug.", encoded)
            self.assertIn("Preserve exact protocol: dose 1; day 2.", encoded)
            self.assertNotIn("Individual sample identifier", encoded)
            self.assertNotIn("omit@example.org", encoded)
            self.assertNotIn("Contact name", encoded)
            self.assertEqual(native["source"], str(native_path.resolve()))
            self.assertEqual(native["source_sha256"], jev.file_hash(native_path))

    def test_missing_summary_narrative_recovers_current_native_protocol_and_reuses_extracted_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            factor_path, sample_path, _ = self.fixtures(root)
            native = {
                "METABOLOMICS WORKBENCH": {"STUDY_ID": "ST000001", "ANALYSIS_ID": "AN000001"},
                "STUDY": {"STUDY_TITLE": "Genotype and treatment comparison",
                          "STUDY_SUMMARY": "WT and GeneX-KO mice received vehicle or drug."},
                "TREATMENT": {"TREATMENT_PROTOCOL": "Exact protocol: 1 mg; collect after 24 hours."},
                "CONTACT": {"EMAIL": "do-not-cache@example.org", "PHONE": "+1234567890"},
                "SUBJECT_SAMPLE_FACTORS": [{"Sample ID": "do-not-cache-sample-123", "Factors": {"Group": "WT"}}],
            }
            replies = {
                "study/study_id/ST000001/summary": [{"study_id": "ST000001", "study_title": "Only a title", "species": "Mus musculus"}],
                "study/analysis_id/AN000001/mwtab": native,
            }
            client = Mock()
            client.get_json.side_effect = replies.__getitem__
            client.current_analysis_ids.return_value = ["AN000001"]
            cache = root / "context_cache"
            first = jev.prepare_contexts(factor_path, sample_path, fetch_client=client, context_cache=cache, studies=["ST000001"])
            self.assertEqual(len(first), 1)
            evidence = json.dumps(first[0]["study_context"])
            self.assertIn("WT and GeneX-KO mice received vehicle or drug.", evidence)
            self.assertIn("Exact protocol: 1 mg; collect after 24 hours.", evidence)
            self.assertEqual([call.args[0] for call in client.get_json.call_args_list], list(replies))
            client.current_analysis_ids.assert_called_once_with("ST000001")
            stored = (cache / "ST000001.native_context.json").read_text()
            for omitted in ("do-not-cache@example.org", "+1234567890", "do-not-cache-sample-123"):
                self.assertNotIn(omitted, stored)
            client.get_json.reset_mock()
            client.current_analysis_ids.reset_mock()
            second = jev.prepare_contexts(factor_path, sample_path, fetch_client=client, context_cache=cache, studies=["ST000001"])
            self.assertEqual(second, first)
            client.get_json.assert_not_called()
            client.current_analysis_ids.assert_not_called()

    def test_native_summary_recovery_rejects_an_unrequested_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            factor_path, sample_path, _ = self.fixtures(root)
            replies = {
                "study/study_id/ST000001/summary": [{"study_id": "ST000001", "study_title": "No narrative"}],
                "study/analysis_id/AN000001/mwtab": {
                    "METABOLOMICS WORKBENCH": {"STUDY_ID": "ST000001", "ANALYSIS_ID": "AN999999"},
                    "STUDY": {"STUDY_SUMMARY": "Foreign analysis narrative must not be used."},
                },
            }
            client = Mock()
            client.get_json.side_effect = replies.__getitem__
            client.current_analysis_ids.return_value = ["AN000001"]
            cache = root / "context_cache"
            with self.assertRaisesRegex(ValueError, "different analysis"):
                jev.prepare_contexts(factor_path, sample_path, fetch_client=client, context_cache=cache, studies=["ST000001"])
            self.assertFalse((cache / "ST000001.native_context.json").exists())


class PilotTests(unittest.TestCase):
    def test_pilot_is_reproducible_and_studies_do_not_cross_tune_test_split(self):
        contexts = []
        for study in range(1, 10):
            for name in ("Group", "Genotype", "Replicate"):
                contexts.append({"study_id": f"ST{study:06d}", "factor_name": name,
                                 "factor_values": ["WT", "KO"] if name == "Genotype" else ["1", "2"],
                                 "sample_evidence": {}, "study_context": []})
        chosen, labels = jev.make_pilot(contexts, size=18, seed=42)
        self.assertEqual(jev.make_pilot(contexts, size=18, seed=42), (chosen, labels))
        self.assertEqual(len(chosen), 18)
        self.assertEqual({(row["study_id"], row["factor_name"]) for row in labels},
                         {(row["study_id"], row["factor_name"]) for row in chosen})
        tune = {row["study_id"] for row in labels if row["split"] == "tune"}
        test = {row["study_id"] for row in labels if row["split"] == "test"}
        self.assertTrue(tune)
        self.assertTrue(test)
        self.assertFalse(tune & test)
        self.assertTrue(all(row["review_status"] == "pending" and row["expected_accession"] == "" for row in labels))


class SourcePreservationTests(unittest.TestCase):
    def test_changed_nested_local_evidence_cannot_reuse_prepared_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, protocol = root / "factors.csv", root / "protocol.json"
            source.write_text("original factor data", encoding="utf-8")
            protocol.write_text("original study protocol", encoding="utf-8")
            contexts = [{"study_id": "ST000001", "factor_name": "Group", "source": str(source),
                         "source_sha256": jev.file_hash(source), "study_context": [{"source": str(protocol),
                         "source_sha256": jev.file_hash(protocol), "content": "protocol text"}]}]
            jev.validate_source_files(contexts)
            protocol.write_text("changed study protocol", encoding="utf-8")
            with self.assertRaises(ValueError):
                jev.validate_source_files(contexts)

    def test_generated_output_paths_cannot_overwrite_inputs_even_through_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("mapping_decisions.csv", "accepted_mappings.csv", "factors_enriched.csv", "run_metadata.json"):
                protected = root / name
                protected.write_text("original source", encoding="utf-8")
                with self.subTest(name=name), self.assertRaises(ValueError):
                    jev.assert_output_paths([protected], root)
            alias = root / "source_alias.csv"
            alias.symlink_to(root / "factors_enriched.csv")
            with self.assertRaises(ValueError):
                jev.assert_output_paths([alias], root)
            original = root / "factors.csv"
            original.write_text("raw export", encoding="utf-8")
            jev.assert_output_paths([original], root)
            self.assertEqual(original.read_text(), "raw export")


if __name__ == "__main__":
    unittest.main()
