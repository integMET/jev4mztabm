"""Offline checks for the MetaboLights factor export."""

import csv
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import metabolights_factors as factors


class StudyIdsTests(unittest.TestCase):
    def test_public_listing_uses_content_not_numeric_studies_count(self):
        payload = {"content": ["MTBLS3", "MTBLS1", "MTBLS1"], "studies": 3}
        self.assertEqual(factors.parse_study_ids(payload), ["MTBLS1", "MTBLS3"])

    def test_plain_list_is_supported(self):
        self.assertEqual(factors.parse_study_ids(["MTBLS2"]), ["MTBLS2"])

    def test_empty_listing_is_valid(self):
        self.assertEqual(factors.parse_study_ids({"content": [], "studies": 0}), [])

    def test_malformed_listing_is_not_silently_treated_as_empty(self):
        for payload in (None, {}, {"content": "MTBLS1"}, {"content": [None]}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                factors.parse_study_ids(payload)

    def test_invalid_accession_is_rejected(self):
        for accession in ("not-a-study", "MTBLS1/../../other", "MTBLS"):
            with self.subTest(accession=accession), self.assertRaises(ValueError):
                factors.parse_study_ids({"content": [accession]})

    def test_incomplete_listing_fails_advertised_count_check(self):
        with self.assertRaises(ValueError):
            factors.parse_study_ids({"content": ["MTBLS1"], "studies": 2})


class FactorRowsTests(unittest.TestCase):
    def test_ontology_fields_are_exported_without_changing_accession(self):
        payload = {
            "factors": [
                {
                    "factorName": "Metabolic syndrome",
                    "factorType": {
                        "annotationValue": "metabolic syndrome",
                        "termSource": {
                            "name": "EFO",
                            "version": "3.94.0",
                            "file": "http://www.ebi.ac.uk/efo/efo.owl",
                        },
                        "termAccession": "http://www.ebi.ac.uk/efo/EFO_0000195",
                    },
                }
            ]
        }
        self.assertEqual(
            list(factors.factor_rows("MTBLS1", payload)),
            [
                {
                    "study_id": "MTBLS1",
                    "factor_name": "Metabolic syndrome",
                    "factor_type": "metabolic syndrome",
                    "ontology_source": "EFO",
                    "ontology_accession": "http://www.ebi.ac.uk/efo/EFO_0000195",
                    "ontology_version": "3.94.0",
                    "ontology_file": "http://www.ebi.ac.uk/efo/efo.owl",
                }
            ],
        )

    def test_unannotated_factor_is_retained(self):
        payload = {"factors": [{"factorName": "Custom group", "factorType": {}}]}
        rows = list(factors.factor_rows("MTBLS2", payload))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["factor_name"], "Custom group")
        for field in (
            "factor_type",
            "ontology_source",
            "ontology_accession",
            "ontology_version",
            "ontology_file",
        ):
            with self.subTest(field=field):
                self.assertEqual(rows[0][field], "")

    def test_shared_factor_names_are_not_collapsed(self):
        payload = {
            "factors": [
                {"factorName": "Treatment", "factorType": {}},
                {"factorName": "Treatment", "factorType": {}},
            ]
        }
        self.assertEqual(len(list(factors.factor_rows("MTBLS3", payload))), 2)

    def test_no_factors_is_valid(self):
        self.assertEqual(list(factors.factor_rows("MTBLS1", {"factors": []})), [])

    def test_malformed_factor_response_is_not_silently_treated_as_empty(self):
        for payload in (None, {}, {"factors": {}}, {"factors": ["Treatment"]}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                list(factors.factor_rows("MTBLS1", payload))


class ApiClientTests(unittest.TestCase):
    @patch("metabolights_factors.time.sleep")
    @patch("metabolights_factors.urlopen")
    def test_rate_limit_is_retried_with_retry_after(self, urlopen, sleep):
        error = HTTPError("https://example.test", 429, "Too many requests", {"Retry-After": "7"}, None)
        urlopen.side_effect = [error, io.BytesIO(b'{"factors": []}')]
        self.assertEqual(factors.ApiClient(retries=1, interval=0).get_json("studies/MTBLS1/factors"), {"factors": []})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(7.0)

    @patch("metabolights_factors.time.sleep")
    @patch("metabolights_factors.urlopen")
    def test_server_failure_is_retried(self, urlopen, sleep):
        error = HTTPError("https://example.test", 503, "Unavailable", {}, None)
        urlopen.side_effect = [error, io.BytesIO(b'{"content": []}')]
        self.assertEqual(factors.ApiClient(retries=1, interval=0).get_json("studies"), {"content": []})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1)

    @patch("metabolights_factors.time.sleep")
    @patch("metabolights_factors.urlopen")
    def test_network_retry_budget_is_bounded(self, urlopen, sleep):
        urlopen.side_effect = URLError("offline")
        with self.assertRaises(URLError):
            factors.ApiClient(retries=2, interval=0).get_json("studies")
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    @patch("metabolights_factors.time.sleep")
    @patch("metabolights_factors.urlopen")
    def test_missing_or_private_study_is_not_retried(self, urlopen, sleep):
        for status in (401, 403, 404):
            with self.subTest(status=status):
                urlopen.reset_mock()
                sleep.reset_mock()
                urlopen.side_effect = HTTPError("https://example.test", status, "Unavailable", {}, None)
                with self.assertRaises(HTTPError):
                    factors.ApiClient(retries=3, interval=0).get_json("studies/MTBLS1/factors")
                urlopen.assert_called_once()
                sleep.assert_not_called()


