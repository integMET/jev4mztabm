"""Checks for source-preserving recovery from structured mwTab sample factors."""

import copy
import json
import unittest

from workbench_structured import (
    ADDITIONAL_PAIRS_MARKER,
    FACTOR_PAIRS_MARKER,
    decode_mwtab_json,
    merge_structured_factors,
)


STUDY = "ST003205"


def investigation(analysis_id, samples, study_id=STUDY):
    return {
        "METABOLOMICS WORKBENCH": {
            "STUDY_ID": study_id,
            "ANALYSIS_ID": analysis_id,
        },
        "SUBJECT_SAMPLE_FACTORS": samples,
    }


def native_sample(sample_id, factors):
    return {"Subject ID": "-", "Sample ID": sample_id, "Factors": factors}


def flat_sample(sample_id, factors="Treatment:A", mb_sample_id="SA000001"):
    return {
        "study_id": STUDY,
        "local_sample_id": sample_id,
        "mb_sample_id": mb_sample_id,
        "sample_source": "plasma",
        "factors": factors,
        "raw_data": "sample.mzML",
    }


def raw_investigation(factors_json, analysis_id="AN005257"):
    """Embed raw factor JSON so repeated object keys survive fixture creation."""
    metadata = json.dumps({"STUDY_ID": STUDY, "ANALYSIS_ID": analysis_id})
    return (
        '{"METABOLOMICS WORKBENCH":' + metadata
        + ',"SUBJECT_SAMPLE_FACTORS":[{"Sample ID":"S1","Factors":'
        + factors_json + '}]}'
    )


