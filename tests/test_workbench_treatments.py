"""Offline treatment metadata parsing, complete CSV exports and cache checks."""

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse

from jev4mztabm import workbench_treatments as treatments


def treatment_page(study_id="ST000001", treatment_id="TR000003", summary="A treatment."):
    rows = []
    if treatment_id is not None:
        rows.append(f"<tr><td><b>Treatment ID:</b></td><td>{treatment_id}</td></tr>")
    if summary is not None:
        rows.append(f"<tr><td><b>Treatment Summary:</b></td><td>{summary}</td></tr>")
    return (
        f"<html><body><h1>Summary of Study {study_id}</h1>"
        f"<table><tr><td>Study ID</td><td>{study_id}</td></tr></table>"
        "<p><b>Treatment:</b></p><table class='datatable2'>"
        + "".join(rows) + "</table></body></html>"
    )


def multiple_treatment_page(records, study_id="ST002097", spacer=True):
    groups = []
    for treatment_id, summary in records:
        cells = []
        if treatment_id is not None:
            cells.append(f"<tr><td><b>Treatment ID:</b></td><td>{treatment_id}</td></tr>")
        if summary is not None:
            cells.append(f"<tr><td><b>Treatment Summary:</b></td><td>{summary}</td></tr>")
        groups.append("".join(cells))
    separator = "<tr><td>&nbsp;</td><td>&nbsp;</td></tr>" if spacer else ""
    page = treatment_page(study_id, treatment_id=None, summary=None)
    return page.replace("<table class='datatable2'>", "<table class='datatable2'>" + separator.join(groups))


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def fake_client(pages, listing=None):
    client = Mock()

    def get_text(url):
        study_id = parse_qs(urlparse(url).query)["StudyID"][0]
        value = pages[study_id]
        if isinstance(value, Exception):
            raise value
        return value

    client.get_text.side_effect = get_text
    client.get_json.return_value = listing
    return client


