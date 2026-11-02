"""Dispatch installed commands without importing optional dependencies."""

import argparse
import importlib
import sys

from . import __version__


COMMANDS = {
    "metabolights": "metabolights_factors",
    "workbench": "workbench_factors",
    "workbench-treatments": "workbench_treatments",
    "analyze": "analyze_ontology_accessions",
    "jev": "workbench_jev",
    "calibrate": "calibrate_workbench_jev",
    "reproduce": "reproduce",
    "snapshot": "snapshot",
}


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="jev4mztabm",
        description="Reproduce metabolomics factor exports, ontology analysis and Jev annotation.",
        epilog="Run 'jev4mztabm COMMAND --help' for the options of a command.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("command", nargs="?", choices=COMMANDS)
    if not args:
        parser.print_help()
        return 0
    if args[0] in {"-h", "--help", "--version"}:
        parser.parse_args(args)
        return 0
    if args[0] not in COMMANDS:
        parser.error(f"unknown command: {args[0]}")
    module = importlib.import_module("." + COMMANDS[args[0]], __package__)
    return module.main(args[1:])
