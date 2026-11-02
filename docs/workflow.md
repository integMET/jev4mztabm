# MetaboLights factor ontology export

Export **Factors (Sample Groups)** and their ontology annotations from all public
[MetaboLights](https://www.ebi.ac.uk/metabolights/) studies. The Python script uses
only the standard library (Python 3.9 or later); no installation or API token is
needed for public studies.

```sh
python3 metabolights_factors.py
```

By default, the script discovers all public accessions from `GET /ws/studies`,
then fetches `GET /ws/studies/{study_id}/factors` for each one. It writes:

- `results/factors.csv`: one row per study factor, retaining the study ID, factor
  name, ontology label (`factor_type`), ontology source, deposited accession or
  URL, ontology version, and ontology file URL.
- `results/study_status.csv`: one row per requested study, with `ok`, `no_factors`,
  or `error` status, factor count, response source (`api` or `cache`), and error
  details. Failed studies have a blank factor count, rather than an apparent zero.
- `results/.cache/`: validated API responses, reused on subsequent runs. The CSV
  files are rebuilt on each run; cached studies are included again without a
  network request. The public study listing is always fetched again.

Try a small export first, or select another output directory:

```sh
python3 metabolights_factors.py --studies MTBLS1 MTBLS2 --output-dir example-results
```

For a fresh snapshot of all currently public studies, including changes to
previously cached annotations:

```sh
python3 metabolights_factors.py --refresh
```

The exporter makes sequential requests, spaces request starts by at least 0.2
seconds, and retries network failures, HTTP 429, and HTTP 5xx responses up to
three times, honoring `Retry-After`. Tune `--interval`, `--timeout`, or `--retries`
if necessary. A full export involves thousands of requests and can take a while.
After interruption or failed studies, rerun the same command to reuse completed
responses and retry the rest. Exit status is 0 for success, 1 for a partial export
(including per-study cache errors), 2 for a listing or CSV output error, and 130
for interruption. Check
`study_status.csv` before treating the export as complete.

These are **study factor definitions**, such as a factor named “Metabolic
syndrome” mapped to an EFO term. Individual sample group values, such as case
versus control assignments, are stored separately in ISA-Tab sample sheets and
are not part of this export. Unannotated factors are kept with blank ontology
fields; the script does not infer or replace ontology mappings. Private and
unreleased studies are outside the public listing.

The API response shapes were verified against the live service on November 2,
2026. See the official [API specification](https://www.ebi.ac.uk/metabolights/ws/api/spec.json)
and [sample/factor guide](https://ebi-metabolights.github.io/guides/Sample/) for the
source fields and the distinction between factors and sample values.

Run the offline checks with:

```sh
python3 -m unittest discover -s tests -v
```

Analyze the deposited ontology accession frequencies and create distribution
charts with:

```sh
python3 -m pip install -r requirements-analysis.txt
python3 analyze_ontology_accessions.py
```

This writes `results/ontology_analysis/ontology_accession_frequencies.csv`, sorted
by descending frequency, and a count-of-counts table named
`ontology_accession_frequency_distribution.csv`. The latter records how many
distinct included accessions occur once, twice, and so on. The accompanying PNG
and SVG contain a histogram with logarithmic bins and axes, plus the top 255
deposited accessions by default. Large top-N views split the ranked accessions
across panels with readable labels and a shared count scale. Ties are ordered
alphabetically by the full accession string. Frequencies count **factor rows**,
rather than samples or unique studies.

Only structurally valid term identifiers are counted: public HTTP(S) term IRIs
or prefixed accessions such as `EFO:0005135`. The filter excludes blanks,
placeholder values, bare ontology names, malformed URLs, ontology documents or
namespace roots without term references, and nonpublic hosts. Known OBO and
legacy NCIT term routes receive additional structure checks. Named terms such
as FOAF `Group`, OBO `NCBITaxon_species`, and legacy `.owl#term` identifiers are
preserved. The rules use the [OBO identifier policy](https://obofoundry.org/id-policy.html)
and [CURIE syntax](https://www.w3.org/TR/curie/) as format guidance.

Validation is offline: acceptance does not establish that a term exists, belongs
to the stated ontology, resolves online, or is current. Accepted strings are
counted exactly as deposited; URL variants are not merged and invalid strings
are not repaired. The source CSV is unchanged.

Rejected values, their frequencies, and rejection reasons are saved separately
in `results/ontology_analysis/excluded_ontology_accessions.csv`. They are excluded
from the frequency table, distribution table, and charts. The frequency table
reports `percent_of_valid_factors` using the included factor rows, and
`percent_of_all_factors` using all original rows, including those excluded.
The distribution table's `percent_of_accessions` uses distinct included
identifiers. The console summary reports both included and excluded totals.

Use `--input PATH`, `--output-dir PATH`, or `--top N` to customize the analysis.
`--no-plot` produces the three CSV tables using only the Python standard library.

To select accession values containing either of two exact strings and plot that
selection:

```sh
python3 analyze_ontology_accessions.py \
  --accession-contains 'http://purl.obolibrary.org/obo/' \
  --accession-contains 'http://www.ebi.ac.uk/efo/' \
  --output-dir results/ontology_analysis_obo_efo
```

Repeated `--accession-contains` arguments use case-sensitive OR matching anywhere
in the deposited value. This mode replaces the structural identifier filter;
no URL normalization or additional identifier rejection is applied. It also
writes `factors_filtered.csv`, preserving all source columns and matching rows
in their original order. The source CSV is unchanged. In this mode,
`percent_of_selected_factors` uses retained rows as its denominator, while
`percent_of_all_factors` still uses all original rows.

## Metabolomics Workbench treatments

```sh
jev4mztabm workbench-treatments --output-dir results/workbench/treatments
# A small selection, without the discovery request:
jev4mztabm workbench-treatments --studies ST000004 ST002115 --output-dir runs/treatments-pilot
```

The default selects every unique public study from
`GET /rest/study/study_id/ST/available`, including studies with multiple analyses.
It retrieves the HTML Treatment tab of each study's
[metadata page](https://www.metabolomicsworkbench.org/data/DRCCMetadata.php?Mode=Study&StudyID=ST002115&StudyType=MS&ResultType=1&DataMode=TreatmentData).
The `Treatment ID:` field is unavailable in mwTab JSON. Study and treatment
accessions need not share a numeric suffix: ST002115 has TR002243.
The exporter verifies the study heading and treatment section, checks the
accessions, and rejects conflicting duplicate fields and error pages returned
with HTTP 200.

- `treatments.csv` has exactly `study_id`, `treatment_id`, and
  `treatment_summary`, with one row per study/treatment pair in study accession
  order. Every selected study has at least one row. When the source lists several
  Treatment IDs, each retains its associated summary on a separate row; no record
  is selected or discarded based on the narrative. Exact duplicate records are
  deduplicated, and conflicting summaries for the same ID are rejected.
  An absent field is blank. The separate `Treatment:` field is never used as a
  summary. Literal summaries such as `NA` remain text. HTML entities are decoded,
  inline markup is removed, formatting whitespace is collapsed, and explicit
  HTML line/paragraph breaks remain newlines inside properly quoted CSV cells.
- `treatment_status.csv` has one row per study with `study_id`, `status`,
  `treatment_count`, `source`, `source_url`, and
  `error`. Status is `ok`, `missing_summary`, `missing_treatment_id`,
  `no_treatment` (an empty treatment section), `fetch_error`, `parse_error`, or
  `cache_error`. Failed studies retain a blank data row. Missing metadata is a
  successful retrieval; failures cause exit code 1. Setup/discovery errors cause
  exit code 2.
- `.cache/treatments/` retains raw HTML pages. Valid pages are reused; invalid
  pages are retried. `--refresh` bypasses valid caches. `.cache/study_listing.json`
  records the public listing when discovery is used. `run_metadata.json` records
  the selected IDs, timestamps, request settings, and failure count.

The shared client defaults to four workers and a minimum of 0.25 seconds between
request starts, retries transient network/HTTP errors, and respects HTTP 429
backoff. Use `--workers`, `--interval`, `--timeout`, `--retries`, and `--limit` to
adjust the run. `python -m jev4mztabm workbench-treatments`, the installed
`workbench-treatments` alias, and `python workbench_treatments.py` expose the same
options. The package has no additional runtime dependencies for this export.

For the full command, recorded snapshot checks, and offline CSV regeneration,
see [reproducing the treatment table](reproducibility.md#reproduce-resultsworkbenchtreatmentstreatmentscsv).

## Metabolomics Workbench export

Export all public [Metabolomics Workbench](https://www.metabolomicsworkbench.org/)
study factors with:

```sh
python3 workbench_factors.py
```

The exporter discovers available studies through `GET /rest/study/study_id/ST/available`,
deduplicates study IDs across analysis records, then fetches `/factors` per
study. The documented factors are sample-level `NAME:VALUE`
pairs, rather than ontology annotations. See the official
[API v1.2 specification](https://www.metabolomicsworkbench.org/tools/MWRestAPIv1.2.pdf)
and [mwTab specification](https://www.metabolomicsworkbench.org/data/mwTab_specification.pdf).
Names and values use whitespace-surrounded `|` separators and the first `:`.
Compact separators are also accepted when another `NAME:VALUE` pair follows.
This preserves literal unspaced pipes in values such as `Ju|Hoan`, as well as
semicolons and additional colons. Empty spaced slots, unnamed factors, empty
factor responses, and malformed pairs trigger an authoritative mwTab JSON
fallback. This matters because the flat API can omit a real `Sample source`
factor and leave doubled pipes. Native sample IDs and study IDs are checked;
all parseable named flat facts must remain present. Native metadata across
analyses may add factors but must not conflict. Additional sample metadata is
separate from experimental factors.

Outputs are written under `results/workbench/`:

- `factors.csv` contains the seven base columns from the MetaboLights export:
  `study_id`, `factor_name`, `factor_type`, `ontology_source`,
  `ontology_accession`, `ontology_version`, and `ontology_file`. Each study/name
  pair appears once. An additional `factor_value_unique` column holds a sorted
  JSON array of every distinct observed `factor_value` for that exact study/name
  pair. Values are taken from the resolved sample evidence, including all
  repeated positions and any observed empty string. Their spelling, case,
  internal whitespace, and literal delimiters are preserved. The five ontology fields remain blank; no ontology
  assignments are generated from free text.
- `sample_factors.csv` retains sample identifiers, sample source, factor names
  and values, the 1-based `factor_position` within the selected metadata
  source, and the original `raw_factors`
  string. `factor_source` distinguishes `factors_api` from `mwtab`, and
  `analysis_ids` identifies the analyses supplying native pairs. Repeated names
  within a sample are preserved at their distinct
  positions and flagged for review. Repeated identical API records are
  deduplicated; conflicting records for the same sample/position fail parsing.
  Blank factor strings remain visible with position 0 and a warning when no
  native factor list is available. Native unnamed factors retain their values
  and positions with warnings; no study-factor names are invented. Empty
  padding from the flat serializer is retained in `raw_factors`, rather than
  added to an authoritative native factor list.
- `study_status.csv` distinguishes `ok`, `no_factors`, `fetch_error`,
  `parse_error`, and `cache_error`, with counts, cache/API source, and error
  details. `no_factors` means no named study factors were extracted;
  `sample_factor_count` counts evidence rows with a factor name. `sample_count`
  counts sample records identified by Workbench sample
  ID, falling back to the local ID; it is not a count of biological subjects.
- `parsing_warnings.csv` records blank factors/slots, blank values, and repeated names.
- `factor_catalog.csv` summarizes names across studies, sample-factor row
  counts, and up to the first ten distinct nonblank values encountered per
  name, displayed in sorted order. These examples are not exhaustive.
- `ontology_mapping_template.csv` contains one row per study factor, with
  additional `mapping_method`, `evidence`, and `review_status` columns. Every
  generated row starts as `pending`.
- `.cache/` retains JSON replies and the discovery listing/manifest;
  `.cache/mwtab/` retains fallback native replies and retrieval metadata;
  `run_metadata.json` records the selected studies, timestamps, and run settings.

Native recovery uses only analysis IDs in the current public listing, excluding
obsolete investigations that can break study-level JSON. Duplicate keys in
native `Factors` and `Additional sample data` objects are preserved as ordered
pairs in the JSON cache. Verification allows formatting-whitespace differences
without changing the native text. A demonstrated flat-API bug splits `__` inside
values and shifts subsequent cells; recovery is allowed only when the entire
ordered flat response exactly matches that transformation, and is recorded in
the warnings file. Original flat strings remain available for comparison.

The defaults are four workers with a shared minimum of 0.25 seconds between
request starts. Use `--workers`, `--interval`, `--timeout`, and `--retries` to
adjust requests. Native mwTab requests allow at least 120 seconds for larger
responses. Rerun the same command after interruption or failures: valid
cached studies are reused, and failed or invalid cached studies are retried.
Use `--refresh` to refetch valid caches. Default discovery is refreshed on each
run. Generated CSVs, including the mapping template, are rebuilt; keep reviewed
mapping files separately. Check study statuses before treating a run as complete.

For ontology enrichment, copy only reviewed rows into a separate CSV, fill in
the annotation, mapping method, and evidence, and set `review_status` to
`approved`. Retain every template column; ontology version and file may remain
blank when unknown. The loader rejects pending, malformed, or duplicate rows.
Mappings match exact study/name pairs and are applied with:

```sh
python3 workbench_factors.py --mappings approved_mappings.csv
```

This additionally writes `factors_enriched.csv` and `mapping_provenance.csv`,
marking mappings as `applied` or `unmatched`. The raw `factors.csv` retains blank
ontology fields. Identifier validation checks structure, not term existence or
scientific appropriateness; the script does not assign mappings automatically.

Run a small pilot in a separate output directory, or run the offline checks:

```sh
python3 workbench_factors.py --limit 5 --output-dir results/workbench-pilot
python3 -m unittest discover -s tests -p 'test_workbench*.py' -v
```

Use `--studies ST000001 ST000004` to select exact studies. Exit status is 0 for
success, 1 for failed studies or unmatched mappings, 2 for a run-level error,
and 130 for interruption.

Add or refresh the unique-value column in an existing export without retrieving
studies again:

```sh
python3 workbench_factors.py --update-factor-values
```

This reads `sample_factors.csv` once and replaces only `factors.csv`, retaining
its existing rows and other columns. The normal exporter also populates this
column, and an enriched export preserves it.

## Jev ontology assignment

`workbench_jev.py` maps factor **dimensions** to the fixed accession vocabulary
using Jev and the official `typesafe_sdk` Python package. The mapping environment
requires Python 3.10+; the existing exporters keep their standard-library-only
dependencies. Install the pinned SDK in a separate environment:

```sh
uv venv --python 3.12 .venv-jev
uv pip install --python .venv-jev/bin/python -r requirements-jev.txt
source .venv-jev/bin/activate
```

Build the target catalog and prepare evidence:

```sh
python workbench_jev.py catalog
python workbench_jev.py prepare --fetch-study-context
python workbench_jev.py pilot --size 250 --seed 42
python workbench_jev.py run --contexts results/workbench/jev/pilot_contexts.jsonl --dry-run
```

The default catalog input is
`results/ontology_analysis_efo_only/ontology_accession_frequencies.csv`; exactly
156 distinct accessions are expected. For another unique accession CSV, use
`catalog --accessions PATH --expected-count N`. Official OLS responses are cached
by request URL. Canonical labels, definitions, synonyms and known ontology release
metadata are retained. Explicitly obsolete, unresolved, malformed and non-EFO
entries stay in the catalog for audit but cannot become automatic candidates.
For example, the MEDO URL included by the earlier literal EFO-path selection
is retained as out of namespace. `catalog --refresh` updates metadata; a changed
catalog invalidates an existing acceptance policy. Catalog JSON content hashes
detect accidental modification, not maliciously re-signed files.

`prepare` preserves the complete JSON value list and the distinct values at
every sample-factor position, including blanks. It checks these values against
the source factor CSV. Cached native mwTab study/protocol descriptions are added
when available. `--fetch-study-context` additionally retrieves and caches public
Workbench study summaries. When a summary omits narrative descriptions, it
recovers study/protocol text from mwTab metadata for the study's current public
analyses. This can download large metadata responses; extracted context is
cached for subsequent runs. Use `prepare --studies ST000001 ST000004` to prepare
a subset, or omit fetching for local-only preparation. Additional context
can be supplied with `--study-context PATH`, a JSON object keyed by exact study
ID, for example `{"ST000001": {"study_summary": "...", "protocol": "..."}}`.
Raw sources, source hashes and study references remain attached to each context.
Run validation rejects changed local evidence; prepare contexts again after a
source update. It also rejects output paths that would overwrite an input.

The pilot balances frequent names, rare names, broad ambiguous names and rows
with multiple factor positions. Its tuning and test studies are disjoint.
Existing pilot files and reviewed labels are never overwritten. Choose new
`--output` and `--labels` paths to create another pilot.

To obtain proposals, set the API key in your environment and run:

```sh
python workbench_jev.py run --contexts results/workbench/jev/pilot_contexts.jsonl
```

The SDK reads `TYPESAFE_API_KEY`; credentials are not written to outputs. This
command makes billable API calls using pinned model `jev-1.14.0`. It sends the
prepared public factor/study context and target catalog to TypeSafe. All eligible
terms plus `UNMAPPED`, `AMBIGUOUS` and `MIXED_FACTOR` fit in one Choice question.
Companion Noul questions ask whether any eligible term fits and whether the row
represents one dimension. A separate second request verifies the selected term
against the original evidence. The questions classify the dimension, such as
treatment, rather than an observed value, such as a particular drug.

No assignments are accepted automatically without a validated calibration
policy. The first run creates `mapping_decisions.csv` with model choices,
probabilities, confidence, evidence and review outcomes. Jev does not generate
explanations; evidence is copied from source context by Python. Review
`reviewed_labels.csv`, set `review_status` to `reviewed`, and fill
`expected_accession` with the exact eligible IRI. A blank expected accession means
a reviewed finding that no eligible mapping applies, not an unfinished review.
Keep the generated study-level `tune`/`test` split.

```sh
python calibrate_workbench_jev.py \
  --audit results/workbench/jev/mapping_decisions.csv \
  --labels results/workbench/jev/reviewed_labels.csv \
  --output results/workbench/jev/acceptance_policy.json \
  --target-precision 0.98 --min-accepted 20

python workbench_jev.py run --policy results/workbench/jev/acceptance_policy.json
```

The calibrator searches 35 threshold policies on tuning studies, selects for
coverage subject to observed precision, freezes the chosen policy, and evaluates
it once on held-out studies. Both splits must meet the requested observed
precision and minimum accepted-row count. Failure writes no new policy. Do not
repeatedly tune against the same held-out labels; use fresh validation studies
after changing questions or rules. The policy records label/audit hashes and is
bound to the exact catalog, model and prompt version. Model confidence and Noul
scores are not independently established probabilities of mapping correctness;
the report includes descriptive precision intervals, not future guarantees.

Outputs under `results/workbench/jev/` are:

- `ontology_target_catalog.json`: validated target vocabulary and eligibility.
- `contexts.jsonl`, `pilot_contexts.jsonl`, `reviewed_labels.csv`: full evidence
  and the review sample/template.
- `mapping_decisions.csv`: every processed decision, including abstentions,
  low-support proposals and API/validation errors as separate outcomes.
- `accepted_mappings.csv`: only policy-accepted mappings in the existing
  mapping-loader format. `acceptance_method=automatic_calibrated_policy`
  distinguishes automated acceptance from human review; `review_status=approved`
  is the loader's application flag, not a claim of individual human review.
- `factors_enriched.csv`: all source rows, in source order, with accepted
  ontology fields filled and `factor_value_unique` unchanged.
- `run_metadata.json`: source hashes, policy identity and counts, including
  unprocessed rows when using a pilot or `--limit`.
- `.cache/jev/`: full successful request/response snapshots. Each `request_id`
  is a SHA256 fingerprint of the request, prompt version and SDK version, not a
  provider HTTP request ID. Reruns reuse matching snapshots; errors are retried.

Use `--workers` to bound concurrency. SDK retry handling backs off on transient
failures. `--max-question-bytes` and `--max-request-bytes` provide conservative
payload guards; these are **not** Jev token counts or guarantees of fitting its
32k per-question/64k per-request token limits. Oversized evidence is never silently
truncated. Preflight and API context-limit failures remain errors requiring
context preparation or explicit budget changes. A dry run needs neither an SDK
installation nor an API key and makes no Jev calls.

The original `results/workbench/factors.csv` is retained. All scientific mapping
decisions are tested using offline fixtures; no inference is performed by tests.
The SDK and model interfaces follow the official
[Python SDK](https://docs.typesafe.ai/sdk/python),
[Choice](https://docs.typesafe.ai/primitives/choice),
[Noul](https://docs.typesafe.ai/primitives/noul), and
[confidence](https://docs.typesafe.ai/confidence) documentation.