class LosslessDecoderTests(unittest.TestCase):
    def test_repeated_additional_fields_preserve_order_and_do_not_become_factors(self):
        raw = raw_investigation('{"Treatment":"A"}').replace(
            '"Factors":',
            '"Additional sample data":{"Dose":"1","Note":{"unit":"mM"},'
            '"Dose":"2","":"first","":"second"},"Factors":',
        )
        expected = [
            ["Dose", "1"], ["Note", {"unit": "mM"}], ["Dose", "2"],
            ["", "first"], ["", "second"],
        ]
        for source in (raw, "[" + raw + "]"):
            with self.subTest(investigation_list=source.startswith("[")):
                decoded = decode_mwtab_json(source)
                sample = (decoded[0] if isinstance(decoded, list) else decoded)["SUBJECT_SAMPLE_FACTORS"][0]
                additional = sample["Additional sample data"]
                self.assertEqual(additional[ADDITIONAL_PAIRS_MARKER], expected)
                self.assertEqual(additional["Dose"], "2")
                cached = json.loads(json.dumps(decoded))
                cached_sample = (cached[0] if isinstance(cached, list) else cached)["SUBJECT_SAMPLE_FACTORS"][0]
                self.assertEqual(cached_sample["Additional sample data"][ADDITIONAL_PAIRS_MARKER], expected)
                merged = merge_structured_factors(STUDY, [flat_sample("S1")], cached)
                self.assertEqual(merged[0]["_factor_pairs"], [("Treatment", "A")])

    def test_repeated_factor_and_additional_fields_use_distinct_markers(self):
        raw = raw_investigation('{"Factor":"Control","Factor":"post"}').replace(
            '"Factors":',
            '"Additional sample data":{"Age":"20","Age":"21"},"Factors":',
        )
        decoded = decode_mwtab_json(raw)
        sample = decoded["SUBJECT_SAMPLE_FACTORS"][0]
        self.assertEqual(sample["Additional sample data"][ADDITIONAL_PAIRS_MARKER], [["Age", "20"], ["Age", "21"]])
        self.assertEqual(sample["Factors"][FACTOR_PAIRS_MARKER], [["Factor", "Control"], ["Factor", "post"]])
        self.assertNotIn(FACTOR_PAIRS_MARKER, sample["Additional sample data"])
        self.assertNotIn(ADDITIONAL_PAIRS_MARKER, sample["Factors"])

    def test_duplicate_additional_fields_outside_exact_sample_path_are_rejected(self):
        raw = raw_investigation('{"Treatment":"A"}')
        sources = [
            '{"nested":' + raw.replace('"Factors":', '"Additional sample data":{"A":"1","A":"2"},"Factors":') + '}',
            raw.replace('"Factors":', '"Additional sample data":{"Nested":{"A":"1","A":"2"}},"Factors":'),
            raw.replace('"Factors":', '"Additional sample data":{},"Additional sample data":{},"Factors":'),
        ]
        for source in sources:
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "Duplicate JSON keys"):
                decode_mwtab_json(source)

    def test_repeated_native_factor_keys_preserve_every_ordered_value(self):
        raw = "[" + raw_investigation(
            '{"Replicate":"1","Treatment":"Control","Treatment":"0_day"}'
        ) + "]"
        expected = [("Replicate", "1"), ("Treatment", "Control"), ("Treatment", "0_day")]
        for source in (raw, raw.encode("utf-8")):
            with self.subTest(source_type=type(source).__name__):
                decoded = decode_mwtab_json(source)
                marked = decoded[0]["SUBJECT_SAMPLE_FACTORS"][0]["Factors"]
                self.assertEqual(marked[FACTOR_PAIRS_MARKER], [list(pair) for pair in expected])
                merged = merge_structured_factors(STUDY, [flat_sample("S1")], decoded)
                self.assertEqual(merged[0]["_factor_pairs"], expected)
                self.assertNotIn(FACTOR_PAIRS_MARKER, dict(merged[0]["_factor_pairs"]))

    def test_plain_cache_json_roundtrip_retains_duplicate_factor_values(self):
        decoded = decode_mwtab_json(raw_investigation('{"Factor":"Control","Factor":"post"}'))
        cached = json.loads(json.dumps(decoded))
        merged = merge_structured_factors(STUDY, [flat_sample("S1")], cached)
        self.assertEqual(merged[0]["_factor_pairs"], [("Factor", "Control"), ("Factor", "post")])

    def test_cross_analysis_merge_matches_occurrences_and_unions_new_ones(self):
        raw = "[" + ",".join([
            raw_investigation('{"Factor":"Control","Factor":"post"}', "AN005257"),
            raw_investigation(
                '{"Other":"X","Factor":"Control","Factor":"post","Factor":"later"}',
                "AN005258",
            ),
        ]) + "]"
        merged = merge_structured_factors(STUDY, [flat_sample("S1")], decode_mwtab_json(raw))
        self.assertEqual(merged[0]["_factor_pairs"], [
            ("Factor", "Control"), ("Factor", "post"), ("Other", "X"), ("Factor", "later"),
        ])
        self.assertEqual(merged[0]["_analysis_ids"], "AN005257;AN005258")

    def test_conflicting_value_in_a_repeated_occurrence_is_rejected(self):
        raw = "[" + ",".join([
            raw_investigation('{"Factor":"Control","Factor":"post"}', "AN005257"),
            raw_investigation('{"Factor":"Control","Factor":"pre"}', "AN005258"),
        ]) + "]"
        with self.assertRaisesRegex(ValueError, "Conflicting native values"):
            merge_structured_factors(STUDY, [flat_sample("S1")], decode_mwtab_json(raw))

    def test_identical_repeated_values_and_empty_names_keep_their_occurrences(self):
        decoded = decode_mwtab_json(raw_investigation('{"":"A","":"B","Factor":"X","Factor":"X"}'))
        merged = merge_structured_factors(STUDY, [flat_sample("S1")], decoded)
        self.assertEqual(merged[0]["_factor_pairs"], [("", "A"), ("", "B"), ("Factor", "X"), ("Factor", "X")])

    def test_duplicate_keys_outside_native_factors_are_rejected(self):
        sources = [
            '{"METABOLOMICS WORKBENCH":{"STUDY_ID":"ST003205","STUDY_ID":"ST003206"}}',
            '{"SUBJECT_SAMPLE_FACTORS":[{"Sample ID":"S1","Sample ID":"S2","Factors":{}}]}',
            '{"SUBJECT_SAMPLE_FACTORS":[],"SUBJECT_SAMPLE_FACTORS":[]}',
            '{"Additional sample data":{"A":"1","A":"2"}}',
            '{"Factors":{"A":"1","A":"2"}}',
        ]
        for raw in sources:
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "Duplicate JSON keys"):
                decode_mwtab_json(raw)

    def test_reserved_marker_collision_in_source_is_rejected(self):
        for marker in (FACTOR_PAIRS_MARKER, ADDITIONAL_PAIRS_MARKER):
            for raw in (
                json.dumps({marker: []}),
                raw_investigation(json.dumps({marker: "a real field"})),
                raw_investigation('{}').replace('"Factors":', '"Additional sample data":' + json.dumps({marker: []}) + ',"Factors":'),
            ):
                with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "reserved marker"):
                    decode_mwtab_json(raw)

    def test_normal_native_objects_do_not_gain_a_marker(self):
        raw = raw_investigation('{"Treatment":"A","Ethnicity":"Ju|Hoan"}')
        self.assertEqual(decode_mwtab_json(raw), json.loads(raw))

    def test_marked_cache_cannot_omit_an_ordinary_factor_field(self):
        decoded = decode_mwtab_json(raw_investigation('{"Factor":"Control","Factor":"post"}'))
        factors = decoded["SUBJECT_SAMPLE_FACTORS"][0]["Factors"]
        factors["Extra"] = "must not disappear"
        with self.assertRaisesRegex(ValueError, "marker does not match"):
            merge_structured_factors(STUDY, [flat_sample("S1")], decoded)


