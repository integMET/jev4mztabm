"""Offline checks for Jev selection, response validation, and request caching."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

import workbench_jev as jev


ACCESSION = "http://www.ebi.ac.uk/efo/EFO_0000727"


def context():
    return {
        "study_id": "ST000001", "factor_name": "Treatment", "factor_values": ["control", "drug"],
        "sample_evidence": {}, "study_context": [],
    }


def catalog():
    return {
        "schema_version": 1, "catalog_id": "test-catalog", "terms": [{
            "ontology_accession": ACCESSION, "label": "treatment", "definition": "Experimental treatment",
            "synonyms": [], "ontology_source": "EFO", "ontology_version": "", "ontology_file": "",
            "is_obsolete": False, "status": "eligible", "metadata_url": "https://www.ebi.ac.uk/ols4/api/terms/test",
        }],
    }


def selection_response(request, choice=ACCESSION):
    choices = list(request["questions"]["accession"]["criteria"])
    return {
        "model": "jev-1.14.0", "usage": {"input_tokens": 120, "output_tokens": 12},
        "answers": {
            "accession": {
                "type": "choice", "choice": choice, "confidence": 0.99,
                "probabilities": {name: 0.97 if name == choice else 0.03 / (len(choices) - 1) for name in choices},
            },
            "eligible_match": {"type": "noul", "noul": 0.98},
            "single_dimension": {"type": "noul", "noul": 0.99},
        },
    }


def verification_response(probability=0.99):
    return {
        "model": "jev-1.14.0", "usage": {"input_tokens": 80, "output_tokens": 4},
        "answers": {"selected_term_supported": {"type": "noul", "noul": probability}},
    }


def calibrated_policy():
    return {
        "schema_version": 1, "calibrated": True, "model": "jev-1.14.0",
        "prompt_version": "workbench-factors-v1", "catalog_id": "test-catalog",
        "label_source_sha256": "a" * 64, "audit_source_sha256": "b" * 64,
        "thresholds": {
            "selected_probability": 0.95, "choice_confidence": 0.95, "top_two_margin": 0.95,
            "eligible_match_probability": 0.95, "single_dimension_probability": 0.95,
            "verification_probability": 0.95,
        },
        "diagnostics": {
            "target_precision": 0.98, "min_accepted": 1,
            "study_disjoint": True, "policy_selection_split": "tune", "heldout_evaluations": 1,
            "tune": {"accepted": 2, "correct": 2, "incorrect": 0, "precision": 1.0,
                     "eligible_rows": 3, "total_rows": 3, "study_count": 1},
            "test": {"accepted": 2, "correct": 2, "incorrect": 0, "precision": 1.0,
                     "eligible_rows": 3, "total_rows": 3, "study_count": 1},
        },
    }


class SnapshotResponse:
    """The SDK's Pydantic response seam, with no installed SDK dependency."""

    def __init__(self, payload, request_id="test-request-id"):
        self.payload = payload
        self.request_id = request_id

    def model_dump(self, *, mode):
        if mode != "json":
            raise AssertionError("SDK snapshots must use JSON-mode serialization")
        return copy.deepcopy(self.payload)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def system_one(self, **request):
        self.calls.append(copy.deepcopy(request))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class RequestAndValidationTests(unittest.TestCase):
    def test_selection_request_uses_full_iris_and_explicit_abstentions(self):
        request = jev.build_selection_request(context(), catalog())
        self.assertEqual(request["model"], "jev-1.14.0")
        self.assertEqual(set(request["questions"]), {"accession", "eligible_match", "single_dimension"})
        choice = request["questions"]["accession"]
        self.assertEqual(choice["type"], "choice")
        self.assertEqual(set(choice["criteria"]), {ACCESSION, "UNMAPPED", "AMBIGUOUS", "MIXED_FACTOR"})
        self.assertIn(ACCESSION, json.dumps(request["state"]))
        self.assertEqual(request["questions"]["eligible_match"]["type"], "noul")
        self.assertEqual(request["questions"]["single_dimension"]["type"], "noul")

    def test_native_snapshot_metrics_are_validated(self):
        request = jev.build_selection_request(context(), catalog())
        result = jev.validate_selection(selection_response(request), request)
        self.assertEqual(result["selected_accession"], ACCESSION)
        self.assertAlmostEqual(result["selected_probability"], 0.97)
        self.assertAlmostEqual(result["choice_confidence"], 0.99)
        self.assertAlmostEqual(result["top_two_margin"], 0.96)
        self.assertAlmostEqual(result["eligible_match_probability"], 0.98)
        self.assertAlmostEqual(result["single_dimension_probability"], 0.99)
        self.assertEqual(result["model"], "jev-1.14.0")
        self.assertAlmostEqual(jev.validate_verification(verification_response(), "jev-1.14.0"), 0.99)

    def test_invalid_probabilities_models_and_missing_answers_are_rejected(self):
        request = jev.build_selection_request(context(), catalog())
        valid = selection_response(request)
        cases = []
        invalid = copy.deepcopy(valid)
        invalid["model"] = "jev-latest"
        cases.append(invalid)
        invalid = copy.deepcopy(valid)
        invalid["answers"]["accession"]["probabilities"][ACCESSION] = float("nan")
        cases.append(invalid)
        invalid = copy.deepcopy(valid)
        invalid["answers"]["accession"]["confidence"] = 1.1
        cases.append(invalid)
        invalid = copy.deepcopy(valid)
        invalid["answers"]["single_dimension"]["noul"] = float("inf")
        cases.append(invalid)
        invalid = copy.deepcopy(valid)
        invalid["answers"]["accession"]["choice"] = "http://example.org/uncatalogued"
        cases.append(invalid)
        invalid = copy.deepcopy(valid)
        invalid["answers"]["accession"]["probabilities"].pop("UNMAPPED")
        cases.append(invalid)
        invalid = copy.deepcopy(valid)
        invalid["answers"].pop("eligible_match")
        cases.append(invalid)
        for response in cases:
            with self.subTest(response=response), self.assertRaises(ValueError):
                jev.validate_selection(response, request)

    def test_verification_is_a_separate_supported_term_question(self):
        request = jev.build_verification_request(context(), catalog()["terms"][0])
        self.assertEqual(set(request["questions"]), {"selected_term_supported"})
        self.assertEqual(request["questions"]["selected_term_supported"]["type"], "noul")
        self.assertIn(ACCESSION, json.dumps(request["state"]))
        for probability in (-0.1, 1.1, float("nan")):
            with self.subTest(probability=probability), self.assertRaises(ValueError):
                jev.validate_verification(verification_response(probability), "jev-1.14.0")

    def test_request_packing_preserves_all_695_values_and_distinct_position_evidence(self):
        original = context()
        values = [f"sample-source-{index:04d}" for index in range(695)]
        subset = values[:7]
        original.update(factor_name="Sample source", factor_values=values, sample_evidence={
            "positions": [{"factor_position": 1, "factor_values": list(values)},
                          {"factor_position": 2, "factor_values": list(subset)}],
            "sample_sources": list(values),
        })
        before = copy.deepcopy(original)
        requests = (jev.build_selection_request(original, catalog()),
                    jev.build_verification_request(original, catalog()["terms"][0]))
        for request in requests:
            packed = request["state"]["factor"]
            self.assertEqual(packed["factor_values"], values)
            self.assertEqual(len(packed["factor_values"]), 695)
            evidence = packed["sample_evidence"]
            self.assertTrue(evidence["sample_sources_same_as_row"])
            for packed_position, source_position in zip(evidence["positions"], before["sample_evidence"]["positions"]):
                recovered = packed["factor_values"] if packed_position.get("factor_values_same_as_row") else packed_position["factor_values"]
                self.assertEqual(recovered, source_position["factor_values"])
            self.assertTrue(evidence["positions"][0]["factor_values_same_as_row"])
            self.assertEqual(evidence["positions"][1]["factor_values"], subset)
            self.assertNotIn("factor_values_same_as_row", evidence["positions"][1])
            self.assertTrue(all("factor.factor_values" in question["instructions"] and "every value" in question["instructions"]
                                for question in request["questions"].values()))
        self.assertEqual(original, before)
        self.assertEqual(json.loads(jev.make_evidence(original))["context"], before)


class CachedRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_snapshot_is_serialized_and_identical_requests_reuse_cache(self):
        request = jev.build_selection_request(context(), catalog())
        snapshot = selection_response(request)
        client = FakeClient([SnapshotResponse(snapshot, "request-123")])
        with tempfile.TemporaryDirectory() as directory:
            first = await jev.cached_request(client, request, Path(directory))
            self.assertEqual(first["response"], snapshot)
            self.assertRegex(first["request_id"], r"^[0-9a-f]{64}$")
            self.assertFalse(first["cache_hit"])
            second = await jev.cached_request(client, copy.deepcopy(request), Path(directory))
            self.assertEqual(second["response"], snapshot)
            self.assertEqual(second["request_id"], first["request_id"])
            self.assertTrue(second["cache_hit"])
            self.assertEqual(len(client.calls), 1)

    async def test_changed_context_does_not_reuse_old_response(self):
        first_request = jev.build_selection_request(context(), catalog())
        changed = context()
        changed["factor_values"] = ["untreated", "treated"]
        second_request = jev.build_selection_request(changed, catalog())
        client = FakeClient([selection_response(first_request), selection_response(second_request)])
        with tempfile.TemporaryDirectory() as directory:
            await jev.cached_request(client, first_request, Path(directory))
            result = await jev.cached_request(client, second_request, Path(directory))
            self.assertFalse(result["cache_hit"])
            self.assertEqual(len(client.calls), 2)

    async def test_invalid_responses_and_tampered_cache_are_rejected(self):
        request = jev.build_selection_request(context(), catalog())
        invalid = selection_response(request)
        invalid["model"] = "jev-latest"
        client = FakeClient([invalid, selection_response(request)])
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            with self.assertRaises(ValueError):
                await jev.cached_request(client, request, cache_dir)
            self.assertEqual(list(cache_dir.glob("*.json")), [])
            await jev.cached_request(client, request, cache_dir)
            path = next(cache_dir.glob("*.json"))
            envelope = json.loads(path.read_text())
            envelope["request"]["state"]["factor"]["factor_name"] = "Altered cached context"
            path.write_text(json.dumps(envelope), encoding="utf-8")
            with self.assertRaises(ValueError):
                await jev.cached_request(client, request, cache_dir)
            self.assertEqual(len(client.calls), 2)

    async def test_payload_limits_fail_before_any_client_call(self):
        request = jev.build_selection_request(context(), catalog())
        client = FakeClient([])
        with tempfile.TemporaryDirectory() as directory:
            for limits in ({"max_question_bytes": 1}, {"max_request_bytes": 1}):
                with self.subTest(limits=limits), self.assertRaises(ValueError):
                    await jev.cached_request(client, request, Path(directory), **limits)
            self.assertEqual(client.calls, [])

    async def test_client_errors_are_not_cached(self):
        request = jev.build_selection_request(context(), catalog())
        client = FakeClient([RuntimeError("service unavailable"), selection_response(request)])
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            with self.assertRaises(RuntimeError):
                await jev.cached_request(client, request, cache_dir)
            self.assertEqual(list(cache_dir.glob("*.json")), [])
            result = await jev.cached_request(client, request, cache_dir)
            self.assertFalse(result["cache_hit"])
            self.assertEqual(len(client.calls), 2)


