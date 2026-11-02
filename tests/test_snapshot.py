"""Public artifact manifests and moved-tree Jev replay remain fully offline."""

import copy
import csv
import io
import json
import shutil
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from jev4mztabm import snapshot
from jev4mztabm import workbench_jev as jev
import test_workbench_jev_cli as fixtures


OUTPUTS = ("mapping_decisions.csv", "accepted_mappings.csv", "factors_enriched.csv", "run_metadata.json")


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return reader.fieldnames, list(reader)


class ManifestTests(unittest.TestCase):
    def setup_results(self, directory):
        root = Path(directory) / "results"
        root.mkdir()
        (root / "factors.csv").write_text("study_id,factor_name\nMTBLS1,Gender\n", encoding="utf-8")
        (root / ".cache").mkdir()
        (root / ".cache" / "MTBLS1.json").write_text('{"factors":[]}', encoding="utf-8")
        for name in (".env", "private-key.txt", "credentials.json", "notes.txt"):
            (root / name).write_text("synthetic excluded fixture; not a credential", encoding="utf-8")
        return root

    def test_manifest_only_includes_known_public_artifacts_and_explicit_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.setup_results(directory)
            manifest = snapshot.create_manifest(root)
            self.assertEqual([item["path"] for item in manifest["files"]], ["factors.csv"])
            self.assertTrue(snapshot.verify_manifest(root, manifest)["verified"])
            with_cache = snapshot.create_manifest(root, include_cache=True)
            self.assertEqual([item["path"] for item in with_cache["files"]], [".cache/MTBLS1.json", "factors.csv"])
            self.assertTrue(snapshot.verify_manifest(root, with_cache)["verified"])

    def test_hash_detects_same_size_changes_and_missing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.setup_results(directory)
            manifest = snapshot.create_manifest(root)
            path = root / "factors.csv"
            path.write_bytes(path.read_bytes().replace(b"Gender", b"gender"))
            result = snapshot.verify_manifest(root, manifest)
            self.assertFalse(result["verified"])
            self.assertEqual(result["failures"], [{"path": "factors.csv", "reason": "content changed"}])
            path.unlink()
            self.assertEqual(snapshot.verify_manifest(root, manifest)["failures"], [{"path": "factors.csv", "reason": "missing"}])

    def test_traversal_absolute_duplicate_and_invalid_hash_records_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.setup_results(directory)
            original = snapshot.create_manifest(root)
            for value in ("../outside.csv", "nested/../../outside.csv", str((root / "factors.csv").resolve())):
                changed = copy.deepcopy(original)
                changed["files"][0]["path"] = value
                with self.subTest(path=value), self.assertRaises(ValueError):
                    snapshot.verify_manifest(root, changed)
            for change in ("duplicate", "hash", "size"):
                changed = copy.deepcopy(original)
                if change == "duplicate":
                    changed["files"].append(dict(changed["files"][0]))
                elif change == "hash":
                    changed["files"][0]["sha256"] = "not-a-hash"
                else:
                    changed["files"][0]["size"] = True
                with self.subTest(change=change), self.assertRaises(ValueError):
                    snapshot.verify_manifest(root, changed)

    def test_symlink_outside_is_rejected_before_manifest_hashing_or_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.setup_results(directory)
            manifest = snapshot.create_manifest(root)
            outside = Path(directory) / "outside.csv"
            outside.write_text("synthetic outside fixture", encoding="utf-8")
            (root / "factors.csv").unlink()
            (root / "factors.csv").symlink_to(outside)
            with self.assertRaises(ValueError):
                snapshot.create_manifest(root)
            with self.assertRaises(ValueError):
                snapshot.verify_manifest(root, manifest)

    def test_known_artifact_cannot_alias_an_unlisted_root_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.setup_results(directory)
            (root / "factors.csv").unlink()
            (root / "factors.csv").symlink_to(root / "private-key.txt")
            with self.assertRaises(ValueError):
                snapshot.create_manifest(root)