class StructuredFactorTests(unittest.TestCase):
    def test_missing_sample_source_is_recovered_without_rewriting_raw_reply(self):
        # The flat factors endpoint omits Sample source in this observed shape.
        raw = "Time point:After 1 Day post-infusion (1D) | | Therapy:axi-cell"
        flat = [flat_sample("CAR-33_+1D", raw)]
        native = [investigation("AN005257", [native_sample("CAR-33_+1D", {
            "Time point": "After 1 Day post-infusion (1D)",
            "Sample source": "plasma",
            "Therapy": "axi-cell",
        })])]
        before_flat, before_native = copy.deepcopy(flat), copy.deepcopy(native)

        merged = merge_structured_factors(STUDY, flat, native)

        self.assertEqual(merged[0]["_factor_pairs"], [
            ("Time point", "After 1 Day post-infusion (1D)"),
            ("Sample source", "plasma"),
            ("Therapy", "axi-cell"),
        ])
        self.assertEqual(merged[0]["_analysis_ids"], "AN005257")
        self.assertEqual({key: merged[0][key] for key in flat[0]}, flat[0])
        self.assertEqual(merged[0]["factors"], raw)
        self.assertIsNot(merged[0], flat[0])
        self.assertEqual(flat, before_flat)
        self.assertEqual(native, before_native)

    def test_native_only_samples_are_added_with_blank_unknown_metadata(self):
        native = investigation("AN005257", [
            native_sample("S1", {"Treatment": "A"}),
            native_sample("S2", {"Treatment": "B"}),
        ])

        merged = merge_structured_factors(STUDY, [flat_sample("S1")], native)

        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[1], {
            "study_id": STUDY,
            "local_sample_id": "S2",
            "mb_sample_id": "",
            "sample_source": "",
            "factors": "",
            "raw_factors": "",
            "_factor_pairs": [("Treatment", "B")],
            "_analysis_ids": "AN005257",
        })

    def test_reordered_cross_analysis_factors_agree_and_new_names_are_unioned(self):
        native = [
            investigation("AN005257", [native_sample("S1", {
                "Treatment": "A", "Sex": "F",
            })]),
            investigation("AN005258", [native_sample("S1", {
                "Sex": "F", "Treatment": "A", "Time point": "Day 1",
            })]),
            investigation("AN005258", [native_sample("S1", {"Treatment": "A"})]),
        ]

        merged = merge_structured_factors(STUDY, [flat_sample("S1")], native)

        self.assertEqual(dict(merged[0]["_factor_pairs"]), {
            "Treatment": "A", "Sex": "F", "Time point": "Day 1",
        })
        self.assertEqual(merged[0]["_analysis_ids"], "AN005257;AN005258")

    def test_conflicting_cross_analysis_values_are_rejected(self):
        native = [
            investigation("AN005257", [native_sample("S1", {"Treatment": "A"})]),
            investigation("AN005258", [native_sample("S1", {"Treatment": "B"})]),
        ]
        with self.assertRaisesRegex(ValueError, "Conflicting native values"):
            merge_structured_factors(STUDY, [flat_sample("S1")], native)

    def test_every_investigation_must_match_the_requested_study(self):
        native = [
            investigation("AN005257", [native_sample("S1", {"Treatment": "A"})]),
            investigation("AN005258", [], study_id="ST003206"),
        ]
        with self.assertRaisesRegex(ValueError, "received mwTab study"):
            merge_structured_factors(STUDY, [flat_sample("S1")], native)

    def test_partial_native_coverage_cannot_drop_known_flat_samples(self):
        flat = [flat_sample("S1"), flat_sample("S2", mb_sample_id="SA000002")]
        native = investigation("AN005257", [native_sample("S1", {"Treatment": "A"})])
        with self.assertRaisesRegex(ValueError, "does not cover 1 flat samples"):
            merge_structured_factors(STUDY, flat, native)

    def test_flat_duplicate_records_are_preserved_for_the_callers_deduplication(self):
        flat = [flat_sample("S1"), flat_sample("S1")]
        native = investigation("AN005257", [native_sample("S1", {"Treatment": "A"})])

        merged = merge_structured_factors(STUDY, flat, native)

        self.assertEqual(len(merged), len(flat))
        self.assertEqual(merged[0], merged[1])
        self.assertIsNot(merged[0], merged[1])
        self.assertIsNot(merged[0]["_factor_pairs"], merged[1]["_factor_pairs"])
        self.assertTrue(all("_factor_pairs" not in row for row in flat))

    def test_empty_native_data_is_valid_only_when_there_are_no_flat_samples(self):
        for payload in (None, [], investigation("AN005257", [])):
            with self.subTest(payload=payload):
                self.assertEqual(merge_structured_factors(STUDY, [], payload), [])
                with self.assertRaisesRegex(ValueError, "does not cover"):
                    merge_structured_factors(STUDY, [flat_sample("S1")], payload)

    def test_empty_factor_dictionary_still_covers_its_sample(self):
        native = investigation("AN005257", [native_sample("S1", {})])
        merged = merge_structured_factors(STUDY, [flat_sample("S1", "")], native)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["_factor_pairs"], [])

    def test_missing_factor_section_is_an_error_even_without_flat_samples(self):
        native = {"METABOLOMICS WORKBENCH": {"STUDY_ID": STUDY}}
        with self.assertRaisesRegex(ValueError, "missing SUBJECT_SAMPLE_FACTORS"):
            merge_structured_factors(STUDY, [], native)

    def test_native_names_and_literal_delimiters_are_preserved(self):
        factors = {"": "unnamed", "Treatment": "A", "treatment": "B", "Ethnicity": "Ju|Hoan"}
        native = investigation("AN005257", [native_sample("S1", factors)])
        merged = merge_structured_factors(STUDY, [flat_sample("S1")], native)
        self.assertEqual(merged[0]["_factor_pairs"], list(factors.items()))

    def test_json_scalars_are_text_and_null_is_blank(self):
        native = investigation("AN005257", [native_sample("S1", {
            "Missing": None, "Count": 2, "Dose": 1.5, "Present": True, "Absent": False,
        })])
        merged = merge_structured_factors(STUDY, [flat_sample("S1")], native)
        self.assertEqual(dict(merged[0]["_factor_pairs"]), {
            "Missing": "", "Count": "2", "Dose": "1.5", "Present": "true", "Absent": "false",
        })

    def test_nested_and_nonfinite_values_are_not_silently_stringified(self):
        for value in (["A"], {"label": "A"}, float("nan"), float("inf")):
            native = investigation("AN005257", [native_sample("S1", {"Treatment": value})])
            with self.subTest(value=value), self.assertRaises(ValueError):
                merge_structured_factors(STUDY, [flat_sample("S1")], native)


if __name__ == "__main__":
    unittest.main()
