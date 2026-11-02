"""Deterministic checks for the fixed, source-verified ontology catalog."""

import copy
import csv
import hashlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import workbench_ontology_catalog as catalog


TREATMENT = "http://www.ebi.ac.uk/efo/EFO_0000727"
SEX = "http://www.ebi.ac.uk/efo/EFO_0000695"
MEDO = "http://www.ebi.ac.uk/efo/medo/MEDO_0000033"
ONTOLOGY = {
    "ontologyId": "efo", "version": "3.95.0",
    "loaded": "2026-11-02T00:08:55.191952015", "updated": "2026-11-02T00:08:55.191952015",
    "config": {"id": "efo", "version": "3.95.0", "preferredPrefix": "EFO",
               "versionIri": "http://www.ebi.ac.uk/efo/releases/v3.95.0/efo.owl",
               "fileLocation": "http://www.ebi.ac.uk/efo/efo.owl"},
}


def term_response(accession, **changes):
    term = {"iri": accession, "label": "treatment", "description": ["An intervention."],
            "synonyms": ["intervention"], "ontology_name": "efo", "is_obsolete": False}
    term.update(changes)
    return {"_embedded": {"terms": [term]}}


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "accessions.csv"
        self.cache = self.root / "cache"
        self.calls = []
        self.responses = {TREATMENT: term_response(TREATMENT),
                          SEX: term_response(SEX, label="obsolete_sex", is_obsolete=True)}
        self.ontology = copy.deepcopy(ONTOLOGY)

    def write_accessions(self, accessions):
        with self.source.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=("ontology_accession", "frequency"))
            writer.writeheader()
            writer.writerows({"ontology_accession": value, "frequency": 1} for value in accessions)

    def fetch(self, url):
        self.calls.append(url)
        if url == catalog.OLS_URL:
            return copy.deepcopy(self.ontology)
        accession = parse_qs(urlsplit(url).query)["iri"][0]
        value = self.responses[accession]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def build(self, accessions=(TREATMENT,), **options):
        self.write_accessions(accessions)
        return catalog.build_catalog(self.source, self.cache, fetch_json=self.fetch, **options)

    def test_canonical_metadata_and_versioned_file_define_eligible_candidate(self):
        self.responses[TREATMENT] = term_response(
            TREATMENT, label="canonical treatment", description=["First definition.", "Second definition."],
            synonyms=["therapy", "intervention", "therapy"],
        )
        result = self.build()
        term = result["terms"][0]
        self.assertEqual(term["status"], "eligible")
        self.assertEqual(term["label"], "canonical treatment")
        self.assertEqual(term["definition"], "First definition.\n\nSecond definition.")
        self.assertEqual(term["synonyms"], ["intervention", "therapy"])
        self.assertIs(term["is_obsolete"], False)
        self.assertEqual(term["ontology_source"], "EFO")
        self.assertEqual(term["ontology_version"], "3.95.0")
        self.assertEqual(term["ontology_file"], ONTOLOGY["config"]["versionIri"])
        self.assertEqual(parse_qs(urlsplit(term["metadata_url"]).query), {"iri": [TREATMENT]})
        self.assertEqual(result["source_sha256"], hashlib.sha256(self.source.read_bytes()).hexdigest())

    def test_obsolete_medo_and_invalid_accessions_are_retained_without_escalation(self):
        result = self.build((TREATMENT, SEX, MEDO, "not-an-accession"))
        terms = {term["ontology_accession"]: term for term in result["terms"]}
        self.assertEqual(terms[SEX]["status"], "obsolete")
        self.assertIs(terms[SEX]["is_obsolete"], True)
        self.assertEqual(terms[MEDO]["status"], "out_of_namespace")
        self.assertEqual(terms["not-an-accession"]["status"], "invalid")
        self.assertEqual(len(self.calls), 4)  # Two release checks plus two exact EFO lookups.
        self.assertEqual(terms[MEDO]["metadata_url"], "")

    def test_missing_or_nonboolean_obsolete_flag_never_becomes_eligible(self):
        for flag in (None, "false", 0):
            with self.subTest(flag=flag):
                self.responses[TREATMENT] = term_response(TREATMENT, is_obsolete=flag)
                result = self.build(refresh=True)
                self.assertEqual(result["terms"][0]["status"], "unresolved")

    def test_wrong_iri_empty_label_and_foreign_ontology_are_unresolved(self):
        for changes in ({"iri": SEX}, {"label": " "}, {"ontology_name": "other"}):
            with self.subTest(changes=changes):
                self.responses[TREATMENT] = term_response(TREATMENT, **changes)
                self.assertEqual(self.build(refresh=True)["terms"][0]["status"], "unresolved")

    def test_foreign_ontology_metadata_cannot_authorize_an_eligible_term(self):
        self.ontology["ontologyId"] = "other"
        term = self.build()["terms"][0]
        self.assertEqual(term["status"], "unresolved")
        self.assertEqual(term["label"], "treatment")
        self.assertIn("Ontology metadata", term["error"])

    def test_unknown_release_fields_stay_blank_instead_of_being_invented(self):
        self.ontology = {"ontologyId": "efo", "config": {"id": "efo"}}
        term = self.build()["terms"][0]
        self.assertEqual(term["ontology_version"], "")
        self.assertEqual(term["ontology_file"], "")

    def test_fetch_failure_is_an_explicit_unresolved_term(self):
        self.responses[TREATMENT] = OSError("network unavailable")
        term = self.build()["terms"][0]
        self.assertEqual(term["status"], "unresolved")
        self.assertEqual(term["error"], "network unavailable")

    def test_cache_reuse_refresh_and_timestamp_independent_catalog_hash(self):
        first = self.build()
        self.assertEqual(len(self.calls), 3)
        self.calls.clear()
        second = self.build()
        self.assertEqual(self.calls, [])
        self.assertEqual(first, second)
        refreshed = self.build(refresh=True)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(first["catalog_id"], refreshed["catalog_id"])

    def test_cache_envelope_for_a_different_request_is_never_reused(self):
        self.build()
        term_url = next(url for url in self.calls if url != catalog.OLS_URL)
        key = hashlib.sha256(term_url.encode()).hexdigest()
        path = self.cache / (key + ".json")
        envelope = json.loads(path.read_text())
        envelope["request_url"] = catalog.OLS_URL + "/terms?iri=wrong"
        envelope["payload"] = term_response(TREATMENT, label="poisoned cache label")
        path.write_text(json.dumps(envelope))
        self.calls.clear()
        result = self.build()
        self.assertEqual(self.calls, [catalog.OLS_URL, term_url, catalog.OLS_URL])
        self.assertEqual(result["terms"][0]["label"], "treatment")

    def test_source_order_does_not_change_the_term_catalog_identity(self):
        first = self.build((TREATMENT, SEX))
        second = self.build((SEX, TREATMENT))
        self.assertEqual(first["catalog_id"], second["catalog_id"])
        self.assertNotEqual(first["source_sha256"], second["source_sha256"])

    def test_new_ontology_snapshot_cannot_relabel_an_old_cached_term(self):
        old = self.build()
        self.ontology["version"] = self.ontology["config"]["version"] = "3.96.0"
        self.ontology["loaded"] = "2026-11-03T00:00:00"
        self.ontology["config"]["versionIri"] = "http://www.ebi.ac.uk/efo/releases/v3.96.0/efo.owl"
        # Simulate an interrupted refresh that replaced only global metadata.
        catalog._cached_json(catalog.OLS_URL, self.cache, True, self.fetch)
        self.responses[TREATMENT] = term_response(TREATMENT, label="new release label")
        self.calls.clear()

        rebuilt = self.build()

        self.assertNotEqual(old["catalog_id"], rebuilt["catalog_id"])
        self.assertEqual(rebuilt["terms"][0]["label"], "new release label")
        self.assertEqual(rebuilt["terms"][0]["ontology_version"], "3.96.0")
        self.assertEqual(len([url for url in self.calls if url != catalog.OLS_URL]), 1)

    def test_cached_only_build_preserves_its_verified_historical_snapshot(self):
        original = self.build()
        self.ontology["version"] = self.ontology["config"]["version"] = "3.96.0"
        self.calls.clear()
        rebuilt = self.build()
        self.assertEqual(self.calls, [])
        self.assertEqual(rebuilt, original)

    def test_release_change_during_fetch_does_not_commit_unverified_term_cache(self):
        self.write_accessions((TREATMENT,))
        ontology_calls = 0

        def changing_fetch(url):
            nonlocal ontology_calls
            if url == catalog.OLS_URL:
                ontology_calls += 1
                value = copy.deepcopy(self.ontology)
                if ontology_calls > 1:
                    value["version"] = value["config"]["version"] = "3.96.0"
                return value
            return self.fetch(url)

        with self.assertRaisesRegex(ValueError, "release changed"):
            catalog.build_catalog(self.source, self.cache, fetch_json=changing_fetch)
        self.assertFalse(catalog._cache_path(catalog._metadata_url(TREATMENT), self.cache).exists())

    def test_failed_final_release_check_does_not_commit_new_term_cache(self):
        self.write_accessions((TREATMENT,))
        ontology_calls = 0

        def interrupted_fetch(url):
            nonlocal ontology_calls
            if url == catalog.OLS_URL:
                ontology_calls += 1
                if ontology_calls > 1:
                    raise OSError("final release check unavailable")
            return self.fetch(url)

        with self.assertRaisesRegex(ValueError, "consistent OLS release"):
            catalog.build_catalog(self.source, self.cache, fetch_json=interrupted_fetch)
        self.assertFalse(catalog._cache_path(catalog._metadata_url(TREATMENT), self.cache).exists())

    def write_legacy_cache(self, term_age):
        now = datetime.now(timezone.utc)
        ontology_entry = {"schema_version": 1, "request_url": catalog.OLS_URL,
                          "fetched_at": (now - timedelta(seconds=10)).isoformat(), "payload": self.ontology}
        term_url = catalog._metadata_url(TREATMENT)
        term_entry = {"schema_version": 1, "request_url": term_url,
                      "fetched_at": (now - timedelta(seconds=term_age)).isoformat(),
                      "payload": term_response(TREATMENT, label="legacy label")}
        catalog._atomic_json(catalog._cache_path(catalog.OLS_URL, self.cache), ontology_entry)
        catalog._atomic_json(catalog._cache_path(term_url, self.cache), term_entry)

    def test_legacy_batch_inside_matching_release_checks_needs_no_term_refetch(self):
        self.write_legacy_cache(term_age=5)
        result = self.build()
        self.assertEqual(self.calls, [catalog.OLS_URL])
        self.assertEqual(result["terms"][0]["label"], "legacy label")
        entry = catalog._cache_entry(catalog._metadata_url(TREATMENT), self.cache,
                                     catalog._release_signature(self.ontology))
        self.assertIsNotNone(entry)
        self.assertIn("release_checked_at", entry)

    def test_legacy_reply_older_than_first_release_check_is_refetched(self):
        self.write_legacy_cache(term_age=60)
        result = self.build()
        self.assertEqual(self.calls, [catalog.OLS_URL, catalog._metadata_url(TREATMENT), catalog.OLS_URL])
        self.assertEqual(result["terms"][0]["label"], "treatment")

    def test_duplicate_source_accessions_and_missing_header_fail_before_fetch(self):
        self.write_accessions((TREATMENT, TREATMENT))
        with self.assertRaises(ValueError):
            catalog.build_catalog(self.source, self.cache, fetch_json=self.fetch)
        self.source.write_text("wrong_header\nvalue\n")
        with self.assertRaises(ValueError):
            catalog.build_catalog(self.source, self.cache, fetch_json=self.fetch)
        self.assertEqual(self.calls, [])

    def test_save_load_roundtrip_and_tampered_hash_rejection(self):
        result = self.build()
        path = self.root / "saved" / "catalog.json"
        catalog.save_catalog(path, result)
        self.assertEqual(catalog.load_catalog(path), result)
        edited = copy.deepcopy(result)
        edited["terms"][0]["label"] = "unchecked edited label"
        path.write_text(json.dumps(edited))
        with self.assertRaisesRegex(ValueError, "hash"):
            catalog.load_catalog(path)

    def test_rehashed_namespace_escalation_and_incomplete_eligible_metadata_fail(self):
        original = self.build((TREATMENT, MEDO))
        for field, value in (("label", ""), ("is_obsolete", None), ("is_obsolete", True),
                             ("ontology_source", ""), ("metadata_url", "https://example.org/term"),
                             ("status", "unrecognized")):
            edited = copy.deepcopy(original)
            term = next(term for term in edited["terms"] if term["ontology_accession"] == TREATMENT)
            term[field] = value
            edited["catalog_id"] = catalog._catalog_id(edited["terms"])
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                catalog.save_catalog(self.root / "bad.json", edited)
        edited = copy.deepcopy(original)
        next(term for term in edited["terms"] if term["ontology_accession"] == MEDO)["status"] = "eligible"
        edited["catalog_id"] = catalog._catalog_id(edited["terms"])
        with self.assertRaises(ValueError):
            catalog.save_catalog(self.root / "bad.json", edited)

    def test_duplicate_catalog_accessions_and_schema_changes_are_rejected(self):
        result = self.build()
        result["terms"].append(copy.deepcopy(result["terms"][0]))
        result["catalog_id"] = catalog._catalog_id(result["terms"])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            catalog.save_catalog(self.root / "bad.json", result)
        result = self.build()
        result["schema_version"] = True
        with self.assertRaisesRegex(ValueError, "schema"):
            catalog.save_catalog(self.root / "bad.json", result)

    def test_invalid_save_does_not_replace_an_existing_valid_catalog(self):
        result = self.build()
        path = self.root / "catalog.json"
        catalog.save_catalog(path, result)
        before = path.read_bytes()
        result["catalog_id"] = "0" * 64
        with self.assertRaises(ValueError):
            catalog.save_catalog(path, result)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.root.glob("*.tmp")), [])


class FetchRetryTests(unittest.TestCase):
    def test_transient_http_error_retries_with_backoff_without_real_network(self):
        error = HTTPError("https://example.org", 503, "unavailable", {}, io.BytesIO())
        with patch.object(catalog, "urlopen", side_effect=[error, io.BytesIO(b'{"ok":true}')]) as opening, \
                patch.object(catalog.time, "sleep") as sleep:
            self.assertEqual(catalog._fetch_json("https://example.org"), {"ok": True})
        self.assertEqual(opening.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_nontransient_http_error_is_not_retried(self):
        error = HTTPError("https://example.org", 404, "missing", {}, io.BytesIO())
        with patch.object(catalog, "urlopen", side_effect=error) as opening, \
                patch.object(catalog.time, "sleep") as sleep:
            with self.assertRaises(HTTPError):
                catalog._fetch_json("https://example.org")
        self.assertEqual(opening.call_count, 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
