#!/usr/bin/env python3
"""Count structurally valid ontology accessions and plot their frequencies."""

import argparse
import csv
import ipaddress
import json
import math
import os
import re
import statistics
import sys
import tempfile
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit


def identifier_rejection_reason(value):
    """Return a structural rejection reason, or None for a term identifier.

    This is an offline filter for public HTTP(S) term IRIs and CURIE-shaped
    accessions. It does not establish term existence, membership or currency.
    Identifier spelling is preserved; rejected values are never repaired.
    """
    if not value or not value.strip():
        return "missing_identifier"
    if value.casefold() in {"-", "n/a", "na", "none", "null", "unknown", "placeholder"}:
        return "placeholder"
    if re.search(r'[\s<>"{}|\\^`\x00-\x1f\x7f]', value):
        return "invalid_identifier_characters"
    if re.search(r"%(?![0-9A-Fa-f]{2})", value):
        return "malformed_percent_escape"
    if value.count("#") > 1:
        return "malformed_fragment"
    try:
        parts = urlsplit(value)
    except ValueError:
        return "malformed_identifier"

    if parts.scheme not in {"http", "https"}:
        # A nonempty prefix and local part are required for standalone accessions.
        if (parts.scheme not in {"ftp", "file", "mailto", "urn", "data", "javascript"}
                and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*:[A-Za-z0-9][A-Za-z0-9_.:-]*", value)):
            return None
        return "not_http_term_iri_or_prefixed_accession"

    try:
        host = parts.hostname
        _ = parts.port  # Reject malformed or out-of-range ports.
    except ValueError:
        return "malformed_http_identifier"
    if not host or parts.username is not None or parts.password is not None:
        return "malformed_http_identifier"
    host = host.rstrip(".")  # DNS absolute names use the same namespace rules.
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return "nonpublic_identifier_host"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            labels = host.rstrip(".").encode("idna").decode("ascii").split(".")
        except UnicodeError:
            return "malformed_identifier_host"
        if len(labels) < 2 or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in labels):
            return "malformed_identifier_host"
    else:
        if not address.is_global:
            return "nonpublic_identifier_host"

    path = unquote(parts.path)
    fragment = unquote(parts.fragment)
    leaf = path.rsplit("/", 1)[-1]
    if (fragment or leaf).casefold() == "placeholder":
        return "placeholder"
    if re.search(r"https?:/*", path, re.IGNORECASE):
        return "embedded_url_in_identifier_path"
    if "#" in value and not fragment.strip():
        return "missing_term_reference"
    if not fragment:
        if re.search(r"\.(?:owl|obo|rdf|ttl|n3|jsonld|xml)$", path, re.IGNORECASE):
            return "ontology_document_without_term"
        if not leaf or leaf.casefold() in {"ontology", "ontologies", "obo", "terms", "ns", "vocab"}:
            return "ontology_namespace_without_term"
        if (host == "purl.allotrope.org" and re.fullmatch(r"/ontologies/[^/]+", path)
                or host == "purl.bioontology.org" and re.fullmatch(r"/ontology/[^/]+", path)
                or host == "www.bioassayontology.org" and path == "/bao"
                or host == "mimi.case.edu" and path.endswith("/UnitsOntology")):
            return "ontology_namespace_without_term"

    # Enforce known term routes without requiring numeric IDs for all vocabularies.
    if host == "purl.obolibrary.org":
        if fragment:
            if not re.fullmatch(r"/obo/[A-Za-z0-9_.-]+", path):
                return "malformed_obo_term_identifier"
        elif not re.fullmatch(r"/obo/[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9][A-Za-z0-9_]*", path):
            return "malformed_obo_term_identifier"
    if host == "ncicb.nci.nih.gov" and path.startswith("/xml/owl/EVS/Thesaurus.owl"):
        if path != "/xml/owl/EVS/Thesaurus.owl" or not re.fullmatch(r"C[0-9]+", fragment):
            return "malformed_ncit_term_identifier"
    if host == "purl.org" and re.match(r"/obo/owl[^/]", path):
        return "malformed_legacy_obo_term_identifier"
    return None


