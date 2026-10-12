"""Configuration shared by the UI and device services."""
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DeviceConfig:
    host: str = "10.11.99.1"
    port: int = 22
    username: str = "root"
    key_path: str = ""
    password: str = field(default="", repr=False)
    state_dir: Path | None = None