class OfflineReplayTests(unittest.TestCase):
    def setup_recorded_run(self, original):
        results = original / "results"
        workbench = results / "workbench"
        run = workbench / "jev"
        workbench.mkdir(parents=True)
        run.mkdir()
        helper = fixtures.JevCliTests(methodName="runTest")
        catalog, catalog_path, source, _, rows = helper.fixtures(workbench)
        for number, name in ((3, "Selection unavailable"), (4, "Verification unavailable")):
            row = dict.fromkeys(fixtures.FIELDS, "")
            row.update(study_id=f"ST{number:06d}", factor_name=name, factor_value_unique='["Control", "Drug"]', note="HTTP 402 fixture")
            rows.append(row)
        jev.write_csv(source, fixtures.FIELDS, rows)
        contexts = jev.prepare_contexts(source)
        contexts_path = run / "contexts.jsonl"
        contexts_path.write_text("".join(jev.canonical_json(row) + "\n" for row in contexts), encoding="utf-8")
        shutil.copyfile(catalog_path, run / "ontology_target_catalog.json")
        calls = []
        sdk = fixtures.fake_sdk(calls)
        base_client = sdk.AsyncTypeSafeClient

        class TypeSafeAPIError(RuntimeError):
            pass

        class CreditLimitedClient(base_client):
            async def system_one(self, **request):
                name = request["state"]["factor"]["factor_name"]
                if (name == "Selection unavailable" or name == "Verification unavailable" and "accession" not in request["questions"]):
                    raise TypeSafeAPIError("POST https://api.typesafe.ai/v1/systemone: 402 Your organization has no available TypeSafe API credits. (request_id=req_synthetic)")
                return await super().system_one(**request)

        sdk.AsyncTypeSafeClient = CreditLimitedClient
        with patch.dict("sys.modules", {"typesafe_sdk": sdk}), \
                patch.object(jev.importlib.metadata, "version", return_value="0.7.5"), \
                patch.object(jev.sys, "version_info", (3, 12, 0)), \
                patch.object(jev.os, "environ", {"TYPESAFE_API_KEY": "offline-test-key"}), \
                patch("urllib.request.urlopen", side_effect=AssertionError("No network in fixture")), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = jev.main(["run", "--input", str(source), "--contexts", str(contexts_path),
                             "--catalog", str(run / "ontology_target_catalog.json"), "--output-dir", str(run)])
        self.assertEqual(code, 1, "Recorded HTTP 402 failures must remain errors")
        _, decisions = read_csv(run / "mapping_decisions.csv")
        self.assertEqual([row["status"] for row in decisions].count("error"), 2)
        self.assertEqual(len(list((run / ".cache/jev").glob("*.json"))), 5)
        return results, run

    def replay_without_clients(self, results, output):
        with ExitStack() as stack:
            stack.enter_context(patch.dict("sys.modules", {"typesafe_sdk": None}))
            stack.enter_context(patch.object(jev.os, "environ", {}))
            stack.enter_context(patch.object(jev.importlib.metadata, "version", side_effect=AssertionError("No SDK lookup in replay")))
            stack.enter_context(patch.object(jev, "run_inference", side_effect=AssertionError("No inference in replay")))
            stack.enter_context(patch.object(jev, "validate_source_files", side_effect=AssertionError("Do not consult original absolute paths")))
            stack.enter_context(patch("urllib.request.urlopen", side_effect=AssertionError("No network in replay")))
            return snapshot.replay_jev(results, output)

    def test_moved_snapshot_replays_all_four_outputs_exactly_and_keeps_http402_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            original = base / "original"
            results, run = self.setup_recorded_run(original)
            before = {name: snapshot.file_hash(run / name) for name in OUTPUTS}
            context_bytes = (run / "contexts.jsonl").read_bytes()
            moved = base / "moved"
            shutil.move(str(original), moved)
            self.assertFalse(original.exists())
            moved_results = moved / "results"
            self.assertEqual((moved_results / "workbench/jev/contexts.jsonl").read_bytes(), context_bytes)
            output = base / "replayed"
            report = self.replay_without_clients(moved_results, output)
            self.assertTrue(report["offline"])
            self.assertEqual(report["new_api_calls"], 0)
            self.assertEqual(report["rows_replayed"], 4)
            self.assertEqual(report["status_counts"], {"needs_review": 2, "error": 2})
            self.assertEqual({name: snapshot.file_hash(output / name) for name in OUTPUTS}, before)
            _, replayed = read_csv(output / "mapping_decisions.csv")
            self.assertTrue(all("402" in row["error"] for row in replayed if row["status"] == "error"))

    def test_cached_metric_source_hash_catalog_and_accepted_mapping_tampering_are_rejected(self):
        for change in ("cached_metric", "audit_metric", "source_hash", "raw_source", "catalog", "accepted"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                results, run = self.setup_recorded_run(base / "original")
                fields, decisions = read_csv(run / "mapping_decisions.csv")
                if change == "cached_metric":
                    path = run / ".cache/jev" / (decisions[0]["request_id"] + ".json")
                    cached = json.loads(path.read_text())
                    cached["response"]["answers"]["accession"]["confidence"] = 0.5
                    path.write_text(json.dumps(cached), encoding="utf-8")
                elif change == "audit_metric":
                    decisions[0]["selected_probability"] = "0.5"
                    jev.write_csv(run / "mapping_decisions.csv", fields, decisions)
                elif change == "source_hash":
                    path = run / "run_metadata.json"
                    metadata = json.loads(path.read_text()); metadata["input_sha256"] = "0" * 64
                    path.write_text(json.dumps(metadata), encoding="utf-8")
                elif change == "raw_source":
                    path = results / "workbench/factors.csv"
                    path.write_text(path.read_text().replace("Keep raw metadata", "Changed metadata"), encoding="utf-8")
                elif change == "catalog":
                    path = run / "ontology_target_catalog.json"
                    catalog = json.loads(path.read_text()); catalog["terms"][0]["label"] = "tampered"
                    path.write_text(json.dumps(catalog), encoding="utf-8")
                else:
                    columns, accepted = read_csv(run / "accepted_mappings.csv")
                    accepted = dict.fromkeys(columns, "")
                    accepted.update(study_id="ST000001", factor_name="Treatment", review_status="approved")
                    jev.write_csv(run / "accepted_mappings.csv", columns, [accepted])
                with self.assertRaises(ValueError):
                    self.replay_without_clients(results, base / "rejected")
                self.assertFalse((base / "rejected/mapping_decisions.csv").exists())

    def test_replay_rejects_cached_response_symlink_outside_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            results, run = self.setup_recorded_run(base / "original")
            _, decisions = read_csv(run / "mapping_decisions.csv")
            path = run / ".cache/jev" / (decisions[0]["request_id"] + ".json")
            outside = base / "outside-response.json"
            path.replace(outside)
            path.symlink_to(outside)
            with self.assertRaises(ValueError):
                self.replay_without_clients(results, base / "rejected")


if __name__ == "__main__":
    unittest.main()
