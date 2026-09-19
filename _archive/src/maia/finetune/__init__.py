"""Fine-tune groundwork: golden->triplet export (offline) + GPU train runner."""
from .export import build_triplets, export_triplets, load_goldens
from .train import check_deps, train

__all__ = ["build_triplets", "export_triplets", "load_goldens", "check_deps", "train"]
