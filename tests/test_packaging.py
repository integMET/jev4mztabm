"""Import compatibility and optional offline installed-wheel smoke checks.

Set JEV4MZTABM_TEST_WHEEL to a built wheel and run with Python 3.10+ to
exercise installation in a fresh environment from outside the checkout.
The ordinary unit suite never downloads build tools or runtime dependencies.
"""

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import venv
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
MODULES = (
    "metabolights_factors", "analyze_ontology_accessions", "workbench_factors",
    "workbench_mapping", "workbench_structured", "workbench_ontology_catalog",
    "workbench_jev", "calibrate_workbench_jev",
)
ENTRY_POINTS = (
    "jev4mztabm", "metabolights-factors", "workbench-factors",
    "ontology-accessions", "workbench-jev", "calibrate-workbench-jev",
)


def offline_environment():
    environment = dict(os.environ)
    environment.pop("TYPESAFE_API_KEY", None)
    environment.pop("PYTHONPATH", None)
    environment.update(PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1",
                       PIP_NO_INDEX="1", UV_OFFLINE="1", UV_PYTHON_DOWNLOADS="never")
    return environment


class SourcePackagingTests(unittest.TestCase):
    def test_legacy_wrapper_imports_alias_the_qualified_modules(self):
        for name in MODULES:
            with self.subTest(module=name):
                legacy = importlib.import_module(name)
                qualified = importlib.import_module("jev4mztabm." + name)
                self.assertIs(legacy, qualified)

    def test_source_package_imports_without_api_keys_network_or_optional_dependencies(self):
        script = textwrap.dedent("""
            import builtins, importlib, os, pathlib, sys, urllib.request
            sys.path.insert(0, sys.argv[1])
            blocked = {'typesafe_sdk', 'matplotlib', 'pydantic', 'httpx2'}
            original_import = builtins.__import__
            def guarded_import(name, *args, **kwargs):
                if name.split('.')[0] in blocked:
                    raise AssertionError('Optional dependency imported eagerly: ' + name)
                return original_import(name, *args, **kwargs)
            builtins.__import__ = guarded_import
            def no_network(*args, **kwargs):
                raise AssertionError('Package import attempted a network request')
            urllib.request.urlopen = no_network
            before = set(pathlib.Path.cwd().iterdir())
            for name in sys.argv[2:]:
                importlib.import_module('jev4mztabm.' + name)
            assert 'TYPESAFE_API_KEY' not in os.environ
            assert not (blocked & set(sys.modules))
            assert set(pathlib.Path.cwd().iterdir()) == before
        """)
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-I", "-c", script, str(REPOSITORY / "src"), *MODULES],
                                    cwd=directory, env=offline_environment(), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


