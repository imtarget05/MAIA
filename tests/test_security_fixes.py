"""Test that security fixes are complete (Simplification Step 3)."""
import ast
from pathlib import Path


def test_no_pickle_usage():
    """Verify no pickle usage in source (only comment/docstring in retriever.py)"""
    src = Path("src/maia")
    # Patterns that indicate actual pickle usage (not just mention in comments/docstrings)
    pickle_patterns = [
        "import pickle",
        "from pickle import",
        "pickle.load",
        "pickle.dump",
        "pickle.loads",
        "pickle.dumps",
    ]
    for py_file in src.rglob("*.py"):
        content = py_file.read_text()
        lines = content.split('\n')
        for i, line in enumerate(lines):
            stripped = line.strip()
            # Check if line contains actual pickle usage patterns
            for pattern in pickle_patterns:
                if pattern in line:
                    # Allow if it's in a comment
                    if not stripped.startswith('#'):
                        raise AssertionError(f"{py_file}:{i+1}: potential pickle usage: {line.strip()}")


def test_output_validator_is_alias():
    """Verify OutputValidator is just an alias of OutputGuardrail"""
    from maia.loops.guardrails import OutputValidator, OutputGuardrail
    assert OutputValidator is OutputGuardrail, "OutputValidator not aliased to OutputGuardrail"


def test_no_output_validator_instantiation():
    """Verify no code instantiates OutputValidator directly"""
    src = Path("src/maia")
    for py_file in src.rglob("*.py"):
        content = py_file.read_text()
        if "OutputValidator(" in content and "class OutputValidator" not in content:
            raise AssertionError(f"{py_file}: instantiates OutputValidator")