class ExportTests(unittest.TestCase):
    def read_csv(self, path):
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def test_successful_and_empty_studies_are_cached_and_reused(self):
        client = Mock()
        client.get_json.side_effect = [
            {"factors": [{"factorName": "Treatment", "factorType": {}}]},
            {"factors": []},
        ]
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            output = Path(directory)
            self.assertEqual(factors.export_factors(["MTBLS1", "MTBLS2"], client, output), 0)
            statuses = self.read_csv(output / "study_status.csv")
            self.assertEqual([row["status"] for row in statuses], ["ok", "no_factors"])
            self.assertEqual([row["factor_count"] for row in statuses], ["1", "0"])
            client.get_json.reset_mock()
            self.assertEqual(factors.export_factors(["MTBLS1", "MTBLS2"], client, output), 0)
            client.get_json.assert_not_called()
            self.assertEqual(len(self.read_csv(output / "factors.csv")), 1)
            self.assertEqual([row["source"] for row in self.read_csv(output / "study_status.csv")], ["cache", "cache"])

    def test_failed_study_is_reported_and_later_studies_still_export(self):
        client = Mock()
        client.get_json.side_effect = [
            HTTPError("https://example.test", 404, "Not found", {}, None),
            {"factors": [{"factorName": "Treatment", "factorType": {}}]},
        ]
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            output = Path(directory)
            self.assertEqual(factors.export_factors(["MTBLS1", "MTBLS2"], client, output), 1)
            statuses = self.read_csv(output / "study_status.csv")
            self.assertEqual([row["status"] for row in statuses], ["error", "ok"])
            self.assertIn("404", statuses[0]["error"])
            self.assertFalse((output / ".cache" / "MTBLS1.json").exists())
            self.assertEqual(self.read_csv(output / "factors.csv")[0]["study_id"], "MTBLS2")

    def test_corrupt_cache_is_refetched(self):
        client = Mock()
        client.get_json.return_value = {"factors": []}
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            output = Path(directory)
            cache = output / ".cache"
            cache.mkdir()
            (cache / "MTBLS1.json").write_text('{"wrong_key": []}', encoding="utf-8")
            self.assertEqual(factors.export_factors(["MTBLS1"], client, output), 0)
            client.get_json.assert_called_once_with("studies/MTBLS1/factors")
            self.assertEqual(self.read_csv(output / "study_status.csv")[0]["source"], "api")

    def test_refresh_replaces_cached_factors(self):
        client = Mock()
        client.get_json.return_value = {"factors": [{"factorName": "New factor", "factorType": {}}]}
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            output = Path(directory)
            cache = output / ".cache"
            cache.mkdir()
            (cache / "MTBLS1.json").write_text('{"factors": []}', encoding="utf-8")
            self.assertEqual(factors.export_factors(["MTBLS1"], client, output, refresh=True), 0)
            client.get_json.assert_called_once_with("studies/MTBLS1/factors")
            self.assertEqual(self.read_csv(output / "factors.csv")[0]["factor_name"], "New factor")


if __name__ == "__main__":
    unittest.main()