def read_accession_counts(path):
    """Read all deposited strings for input accounting and exclusion reporting."""
    counts = Counter()
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "ontology_accession" not in reader.fieldnames:
            raise ValueError("Input CSV must contain an ontology_accession column")
        for row in reader:
            accession = row.get("ontology_accession")
            if accession is None or None in row:
                raise ValueError(f"Malformed CSV record ending at line {reader.line_num}")
            counts[accession] += 1
    return counts


def accession_exclusion_reason(accession, accession_contains=None):
    """Apply the requested literal substring rule, or default structure checks."""
    if accession_contains is not None:
        return None if any(text in accession for text in accession_contains) else "not_matching_requested_substring"
    return identifier_rejection_reason(accession)


def partition_counts(raw_counts, accession_contains=None):
    """Separate selected identifiers from excluded values, conserving rows."""
    counts, excluded = Counter(), Counter()
    for accession, frequency in raw_counts.items():
        target = counts if accession_exclusion_reason(accession, accession_contains) is None else excluded
        target[accession] = frequency
    return counts, excluded


def read_counts(path, accession_contains=None):
    """Count identifiers selected by structure checks or literal substrings."""
    return partition_counts(read_accession_counts(path), accession_contains)[0]


def write_filtered_factors(input_path, output_dir, counts):
    """Preserve all columns and source order for the selected accession rows."""
    written = 0
    with Path(input_path).open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames or "ontology_accession" not in reader.fieldnames:
            raise ValueError("Input CSV must contain an ontology_accession column")
        with (Path(output_dir) / "factors_filtered.csv").open("w", newline="", encoding="utf-8") as target:
            writer = csv.DictWriter(target, fieldnames=reader.fieldnames)
            writer.writeheader()
            for row in reader:
                if row["ontology_accession"] in counts:
                    writer.writerow(row)
                    written += 1
    return written


