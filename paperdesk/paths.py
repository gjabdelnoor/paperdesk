"""Portable application paths, shared by source and bundled applications."""
from pathlib import Path
from platformdirs import user_cache_path, user_state_path


def resource_root() -> Path:
    return Path(__file__).resolve().parent


def state_root() -> Path:
    return user_state_path("PaperDesk", appauthor=False)


def cache_root() -> Path:
    return user_cache_path("PaperDesk", appauthor=False)
