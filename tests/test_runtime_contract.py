"""Keep installer metadata, lint target and setup instructions in agreement."""

import ast
import tomllib
from pathlib import Path

from packaging.specifiers import SpecifierSet

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_metadata_rejects_python_without_strenum():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    supported = SpecifierSet(config["project"]["requires-python"])
    assert "3.10.16" not in supported
    assert "3.11.0" in supported
    assert "3.12.0" in supported
    assert config["tool"]["ruff"]["target-version"] == "py311"


def test_source_parses_with_declared_minimum_grammar():
    # Syntax compatibility only; this does not replace running on Python 3.11.
    for path in (ROOT / "src").rglob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 11))


def test_setup_docs_do_not_recommend_unsupported_python():
    paths = [ROOT / "README.md", *(ROOT / "docs").glob("*.md")]
    for path in paths:
        content = path.read_text(encoding="utf-8")
        assert "py -3.10" not in content, path
        assert "Python 3.10 以上" not in content, path
