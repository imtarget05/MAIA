"""Test that config.py is flat with no delegation (Simplification Step 2)."""
import ast
from pathlib import Path


def test_config_is_flat_no_delegation():
    """Verify config.py has no __getattr__/__setattr__ delegation"""
    content = Path("src/maia/config.py").read_text()
    tree = ast.parse(content)
    
    # Check for actual method definitions, not mentions in docstrings
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            if node.name in ("__getattr__", "__setattr__"):
                raise AssertionError(f"Found {node.name} delegation method")
    
    # Verify no sub-config class definitions
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name != "Settings":
            raise AssertionError(f"Found sub-config class: {node.name}")


def test_all_settings_are_flat_fields():
    """Verify all settings are direct BaseSettings fields"""
    from maia.config import Settings
    fields = set(Settings.model_fields.keys())
    # All fields should be simple types, not nested objects
    for name, field in Settings.model_fields.items():
        # Check for nested config objects (not just list/dict of primitives)
        annotation = field.annotation
        # Allow primitive types, Optional, Union, etc.
        # Disallow nested pydantic models as fields
        if hasattr(annotation, '__origin__'):
            # Handle Optional, Union, etc.
            pass
        elif hasattr(annotation, '__bases__'):
            # This is a class - check if it's a BaseModel
            from pydantic import BaseModel
            if issubclass(annotation, BaseModel):
                raise AssertionError(f"Field {name} is a nested BaseModel: {annotation}")
