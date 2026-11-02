# Reproducing the factor and ontology workflow

`jev4mztabm` packages the exporters, accession analysis, ontology catalog, Jev
decisions and calibration tools. Version 0.1.0 installs the `jev4mztabm` command;
`python -m jev4mztabm` exposes the same interface. Legacy root scripts remain
compatibility entry points when working from the checkout.

Installing the code and preserving a scientific run are separate tasks. The
package does not supply the complete downloaded studies or paid model responses.
A fresh run retrieves current public metadata and may produce different results.
Preserve the recorded artifacts below to verify the earlier run.

## Install and record the software environment

Use Python 3.10 or later; the combined example uses Python 3.12. From this
checkout:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install '.[all]'
jev4mztabm --help
python -m jev4mztabm --help
```

The base installation, `python -m pip install .`, supports the exporters and
table-only analysis. The `analysis` extra adds plotting dependencies, `jev` adds
the pinned TypeSafe SDK, and `all` combines both. For development, use
`python -m pip install -e '.[all]'`; use a regular installation when checking the
built distribution. The `src/jev4mztabm` layout separates importable code from
checkout files. These conventions follow [PyPA's packaging guidance](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/),
its [src-layout discussion](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/),
and [setuptools console entry points](https://setuptools.pypa.io/en/latest/userguide/entry_point.html).

The repository also includes `uv.lock`, with resolved dependency versions and
artifact hashes. Use `uv sync --frozen --extra all` for that locked environment,
then `uv run --frozen --extra all jev4mztabm --help`. Lockfile resolution accounts
for Python/platform compatibility; record which interpreter and platform you use.

To build an archive of the software and record the resolved environment:

```sh
python -m pip install build
python -m build
python -m pip freeze --all > environment.txt
shasum -a 256 dist/*.whl > wheel-sha256.txt
```

Retain the wheel, its hash, the source revision, Python version, operating system
and dependency versions. An environment listing alone is not a portable lockfile.
For stricter repeatable installation, retain dependency wheels and fully pinned
requirements with hashes, as described in [pip's repeatable-install guidance](https://pip.pypa.io/en/stable/topics/repeatable-installs/).
Plot rendering can also vary with Matplotlib, fonts and platform; compare the
underlying CSV tables separately from image bytes.

## Run the workflow in a new output directory

Keep the recorded `results/` tree intact. Start by inspecting a plan:

```sh
jev4mztabm reproduce --output-dir runs/new-run --no-plot
```

`reproduce` defaults to a dry plan. Execution must be explicit:

```sh
jev4mztabm reproduce --output-dir runs/new-run --execute --no-plot
```

This uses public metadata services and local processing; paid Jev inference
requires the additional `--with-jev` flag. The `--stage` selector supports
`all`, `metabolights`, `analysis`, `workbench`, `catalog`, `contexts`, `pilot`
and `jev`. `--fetch-study-context` opts into additional public study/protocol
retrieval, which can download large metadata responses. `--pilot-size` defaults
to 250 and `--seed` to 42. Omit `--no-plot` after installing the plotting extra
to produce figures.

The orchestrated catalog stage uses the package's frozen
`data/target_accessions.csv`, preserving the original 156 literal accessions
even if a fresh accession-frequency analysis changes. Its default
`--expected-catalog-count` is 156. The individual `jev catalog` command retains
its historical default frequency-CSV input; use its `--accessions` option to
select a different explicit vocabulary. Changing targets requires a new catalog
and invalidates a calibration policy tied to the prior catalog identity.

The component commands remain available for selective work:

```sh
jev4mztabm metabolights --studies MTBLS1 MTBLS2 --output-dir runs/metabolights-pilot
jev4mztabm workbench --studies ST000001 ST000004 --output-dir runs/workbench-pilot
jev4mztabm analyze --input results/factors.csv --output-dir runs/analysis --no-plot
jev4mztabm jev run --contexts results/workbench/jev/pilot_contexts.jsonl --dry-run
```

For a new mapping experiment, first review the pilot and obtain independent
labels. A paid full-context run is a separate operation:

```sh
jev4mztabm reproduce --output-dir runs/new-run --stage jev --execute --with-jev
```

Supply `TYPESAFE_API_KEY` through the execution environment. Do not put a key in
commands, configuration examples, source files or result archives. No credential
is needed for `--dry-run`. New inference consumes TypeSafe credits; matching
successful response caches are reused, while prior API failures can be retried.

Calibration requires genuinely reviewed labels and an untouched held-out split:

```sh
jev4mztabm calibrate \
  --audit results/workbench/jev/mapping_decisions.csv \
  --labels results/workbench/jev/reviewed_labels.csv \
  --output runs/acceptance_policy.json \
  --target-precision 0.98 --min-accepted 20
```

An approved application flag is not evidence that a row was individually reviewed.
Keep human review, model proposals and calibrated automatic acceptance distinct.

## Recorded snapshot: November 2, 2026

These counts were checked against the local CSVs and metadata, rather than
inferred from a fresh query:

| Artifact | Recorded state |
| --- | --- |
| MetaboLights export | 7,505 factor rows; 3,610 studies, comprising 3,566 with factors and 44 without factors |
| Workbench export | 9,321 factor rows from 4,684 studies; no failed studies |
| Target catalog | 156 accessions: 114 eligible, 41 obsolete, one MEDO accession outside the automated EFO namespace |
| Prepared contexts | 9,321 rows; 113 rows across 39 studies carry additional nonempty study context |
| Jev audit | 9,321 rows: 8,300 non-error outcomes and 1,021 HTTP 402 credit errors |
| Non-error outcomes | 6,608 `needs_review`, 821 `mixed_factor`, 533 `unmapped`, 338 `ambiguous` |
| Accepted mappings | Zero; automatic acceptance was disabled and no calibration policy was supplied |

The recorded mapping run used `jev-1.14.0`, `typesafe-sdk` 0.7.5 and prompt
version `workbench-factors-v1`. Its catalog identity was
`7f10b5978d37db9abaa71ca9aff69fe0908ca9d96364afe21113144adc486806`.
The original Workbench factor CSV hash was
`6590972941382ae9b110478f005edb4b25c5e217d35541e274f51440b949dfce`;
the context JSONL hash was
`2b8aab54921478f2fe0db4f2ae4f312bdab75fbd9bc6bad1423a77e9aee54e01`.
Check the run's own metadata when comparing another snapshot.

The 113 context-rich rows are not the only rows with evidence: every prepared
row retains its factor name, values and sample-evidence summary. Additional
study narratives were available for only that recorded subset. The 1,021 error
rows are unresolved API outcomes, not unmapped findings or accepted assignments.

## Preserve artifacts for exact verification

Archive these together, retaining relative paths and file bytes:

| Group | Required artifacts |
| --- | --- |
| Public-study inputs | MetaboLights `results/.cache/`, its factor/status CSVs; Workbench `results/workbench/.cache/`, including listing manifests, native mwTab replies and retrieval metadata |
| Export evidence | Workbench factor, sample-factor, status and warning CSVs, plus `run_metadata.json` |
| Vocabulary | The selected accession CSV, bundled frozen accession list, validated `ontology_target_catalog.json` and official OLS response caches |
| Model inputs | Exact `contexts.jsonl`, optional pilot contexts, study-context caches and any supplied study/protocol evidence |
| Model outputs | Successful `.cache/jev/` request/response snapshots, `mapping_decisions.csv`, enriched output and mapping-run metadata |
| Review and policy | Reviewed labels, their tuning/test split, any calibration report/policy and accepted-mapping provenance |
| Software | Source revision or wheel with hashes, interpreter/platform details and the resolved dependency environment |

The current recorded Jev response cache contains 14,921 successful request
snapshots. A request fingerprint binds the request content, prompt version and
SDK version; it is not the provider's HTTP request ID. Preserve the actual cache
files, not just their number. Of the 1,021 credit-error rows, 13 have a cached
selection but lack verification; 1,008 lack a selection response. Replay retains
those failures rather than manufacturing missing answers.

Create a manifest that includes the raw and response caches:

```sh
jev4mztabm snapshot create --results-dir results --output snapshot.json --include-cache
```

The manifest contains relative paths and hashes, not the underlying data. Archive
the complete selected `results/` tree alongside it; keep the files listed above,
including hidden cache directories. After restoring or moving the archive:

```sh
jev4mztabm snapshot verify --results-dir restored/results --manifest snapshot.json
jev4mztabm snapshot replay-jev --results-dir restored/results --output-dir runs/replayed-jev
```

`replay-jev` regenerates the recorded Jev outputs from the archived factor CSV,
catalog, contexts, audit and validated successful response caches. It runs
without the TypeSafe SDK, an API key or network access. Original source-path
strings remain part of frozen request fingerprints, but replay reads the restored
factor CSV and validates its actual hash instead of opening paths from the
original machine. Recorded HTTP 402 rows are preserved as errors without retry.
This reproduces the incomplete audit; it does not manufacture their missing
answers.

The replay path currently supports proposals-only runs such as this snapshot.
It rejects nonempty accepted mappings instead of bypassing calibration-policy
validation. Regenerating an accepted-mapping run requires supported validation of
its archived policy and review artifacts.

Hash verification establishes whether archived artifacts are unchanged. Hashes
detect changed bytes; they do not establish
scientific correctness, authenticity of a deliberately rewritten manifest, or
completeness of missing responses. An installed package alone cannot reconstruct
discarded public metadata or paid responses. Cache reuse and archive verification
are not a promise that every workflow stage can regenerate outputs offline.

Live study listings, annotations, ontology releases and retrieval formats can
change. Jev produces probabilistic decisions; pinning a model identifier does not
provide a documented guarantee of identical future response probabilities.
Exact verification therefore relies on archived inputs and recorded responses,
whereas new network or model calls constitute a new run.

Keep secrets outside both archives and distribution artifacts. Source-code
packaging does not establish redistribution rights for downloaded datasets;
check the applicable providers' terms before sharing data archives. No dataset
license is asserted here.