def write_tables(counts, output_dir, excluded=None, accession_contains=None):
    """Write filtered frequencies, their distribution, and rejection reasons."""
    excluded = excluded or Counter()
    included_total = sum(counts.values())
    total = included_total + sum(excluded.values())
    with (output_dir / "ontology_accession_frequencies.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.writer(handle)
        percent_field = "percent_of_selected_factors" if accession_contains is not None else "percent_of_valid_factors"
        writer.writerow(("ontology_accession", "frequency", percent_field, "percent_of_all_factors"))
        for accession, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
            writer.writerow((accession, count, round(100 * count / included_total, 6), round(100 * count / total, 6)))

    with (output_dir / "excluded_ontology_accessions.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(("ontology_accession", "frequency", "reason", "percent_of_all_factors"))
        for accession, count in sorted(excluded.items(), key=lambda item: (-item[1], item[0])):
            writer.writerow((accession, count, accession_exclusion_reason(accession, accession_contains), round(100 * count / total, 6)))

    distribution = Counter(count for accession, count in counts.items() if accession != "")
    unique = sum(distribution.values())
    with (output_dir / "ontology_accession_frequency_distribution.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(("frequency", "number_of_accessions", "percent_of_accessions"))
        for frequency, number in sorted(distribution.items()):
            writer.writerow((frequency, number, round(100 * number / unique, 6)))


def summarize(counts, excluded=None, accession_contains=None):
    excluded = excluded or Counter()
    nonblank = [count for accession, count in counts.items() if accession != ""]
    occurrences = sum(nonblank)
    singletons = nonblank.count(1)
    result = {
        "input_factor_rows": sum(counts.values()) + sum(excluded.values()),
        "included_factor_rows": occurrences,
        "excluded_factor_rows": sum(excluded.values()),
        "blank_accession_rows_excluded": excluded.get("", 0),
        ("distinct_selected_accessions" if accession_contains is not None else "distinct_valid_accessions"): len(nonblank),
        "distinct_excluded_values": len(excluded),
        "accessions_occurring_once": singletons,
        "percent_accessions_occurring_once": round(100 * singletons / len(nonblank), 2) if nonblank else 0,
        "median_frequency": statistics.median(nonblank) if nonblank else None,
        "maximum_frequency": max(nonblank, default=0),
        "top_10_percent_of_included_occurrences": round(100 * sum(sorted(nonblank, reverse=True)[:10]) / occurrences, 2) if occurrences else 0,
    }
    if accession_contains is not None:
        result.update(filter_mode="exact_case_sensitive_substring_or", accession_contains=list(accession_contains))
    return result


def short_label(accession):
    """Shorten only chart labels; frequency-table identifiers remain unchanged."""
    parts = urlsplit(accession)
    label = (parts.fragment or parts.path.rstrip("/").rsplit("/", 1)[-1]) if parts.netloc else accession
    return label if len(label) <= 42 else label[:18] + "..." + label[-21:]


def plot_frequencies(counts, output_dir, top_n=255, excluded=None, accession_contains=None):
    # Keep Matplotlib's font cache in a writable location for headless execution.
    os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "metabolights-matplotlib"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    nonblank = {accession: count for accession, count in counts.items() if accession != ""}
    summary = summarize(counts, excluded, accession_contains)
    substring_mode = accession_contains is not None
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "svg.fonttype": "none"})
    top = sorted(nonblank.items(), key=lambda item: (-item[1], item[0]))[:top_n]
    expanded = len(top) > 30
    if expanded:
        # Thirty-two rows per panel keep all labels readable for large top-N views.
        panel_count = math.ceil(len(top) / 32)
        row_count = math.ceil((panel_count + 1) / 3)
        figure_height = row_count * 8 + 1.6
        fig, axes = plt.subplots(row_count, 3, figsize=(25, figure_height), squeeze=False)
        axes = list(axes.flat)
        histogram = axes[0]
        top_charts = axes[1:panel_count + 1]
        for unused in axes[panel_count + 1:]:
            unused.set_visible(False)
        fig.subplots_adjust(left=0.095, right=0.97, top=1 - 1.3 / figure_height,
                            bottom=1.4 / figure_height, wspace=0.72, hspace=0.30)
    else:
        fig, (histogram, top_chart) = plt.subplots(1, 2, figsize=(15, max(6.5, 2.2 + 0.25 * len(top))))
        top_charts = [top_chart]
        fig.subplots_adjust(left=0.06, right=0.97, top=0.79, bottom=0.18, wspace=0.50)
    title_y = 1 - 0.3 / fig.get_figheight() if expanded else 0.97
    subtitle_y = 1 - 0.75 / fig.get_figheight() if expanded else 0.90
    selection_label = "matching accessions" if substring_mode else "filtered accessions"
    population_label = "matching accession strings" if substring_mode else "structurally valid identifiers"
    fig.suptitle(f"MetaboLights factor frequencies: Top {len(top)} {selection_label}", fontsize=17, x=0.06, ha="left", y=title_y)
    fig.text(0.06, subtitle_y, f"{len(nonblank):,} {population_label} across {summary['included_factor_rows']:,} of {summary['input_factor_rows']:,} factor rows", fontsize=11, color="#435164")

    for axis in [histogram, *top_charts]:
        axis.spines[["top", "right"]].set_visible(False)
        axis.set_axisbelow(True)

    histogram.set_title("Frequency distribution", loc="left", fontweight="bold", pad=16)
    histogram.set_xlabel("Factor rows per accession (log scale)")
    histogram.set_ylabel("Number of accessions per bin (log scale)")

    if nonblank:
        frequencies = list(nonblank.values())
        edges = [2 ** power for power in range(max(frequencies).bit_length() + 1)]
        heights, _, _ = histogram.hist(frequencies, bins=edges, color="#3975a8", edgecolor="white", linewidth=1)
        histogram.set_xscale("log", base=2)
        histogram.set_yscale("log")
        histogram.set_xticks(edges)
        histogram.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:,.0f}"))
        histogram.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:,.0f}"))
        histogram.tick_params(axis="x", labelsize=8, rotation=35)
        histogram.set_ylim(0.7, max(heights) * 3)
        histogram.grid(axis="y", alpha=0.18)
        for left, right, height in zip(edges, edges[1:], heights):
            if height:
                histogram.text(math.sqrt(left * right), height * 1.13, f"{int(height):,}", ha="center", va="bottom", fontsize=9)
        singleton_population = "matching strings" if substring_mode else "valid identifiers"
        histogram.text(0.98, 0.97, f"{summary['accessions_occurring_once']:,} accessions occur once\n({summary['percent_accessions_occurring_once']:.1f}% of {singleton_population})", transform=histogram.transAxes, ha="right", va="top", fontsize=10)

        labels = [short_label(accession) for accession, _ in top]
        duplicate_labels = Counter(labels)
        labels = [f"{index}. {label}" if expanded else
                  (f"{label} (rank {index})" if duplicate_labels[label] > 1 else label)
                  for index, label in enumerate(labels, 1)]
        maximum = top[0][1]
        panel_size = 32 if expanded else len(top)
        for panel_index, top_chart in enumerate(top_charts):
            offset = panel_index * panel_size
            panel = top[offset:offset + panel_size]
            panel_labels = labels[offset:offset + panel_size]
            top_chart.barh(range(len(panel)), [count for _, count in panel], color="#3975a8", height=0.7)
            top_chart.set_yticks(range(len(panel)), labels=panel_labels)
            top_chart.tick_params(axis="y", labelsize=9)
            top_chart.set_ylim(len(panel) - 0.25, -0.75)
            top_chart.set_xlim(0, maximum * 1.2)
            top_chart.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:,.0f}"))
            top_chart.set_xlabel("Factor rows (shared scale)" if expanded else "Factor rows")
            heading = (f"Ranks {offset + 1} to {offset + len(panel)} of Top {len(top)}"
                       if expanded else f"Top {len(top)} deposited accessions")
            top_chart.set_title(heading, loc="left", fontweight="bold", pad=16)
            top_chart.grid(axis="x", alpha=0.18)
            for index, (_, count) in enumerate(panel):
                top_chart.text(count + maximum * 0.015, index, f"{count:,}", va="center", fontsize=9)
    else:
        for axis in [histogram, *top_charts]:
            axis.text(0.5, 0.5, "No matching accessions" if substring_mode else "No valid accessions", transform=axis.transAxes, ha="center")

    note_y = 0.55 / fig.get_figheight() if expanded else 0.065
    method_y = 0.24 / fig.get_figheight() if expanded else 0.03
    if substring_mode:
        note = f"Excluded {summary['excluded_factor_rows']:,} nonmatching rows. Counts use exact deposited strings; frequency ties use alphabetical accession order."
        method = "Case-sensitive contains filter (OR): " + " or ".join(accession_contains)
    else:
        note = f"Excluded {summary['excluded_factor_rows']:,} rows with invalid or missing identifiers. Rejected values and reasons are saved in excluded_ontology_accessions.csv."
        method = "Offline identifier-structure checks only; term existence is not verified. Accepted strings are not normalized. Frequency ties use alphabetical accession order."
    fig.text(0.06, note_y, note, fontsize=9, color="#435164")
    fig.text(0.06, method_y, method, fontsize=9, color="#435164")
    for extension in ("png", "svg"):
        fig.savefig(output_dir / f"ontology_accession_frequency_distribution.{extension}", dpi=180, facecolor="white")
    plt.close(fig)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("results/factors.csv"))
    parser.add_argument("--output-dir", type=Path, default=Path("results/ontology_analysis"))
    parser.add_argument("--top", type=int, default=255, help="Number of accessions in the top-frequency chart (default: 255)")
    parser.add_argument("--no-plot", action="store_true", help="Write CSV tables without requiring Matplotlib")
    parser.add_argument("--accession-contains", action="append", metavar="TEXT",
                        help="Keep accessions containing any supplied exact substring; replaces structure filtering")
    args = parser.parse_args(argv)
    if args.top < 1:
        parser.error("--top must be positive")
    if args.accession_contains is not None and any(not text for text in args.accession_contains):
        parser.error("--accession-contains must not be empty")
    try:
        counts, excluded = partition_counts(read_accession_counts(args.input), args.accession_contains)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        write_tables(counts, args.output_dir, excluded, args.accession_contains)
        if args.accession_contains is not None:
            written = write_filtered_factors(args.input, args.output_dir, counts)
            if written != sum(counts.values()):
                raise ValueError("Filtered row count does not match the frequency total")
        if not args.no_plot:
            plot_frequencies(counts, args.output_dir, args.top, excluded, args.accession_contains)
    except ImportError as error:
        print(f"CSV tables were written, but plotting needs Matplotlib: {error}. Install 'jev4mztabm[analysis]' or use --no-plot.", file=sys.stderr)
        return 1
    except (OSError, ValueError, csv.Error) as error:
        print(f"Analysis failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summarize(counts, excluded, args.accession_contains), indent=2))
    print(f"Outputs: {args.output_dir.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