class TreatmentParsingTests(unittest.TestCase):
    def test_url_targets_the_official_treatment_metadata_tab(self):
        url = urlparse(treatments.treatment_url("ST002115"))
        self.assertEqual(url.scheme, "https")
        self.assertEqual(url.netloc, "www.metabolomicsworkbench.org")
        self.assertEqual(url.path, "/data/DRCCMetadata.php")
        query = parse_qs(url.query)
        self.assertEqual(query["StudyID"], ["ST002115"])
        self.assertEqual(query["DataMode"], ["TreatmentData"])
        self.assertEqual(query["Mode"], ["Study"])

    def test_deposited_treatment_id_is_not_derived_from_the_study_accession(self):
        parsed = treatments.parse_treatment(
            "ST002115", treatment_page("ST002115", "TR002243", "Cells were treated for 2 hours.")
        )
        self.assertEqual(parsed, {
            "study_id": "ST002115", "treatment_id": "TR002243",
            "treatment_summary": "Cells were treated for 2 hours.",
        })

    def test_nested_markup_entities_unicode_and_explicit_line_breaks_preserve_text(self):
        page = treatment_page(summary='  β cells &amp; <em>drug</em>, "dose".\n <br>Second&nbsp;line.')
        parsed = treatments.parse_treatment("ST000001", page)
        self.assertEqual(parsed["treatment_summary"], 'β cells & drug, "dose".\nSecond line.')

    def test_missing_summary_is_a_valid_blank_value(self):
        parsed = treatments.parse_treatment("ST000001", treatment_page(summary=None))
        self.assertEqual(parsed["treatment_id"], "TR000003")
        self.assertEqual(parsed["treatment_summary"], "")

    def test_missing_treatment_id_does_not_invent_an_accession(self):
        parsed = treatments.parse_treatment("ST000001", treatment_page(treatment_id=None))
        self.assertEqual(parsed["treatment_id"], "")
        self.assertEqual(parsed["treatment_summary"], "A treatment.")

    def test_empty_treatment_section_keeps_the_study(self):
        parsed = treatments.parse_treatment("ST000001", treatment_page(treatment_id=None, summary=None))
        self.assertEqual(parsed, {
            "study_id": "ST000001", "treatment_id": "", "treatment_summary": "",
        })

    def test_truncated_treatment_section_is_not_reported_as_missing_metadata(self):
        page = treatment_page()
        incomplete_pages = (
            page.split("<table class='datatable2'>", 1)[0],
            page.split("<tr><td><b>Treatment Summary:", 1)[0],
        )
        for incomplete in incomplete_pages:
            with self.subTest(incomplete=incomplete), self.assertRaises(ValueError):
                treatments.parse_treatment("ST000001", incomplete)

    def test_treatment_summary_rows_outside_the_treatment_table_are_ignored(self):
        unrelated = "<table><tr><td>Treatment Summary:</td><td>Unrelated summary.</td></tr></table>"
        for summary in ("Actual treatment.", None):
            page = treatment_page(summary=summary)
            page = page.replace("<p><b>Treatment:</b></p>", unrelated + "<p><b>Treatment:</b></p>")
            page = page.replace("</body>", unrelated + "</body>")
            with self.subTest(summary=summary):
                parsed = treatments.parse_treatment("ST000001", page)
                self.assertEqual(parsed["treatment_summary"], summary or "")

    def test_deposited_literal_summaries_are_preserved(self):
        for summary in ("NA", "N/A", "blinded", "None", "-", "No specific treatment to mention."):
            with self.subTest(summary=summary):
                parsed = treatments.parse_treatment("ST000001", treatment_page(summary=summary))
                self.assertEqual(parsed["treatment_summary"], summary)

    def test_error_and_foreign_pages_are_rejected_even_when_they_return_http_200(self):
        pages = (
            "<html><h1>Sign in</h1><p>Unavailable</p></html>",
            "<h1>Summary of study ST000001</h1>No data available...<br/>",
            treatment_page("ST000002"),
            treatment_page().replace("<p><b>Treatment:</b></p>", ""),
        )
        for page in pages:
            with self.subTest(page=page), self.assertRaises(ValueError):
                treatments.parse_treatment("ST000001", page)

    def test_invalid_accessions_are_rejected(self):
        for study_id in ("ST1", "ST000001/other", "MTBLS1", "ST00000X"):
            with self.subTest(study_id=study_id), self.assertRaises(ValueError):
                treatments.treatment_url(study_id)
            with self.subTest(study_id=study_id), self.assertRaises(ValueError):
                treatments.parse_treatment(study_id, treatment_page())
        for treatment_id in ("TR1", "ST000003", "TR000003/other"):
            with self.subTest(treatment_id=treatment_id), self.assertRaises(ValueError):
                treatments.parse_treatment("ST000001", treatment_page(treatment_id=treatment_id))

    def test_conflicting_duplicate_fields_are_rejected(self):
        page = treatment_page().replace(
            "</table></body>",
            "<tr><td>Treatment ID:</td><td>TR000004</td></tr></table></body>",
        )
        with self.assertRaises(ValueError):
            treatments.parse_treatment("ST000001", page)