@unittest.skipUnless(os.environ.get("JEV4MZTABM_TEST_WHEEL"), "Set JEV4MZTABM_TEST_WHEEL for offline installed-wheel checks")
@unittest.skipIf(sys.version_info < (3, 10), "The wheel supports Python 3.10+")
class InstalledWheelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        wheel = Path(os.environ["JEV4MZTABM_TEST_WHEEL"]).resolve()
        if not wheel.is_file():
            raise AssertionError("JEV4MZTABM_TEST_WHEEL must identify a built wheel")
        temporary = tempfile.TemporaryDirectory(prefix="jev4mztabm-wheel-test-")
        cls.addClassCleanup(temporary.cleanup)
        cls.root = Path(temporary.name)
        cls.work = cls.root / "outside-checkout"
        cls.work.mkdir()
        cls.environment = offline_environment()
        cls.environment["UV_CACHE_DIR"] = str(cls.root / "uv-cache")
        environment = cls.root / "environment"
        uv = shutil.which("uv")
        venv.EnvBuilder(with_pip=uv is None).create(environment)
        cls.bin = environment / ("Scripts" if os.name == "nt" else "bin")
        cls.python = cls.bin / ("python.exe" if os.name == "nt" else "python")
        command = ([uv, "pip", "install", "--python", str(cls.python)] if uv else
                   [str(cls.python), "-m", "pip", "install"])
        result = subprocess.run([*command, "--no-deps", "--no-index", str(wheel)], cwd=cls.work,
                                env=cls.environment, capture_output=True, text=True, timeout=45)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    def run_installed(self, arguments):
        result = subprocess.run([str(value) for value in arguments], cwd=self.work, env=self.environment,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def executable(self, name):
        return self.bin / (name + ".exe" if os.name == "nt" else name)

    def test_installed_metadata_resource_and_imports_do_not_depend_on_checkout(self):
        script = textwrap.dedent("""
            import csv, importlib, importlib.metadata, importlib.resources, importlib.util, io, pathlib, sys
            package = importlib.import_module('jev4mztabm')
            assert pathlib.Path(sys.argv[1]) not in pathlib.Path(package.__file__).resolve().parents
            distribution = importlib.metadata.distribution('jev4mztabm')
            assert distribution.version == '0.1.0'
            assert distribution.metadata['Requires-Python'] == '>=3.10'
            requirements = distribution.requires or []
            assert all('extra ==' in value for value in requirements), requirements
            for name in ('typesafe_sdk', 'matplotlib', 'pydantic'):
                assert importlib.util.find_spec(name) is None, name
            for name in sys.argv[2:]:
                importlib.import_module('jev4mztabm.' + name)
            resource = importlib.resources.files('jev4mztabm').joinpath('data', 'target_accessions.csv')
            rows = list(csv.DictReader(io.StringIO(resource.read_text(encoding='utf-8-sig'))))
            identifiers = [row['ontology_accession'] for row in rows]
            assert len(identifiers) == len(set(identifiers)) == 156
            assert all(identifiers)
            expected = {'jev4mztabm', 'metabolights-factors', 'workbench-factors',
                        'ontology-accessions', 'workbench-jev', 'calibrate-workbench-jev'}
            assert expected <= {entry.name for entry in distribution.entry_points if entry.group == 'console_scripts'}
        """)
        self.run_installed([self.python, "-I", "-c", script, REPOSITORY, *MODULES])

    def test_all_console_help_and_unified_dispatch_work_without_extras(self):
        for name in ENTRY_POINTS:
            with self.subTest(entry_point=name):
                self.assertIn("usage:", self.run_installed([self.executable(name), "--help"]).lower())
        for command in ("metabolights", "workbench", "analyze", "jev", "calibrate", "reproduce", "snapshot"):
            with self.subTest(command=command):
                self.assertIn("usage:", self.run_installed([self.executable("jev4mztabm"), command, "--help"]).lower())
        self.assertIn("usage:", self.run_installed([self.python, "-I", "-m", "jev4mztabm", "--help"]).lower())

    def test_installed_base_analysis_and_jev_dry_run_need_no_optional_dependencies(self):
        fixture_script = textwrap.dedent("""
            import csv, json
            from pathlib import Path
            from jev4mztabm.workbench_jev import canonical_json, prepare_contexts
            from jev4mztabm.workbench_ontology_catalog import OLS_URL, build_catalog, save_catalog
            root = Path.cwd()
            accession = 'http://www.ebi.ac.uk/efo/EFO_0000727'
            fields = ['study_id','factor_name','factor_type','ontology_source','ontology_accession',
                      'ontology_version','ontology_file','factor_value_unique']
            row = dict.fromkeys(fields, '')
            row.update(study_id='ST000001', factor_name='Treatment', factor_value_unique='["control","drug"]')
            with (root / 'factors.csv').open('w', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader(); writer.writerow(row)
            with (root / 'accessions.csv').open('w', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=['ontology_accession'])
                writer.writeheader(); writer.writerow({'ontology_accession': accession})
            def fixture(url):
                if url == OLS_URL:
                    return {'ontologyId':'efo','config':{'id':'efo','version':'3.94.0',
                            'versionIri':'http://www.ebi.ac.uk/efo/efo.owl'}}
                return {'_embedded':{'terms':[{'iri':accession,'label':'treatment','description':['Treatment dimension'],
                        'synonyms':[],'ontology_name':'efo','is_obsolete':False}]}}
            catalog = build_catalog(root / 'accessions.csv', root / 'ols-cache', fetch_json=fixture)
            save_catalog(root / 'catalog.json', catalog)
            contexts = prepare_contexts(root / 'factors.csv')
            (root / 'contexts.jsonl').write_text(''.join(canonical_json(value) + '\\n' for value in contexts))
        """)
        self.run_installed([self.python, "-I", "-c", fixture_script])
        self.run_installed([self.executable("jev4mztabm"), "analyze", "--input", self.work / "accessions.csv",
                            "--output-dir", self.work / "analysis", "--no-plot"])
        self.assertTrue((self.work / "analysis" / "ontology_accession_frequencies.csv").is_file())
        result = self.run_installed([self.executable("jev4mztabm"), "jev", "run", "--input", self.work / "factors.csv",
                                     "--contexts", self.work / "contexts.jsonl", "--catalog", self.work / "catalog.json",
                                     "--output-dir", self.work / "dry-run", "--dry-run"])
        summary = json.loads(result)
        self.assertEqual(summary["jev_calls"], 0)
        self.assertFalse(summary["automatic_acceptance"])


if __name__ == "__main__":
    unittest.main()
