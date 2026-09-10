"""Test that no main source file imports from maia.stream at module level (archived per Simplification Step 1)."""
import ast
from pathlib import Path


def test_no_stream_imports_in_main_source():
    """Verify no main source file imports from maia.stream at module level"""
    src = Path("src/maia")
    for py_file in src.rglob("*.py"):
        if "_archive" in str(py_file):
            continue
        tree = ast.parse(py_file.read_text())
        # Only check module-level imports (direct children of Module node)
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("maia.stream"):
                    raise AssertionError(f"{py_file}: imports {node.module} at module level")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("maia.stream"):
                        raise AssertionError(f"{py_file}: imports {alias.name} at module level")
