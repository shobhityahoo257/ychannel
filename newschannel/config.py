from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    """Minimal .env loader (no extra dependency). Existing env vars win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class Format:
    width: int
    height: int
    fps: int
    target_seconds: int
    max_scenes: int
    captions: bool
    ticker: bool
    name: str = ""

    @property
    def portrait(self) -> bool:
        return self.height > self.width


class Config:
    def __init__(self, data: dict[str, Any], root: Path = ROOT):
        self.data = data
        self.root = root

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        load_dotenv(ROOT / ".env")
        p = Path(path) if path else ROOT / "config.yaml"
        return cls(yaml.safe_load(p.read_text(encoding="utf-8")), p.parent)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.root / p

    def fmt(self, name: str) -> Format:
        return Format(name=name, **self.data["formats"][name])

    @staticmethod
    def env(name: str, required: bool = False) -> str:
        v = os.environ.get(name, "")
        if required and not v:
            raise RuntimeError(f"Missing environment variable {name} (see .env.example)")
        return v
