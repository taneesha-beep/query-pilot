"""Toolchain checks, so continuous integration has something to run from the first commit."""

import sys

import query_pilot


def test_package_imports_and_declares_a_version() -> None:
    assert query_pilot.__version__ == "0.1.0"


def test_runs_on_the_pinned_python() -> None:
    assert sys.version_info[:2] == (3, 12)
