"""Demo fixtures and the loader that validates them through the runtime DTOs."""

from shopping_backend.seeds.loader import SeedReport, count_rows, load_seeds, main

__all__ = ["SeedReport", "count_rows", "load_seeds", "main"]