class CalibrationPolicyTests(unittest.TestCase):
    def load(self, policy):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps(policy), encoding="utf-8")
            return jev.load_policy(path, "jev-1.14.0", "test-catalog")

    def test_matching_calibrated_policy_with_all_six_thresholds_is_accepted(self):
        policy = calibrated_policy()
        self.assertEqual(self.load(policy), policy)

    def test_policy_must_match_model_prompt_catalog_and_validate_every_threshold(self):
        cases = []
        for field, value in (("calibrated", False), ("model", "jev-latest"),
                             ("prompt_version", "old-prompt"), ("catalog_id", "other-catalog")):
            invalid = calibrated_policy()
            invalid[field] = value
            cases.append(invalid)
        for metric in calibrated_policy()["thresholds"]:
            invalid = calibrated_policy()
            invalid["thresholds"].pop(metric)
            cases.append(invalid)
            for bad_value in (float("nan"), float("inf"), -0.01, 1.01, True):
                invalid = calibrated_policy()
                invalid["thresholds"][metric] = bad_value
                cases.append(invalid)
        for policy in cases:
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                self.load(policy)

    def test_both_calibration_splits_require_accepted_examples_and_target_precision(self):
        for split in ("tune", "test"):
            for report in ({"accepted": 0, "precision": 1.0}, {"accepted": 1, "precision": 0.97}):
                policy = calibrated_policy()
                policy["diagnostics"][split] = report
                with self.subTest(split=split, report=report), self.assertRaises(ValueError):
                    self.load(policy)

    def test_nonfinite_precision_cannot_make_a_policy_appear_calibrated(self):
        for split in ("tune", "test"):
            for invalid_precision in (float("nan"), float("inf")):
                policy = calibrated_policy()
                policy["diagnostics"][split]["precision"] = invalid_precision
                with self.subTest(split=split, precision=invalid_precision), self.assertRaises(ValueError):
                    self.load(policy)

    def test_invalid_calibration_provenance_and_inconsistent_counts_are_rejected(self):
        cases = []
        for field in ("label_source_sha256", "audit_source_sha256"):
            invalid = calibrated_policy()
            invalid[field] = "missing-source-hash"
            cases.append(invalid)
        for field, value in (("study_disjoint", False), ("policy_selection_split", "test"),
                             ("heldout_evaluations", 2), ("min_accepted", 0),
                             ("min_accepted", True), ("target_precision", float("nan")),
                             ("target_precision", float("inf")), ("target_precision", 0)):
            invalid = calibrated_policy()
            invalid["diagnostics"][field] = value
            cases.append(invalid)
        for split in ("tune", "test"):
            for field, value in (("accepted", True), ("accepted", float("nan")),
                                 ("correct", 1), ("incorrect", -1), ("precision", 0.99),
                                 ("eligible_rows", 1), ("total_rows", 1), ("study_count", 0)):
                invalid = calibrated_policy()
                invalid["diagnostics"][split][field] = value
                cases.append(invalid)
        for policy in cases:
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                self.load(policy)


class DecisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_high_scores_without_calibrated_policy_remain_review_proposals(self):
        request = jev.build_selection_request(context(), catalog())
        client = FakeClient([SnapshotResponse(selection_response(request)), SnapshotResponse(verification_response())])
        with tempfile.TemporaryDirectory() as directory:
            decision = await jev.decide_row(context(), catalog(), client, Path(directory))
            self.assertEqual(decision["status"], "needs_review")
            self.assertEqual(decision["selected_accession"], ACCESSION)
            self.assertEqual(decision["acceptance_method"], "")
            self.assertEqual(len(client.calls), 2)
            self.assertEqual(client.calls[1]["state"]["selected_term"]["ontology_accession"], ACCESSION)
            self.assertEqual(set(client.calls[1]["questions"]), {"selected_term_supported"})
            with self.assertRaises(ValueError):
                jev.mapping_from_decision(decision, catalog()["terms"][0])

    async def test_abstentions_preserve_mixed_or_uncertain_context_and_skip_verification(self):
        cases = (
            ("UNMAPPED", "unmapped", "Batch", ["batch1", "batch2"]),
            ("AMBIGUOUS", "ambiguous", "Replicate", ["1", "2", "3"]),
            ("MIXED_FACTOR", "mixed_factor", "Group", ["WT_vehicle", "GeneX-KO_drug"]),
        )
        for choice, status, name, values in cases:
            with self.subTest(choice=choice), tempfile.TemporaryDirectory() as directory:
                evidence = context()
                evidence.update(factor_name=name, factor_values=values)
                request = jev.build_selection_request(evidence, catalog())
                client = FakeClient([selection_response(request, choice)])
                decision = await jev.decide_row(evidence, catalog(), client, Path(directory))
                self.assertEqual(decision["status"], status)
                self.assertEqual(decision["selected_accession"], "")
                self.assertEqual(client.calls[0]["state"]["factor"]["factor_values"], values)
                self.assertEqual(len(client.calls), 1)
                with self.assertRaises(ValueError):
                    jev.mapping_from_decision(decision, catalog()["terms"][0])

    async def test_calibrated_acceptance_requires_every_one_of_six_metrics(self):
        request = jev.build_selection_request(context(), catalog())
        observed = {
            "selected_probability": 0.97, "choice_confidence": 0.99, "top_two_margin": 0.96,
            "eligible_match_probability": 0.98, "single_dimension_probability": 0.99,
            "verification_probability": 0.99,
        }
        for failing_metric in (None, *observed):
            with self.subTest(failing_metric=failing_metric), tempfile.TemporaryDirectory() as directory:
                policy = calibrated_policy()
                if failing_metric is not None:
                    policy["thresholds"][failing_metric] = observed[failing_metric] + 0.005
                client = FakeClient([selection_response(request), verification_response()])
                decision = await jev.decide_row(context(), catalog(), client, Path(directory), policy)
                self.assertEqual(decision["status"], "mapped" if failing_metric is None else "needs_review")
                if failing_metric is None:
                    mapping = jev.mapping_from_decision(decision, catalog()["terms"][0])
                    self.assertEqual(mapping["ontology_accession"], ACCESSION)
                    self.assertEqual(mapping["review_status"], "approved")
                    self.assertEqual(mapping["acceptance_method"], "automatic_calibrated_policy")

    async def test_bad_selection_or_verification_remains_error_and_cannot_become_mapping(self):
        request = jev.build_selection_request(context(), catalog())
        bad_selection = selection_response(request)
        bad_selection["model"] = "jev-latest"
        bad_verification = verification_response()
        bad_verification["model"] = "other-model"
        for responses in ([bad_selection], [selection_response(request), bad_verification]):
            with self.subTest(responses=responses), tempfile.TemporaryDirectory() as directory:
                decision = await jev.decide_row(context(), catalog(), FakeClient(responses), Path(directory))
                self.assertEqual(decision["status"], "error")
                self.assertTrue(decision["error"])
                with self.assertRaises(ValueError):
                    jev.mapping_from_decision(decision, catalog()["terms"][0])


if __name__ == "__main__":
    unittest.main()