class TreatmentExportTests(unittest.TestCase):
    def test_csv_round_trip_preserves_quoted_unicode_multiline_summary(self):
        summary = 'β &amp; γ, "dose"<br>Follow-up: 2 hours.'
        client = fake_client({"ST000001": treatment_page(summary=summary)})
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            output = Path(directory)
            self.assertEqual(treatments.export_treatments(["ST000001"], client, output), 0)
            rows = read_csv(output / "treatments.csv")
            self.assertEqual(rows, [{
                "study_id": "ST000001", "treatment_id": "TR000003",
                "treatment_summary": 'β & γ, "dose"\nFollow-up: 2 hours.',
            }])
            self.assertEqual(read_csv(output / "treatment_status.csv")[0]["status"], "ok")

    def test_partial_failures_and_missing_fields_keep_one_row_for_every_study(self):
        ids = [f"ST{number:06}" for number in range(1, 7)]
        client = fake_client({
            ids[0]: treatment_page(ids[0]),
            ids[1]: treatment_page(ids[1], summary=None),
            ids[2]: treatment_page(ids[2], treatment_id=None),
            ids[3]: treatment_page(ids[3], treatment_id=None, summary=None),
            ids[4]: URLError("temporary outage"),
            ids[5]: "<html><h1>Server error</h1></html>",
        })
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            output = Path(directory)
            failures = treatments.export_treatments(ids, client, output, workers=2)
            self.assertEqual(failures, 2)
            rows = read_csv(output / "treatments.csv")
            statuses = read_csv(output / "treatment_status.csv")
            self.assertEqual([row["study_id"] for row in rows], ids)
            self.assertEqual([row["study_id"] for row in statuses], ids)
            self.assertEqual([row["status"] for row in statuses], [
                "ok", "missing_summary", "missing_treatment_id", "no_treatment", "fetch_error", "parse_error",
            ])
            self.assertEqual([row["treatment_count"] for row in statuses], ["1", "1", "1", "0", "0", "0"])
            for row in rows[-2:]:
                self.assertEqual(row["treatment_id"], "")
                self.assertEqual(row["treatment_summary"], "")
            for row in statuses[-2:]:
                self.assertTrue(row["error"])

    def test_valid_cache_is_reused_and_refresh_refetches(self):
        client = fake_client({"ST000001": treatment_page(summary="Original.")})
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            output = Path(directory)
            self.assertEqual(treatments.export_treatments(["ST000001"], client, output), 0)
            self.assertTrue((output / ".cache/treatments/ST000001.html").is_file())
            client.get_text.side_effect = AssertionError("Valid cache must avoid network")
            self.assertEqual(treatments.export_treatments(["ST000001"], client, output), 0)
            self.assertEqual(read_csv(output / "treatments.csv")[0]["treatment_summary"], "Original.")
            client.get_text.side_effect = None
            client.get_text.return_value = treatment_page(summary="Updated.")
            self.assertEqual(treatments.export_treatments(["ST000001"], client, output, refresh=True), 0)
            self.assertEqual(read_csv(output / "treatments.csv")[0]["treatment_summary"], "Updated.")

    def test_invalid_or_foreign_cache_is_refetched(self):
        truncated = treatment_page().split("<tr><td><b>Treatment Summary:", 1)[0]
        for cached in ("not HTML", treatment_page("ST000002"), truncated):
            with self.subTest(cached=cached), tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
                output = Path(directory)
                path = output / ".cache/treatments/ST000001.html"
                path.parent.mkdir(parents=True)
                path.write_text(cached, encoding="utf-8")
                client = fake_client({"ST000001": treatment_page(summary="Valid replacement.")})
                self.assertEqual(treatments.export_treatments(["ST000001"], client, output), 0)
                self.assertEqual(client.get_text.call_count, 1)
                self.assertEqual(read_csv(output / "treatments.csv")[0]["treatment_summary"], "Valid replacement.")


class MultipleTreatmentTests(unittest.TestCase):
    def test_distinct_treatment_ids_preserve_their_associated_summaries(self):
        # ST002097 has these two deposited IDs in one Treatment table.
        records = [("TR002193", "Cell-line infection protocol."), ("TR002194", "Mouse acclimatization protocol.")]
        expected = [
            {"study_id": "ST002097", "treatment_id": treatment_id, "treatment_summary": summary}
            for treatment_id, summary in records
        ]
        for spacer in (True, False):
            with self.subTest(spacer=spacer):
                page = multiple_treatment_page(records, spacer=spacer)
                self.assertEqual(treatments.parse_treatments("ST002097", page), expected)
                with self.assertRaises(ValueError):
                    treatments.parse_treatment("ST002097", page)

    def test_duplicate_treatment_ids_with_identical_summaries_are_deduplicated(self):
        record = ("TR002193", "Cell-line infection protocol.")
        page = multiple_treatment_page([record, record])
        self.assertEqual(treatments.parse_treatments("ST002097", page), [{
            "study_id": "ST002097", "treatment_id": record[0], "treatment_summary": record[1],
        }])

    def test_conflicting_summaries_for_the_same_treatment_id_are_rejected(self):
        page = multiple_treatment_page([("TR002193", "Original summary."), ("TR002193", "Different summary.")])
        with self.assertRaises(ValueError):
            treatments.parse_treatments("ST002097", page)

    def test_blank_spacer_separates_a_later_record_without_treatment_id(self):
        page = multiple_treatment_page([("TR002193", "First protocol."), (None, "Second protocol.")])
        self.assertEqual(treatments.parse_treatments("ST002097", page), [
            {"study_id": "ST002097", "treatment_id": "TR002193", "treatment_summary": "First protocol."},
            {"study_id": "ST002097", "treatment_id": "", "treatment_summary": "Second protocol."},
        ])

    def test_export_writes_all_treatments_and_one_status_row_per_study(self):
        records = [("TR002193", "Cell-line infection protocol."), ("TR002194", "Mouse acclimatization protocol.")]
        client = fake_client({
            "ST002097": multiple_treatment_page(records),
            "ST002115": treatment_page("ST002115", "TR002243", "Single treatment protocol."),
        })
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            output = Path(directory)
            failures = treatments.export_treatments(["ST002097", "ST002115"], client, output, workers=2)
            self.assertEqual(failures, 0)
            self.assertEqual(read_csv(output / "treatments.csv"), [
                {"study_id": "ST002097", "treatment_id": records[0][0], "treatment_summary": records[0][1]},
                {"study_id": "ST002097", "treatment_id": records[1][0], "treatment_summary": records[1][1]},
                {"study_id": "ST002115", "treatment_id": "TR002243", "treatment_summary": "Single treatment protocol."},
            ])
            statuses = read_csv(output / "treatment_status.csv")
            self.assertEqual([row["study_id"] for row in statuses], ["ST002097", "ST002115"])
            self.assertEqual([row["status"] for row in statuses], ["ok", "ok"])
            self.assertEqual([row["treatment_count"] for row in statuses], ["2", "1"])


