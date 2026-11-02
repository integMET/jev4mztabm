# jev4mztabm

An installable Python package for exporting study factors from MetaboLights and
Metabolomics Workbench, analyzing ontology accessions, and proposing factor
annotations with the Jev decision model.

The package preserves raw exports, exact factor values, source evidence and
model responses. Ontology proposals and accepted annotations are separate
outputs. It does not currently generate mzTab-M documents.

## Install

Use Python 3.10 or later. From a checkout:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install '.[all]'
jev4mztabm --version
```

The base package has no third-party runtime dependencies. Optional extras are:

| Extra | Adds |
| --- | --- |
| `analysis` | Matplotlib plots |
| `jev` | `typesafe-sdk==0.7.5` |
| `all` | Both analysis and Jev dependencies |
| `dev` | Distribution build tools |

For development, install `python -m pip install -e '.[all,dev]'`.
For the dependency versions recorded in this repository, use
`uv sync --frozen --extra all` and run commands with `uv run --frozen --extra all`.
The lockfile selects compatible dependencies for each supported Python version.

## Reproduce the workflow

Preview the workflow without network requests, credentials or file writes:

```sh
jev4mztabm reproduce --output-dir runs/example --no-plot
```

Run the public-data exports and local preparation:

```sh
jev4mztabm reproduce --output-dir runs/example --execute --no-plot
```

Omit `--no-plot` when the analysis extra is installed to create figures.
The workflow exports MetaboLights factors, counts structurally valid identifiers,
creates the Top 255 plots, selects the exact EFO URL substring, exports Workbench
factors with complete `factor_value_unique` lists, validates the frozen
156-accession vocabulary, prepares contexts and selects a 250-row review pilot.

Public data and ontology releases change. A fresh run is a new snapshot and may
have different counts from the November 2, 2026 reference run. The reference
vocabulary and summary are bundled as small package resources; downloaded
studies, generated results and paid responses are not included in distributions.

Use `--stage` for one stage, `--pilot-size` and `--seed` for another pilot,
or `--fetch-study-context` to retrieve additional study/protocol text. Existing
reviewed pilot files are preserved and checked before reuse.

## Commands

Both `jev4mztabm` and `python -m jev4mztabm` provide these subcommands:

| Command | Purpose |
| --- | --- |
| `metabolights` | Export public MetaboLights factor definitions and annotations |
| `workbench` | Export public Workbench factors and sample evidence |
| `analyze` | Validate/filter accessions, count frequencies and plot distributions |
| `jev` | Build a catalog, prepare evidence, select a pilot and propose mappings |
| `calibrate` | Validate an acceptance policy with reviewed, study-disjoint labels |
| `reproduce` | Preview or execute the ordered workflow |
| `snapshot` | Fingerprint archives, verify saved data and replay Jev proposals offline |

Each command accepts `--help`. Examples:

```sh
jev4mztabm metabolights --studies MTBLS1 MTBLS2 --output-dir runs/mtbls
jev4mztabm workbench --studies ST000001 ST000004 --output-dir runs/workbench
jev4mztabm analyze --input results/factors.csv --top 255 --no-plot
jev4mztabm analyze --input results/factors.csv \
  --accession-contains 'http://www.ebi.ac.uk/efo/' \
  --output-dir runs/efo-only --no-plot
jev4mztabm jev run --contexts results/workbench/jev/pilot_contexts.jsonl --dry-run
```

Paths default to the caller's current directory, not the installed package
directory. The original root scripts and their imports remain compatible.
Installed aliases include `metabolights-factors`, `workbench-factors`,
`ontology-accessions`, `workbench-jev` and `calibrate-workbench-jev`.

## Jev inference and review

Provide `TYPESAFE_API_KEY` in the environment of the Python process. In zsh,
enter it without putting the value in command history:

```zsh
set +x
read -rs "TYPESAFE_API_KEY?Jev API key: "
printf '\n'
export TYPESAFE_API_KEY
```

Paid inference is explicitly enabled in the reproduction command:

```sh
jev4mztabm reproduce --output-dir runs/example --stage jev --execute --with-jev
```

This pins `jev-1.14.0` and the SDK version. It selects from eligible terms and
explicit abstentions, asks companion eligibility/dimension questions, and
verifies selected terms in a second call. Matching successful responses are
cached. Rerunning the same stage reuses completed calls and retries failures.

The `mapping_decisions.csv` output contains proposals, abstentions and errors.
Without a reviewed calibration policy, `accepted_mappings.csv` stays empty and
the enriched export retains the original annotations. Automatic acceptance
requires a policy validated against separate tuning and held-out studies;
model confidence alone is insufficient. See the [reproducibility guide](docs/reproducibility.md)
for review and calibration commands.

The recorded run is **incomplete**: 8,300 of 9,321 rows completed; 1,021 received
HTTP 402 after TypeSafe credits ran out. It contains 6,608 proposals awaiting
review and zero accepted mappings. Only 113 rows had additional study context.
These are reference observations, not accuracy claims or fresh-run guarantees.

## Verify and replay saved results

Save fingerprints alongside an archive of the complete results tree:

```sh
jev4mztabm snapshot create --results-dir results --output snapshot.json --include-cache
jev4mztabm snapshot verify --results-dir restored/results --manifest snapshot.json
jev4mztabm snapshot replay-jev --results-dir restored/results --output-dir runs/replayed
```

Offline replay needs the archived factor CSV, catalog, contexts, audit, metadata
and successful response caches. It needs no SDK, API key or network connection.
It validates recorded requests and responses, reproduces proposal-only outputs,
and preserves recorded failures. It cannot recover missing answers or bypass
calibration. Existing replay outputs are never overwritten.

Read [reproducibility and archive requirements](docs/reproducibility.md) and the
[detailed workflow and source schemas](docs/workflow.md) for data semantics,
filtering rules, cache behavior and limitations.

## Test and build

Install the `dev` extra before building distributions.

```sh
python -m unittest discover -s tests -v
python -m build
```

The tests use offline fixtures and fake model clients. Installed-wheel tests also
exercise the package from outside the checkout:

```sh
JEV4MZTABM_TEST_WHEEL="$PWD/dist/jev4mztabm-0.1.0-py3-none-any.whl" \
  python -m unittest discover -s tests -p test_packaging.py -v
```

No API keys, downloaded studies or generated results are packaged. Builds create
a wheel and source archive under `dist/`; this repository does not publish them
automatically.