class TreatmentCommandTests(unittest.TestCase):
    def test_default_discovery_deduplicates_all_public_studies_across_analyses(self):
        listing = {
            "1": {"study_id": "ST000002", "analysis_id": "AN000003"},
            "2": {"study_id": "ST000001", "analysis_id": "AN000001"},
            "3": {"study_id": "ST000001", "analysis_id": "AN000002"},
        }
        client = fake_client({
            "ST000001": treatment_page("ST000001"),
            "ST000002": treatment_page("ST000002", "TR000006", summary=None),
        }, listing)
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()), patch.object(
            treatments, "WorkbenchClient", return_value=client
        ):
            self.assertEqual(treatments.main(["--output-dir", directory, "--workers", "1"]), 0)
            output = Path(directory)
            self.assertEqual([row["study_id"] for row in read_csv(output / "treatments.csv")], ["ST000001", "ST000002"])
            client.get_json.assert_called_once_with("study/study_id/ST/available")
            metadata = json.loads((output / "run_metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["study_ids"], ["ST000001", "ST000002"])
            self.assertEqual(metadata["study_count"], 2)
            self.assertEqual(metadata["failed_studies"], 0)

    def test_explicit_studies_skip_discovery_and_limit_is_applied_after_deduplication(self):
        client = fake_client({"ST000001": treatment_page("ST000001")})
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()), patch.object(
            treatments, "WorkbenchClient", return_value=client
        ):
            self.assertEqual(treatments.main([
                "--studies", "ST000002", "ST000001", "ST000001", "--limit", "1",
                "--output-dir", directory,
            ]), 0)
            client.get_json.assert_not_called()
            self.assertEqual([row["study_id"] for row in read_csv(Path(directory) / "treatments.csv")], ["ST000001"])

    def test_partial_failure_returns_nonzero_and_records_failure_count(self):
        client = fake_client({"ST000001": URLError("unreachable")})
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()), patch.object(
            treatments, "WorkbenchClient", return_value=client
        ):
            self.assertEqual(treatments.main(["--studies", "ST000001", "--output-dir", directory]), 1)
            metadata = json.loads((Path(directory) / "run_metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["failed_studies"], 1)

    def test_failed_discovery_does_not_produce_a_misleading_empty_export(self):
        client = Mock()
        client.get_json.side_effect = URLError("unreachable")
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()), patch.object(
            treatments, "WorkbenchClient", return_value=client
        ):
            self.assertEqual(treatments.main(["--output-dir", directory]), 2)
            self.assertFalse((Path(directory) / "treatments.csv").exists())

    def test_invalid_request_settings_are_rejected_before_network_access(self):
        for option, value in (("--workers", "0"), ("--interval", "-1"), ("--timeout", "0"), ("--retries", "-1"), ("--limit", "0")):
            with self.subTest(option=option), redirect_stderr(io.StringIO()), patch.object(
                treatments, "WorkbenchClient"
            ) as client, self.assertRaises(SystemExit):
                treatments.main([option, value])
            client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
