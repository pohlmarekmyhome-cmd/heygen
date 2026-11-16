"""Load the TOML config (see config.example.toml)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from .captions import CaptionStyle


@dataclass
class Config:
    languages: list[str]
    mode: str = "precision"  # "precision" = lip-sync re-render, "speed" = voice dub only
    api_key_env: str = "HEYGEN_API_KEY"
    request_options: dict = field(default_factory=dict)  # extra fields for the create request
    poll_interval_seconds: int = 30
    timeout_minutes: int = 120
    output_dir: Path = Path("output")
    crf: int = 18
    preset: str = "medium"
    burn_captions: bool = True
    captions: CaptionStyle = field(default_factory=CaptionStyle)


def load_config(path: Path | None) -> Config:
    raw: dict = {}
    if path:
        with open(path, "rb") as f:
            raw = tomllib.load(f)

    heygen = raw.get("heygen", {})
    output = raw.get("output", {})
    cap_raw = dict(raw.get("captions", {}))
    known = {f.name for f in fields(CaptionStyle)}
    unknown = set(cap_raw) - known
    if unknown:
        raise ValueError(f"Unknown [captions] keys in {path}: {sorted(unknown)}")

    return Config(
        languages=list(raw.get("languages", [])),
        mode=heygen.get("mode", "precision"),
        api_key_env=heygen.get("api_key_env", "HEYGEN_API_KEY"),
        request_options=dict(heygen.get("request_options", {})),
        poll_interval_seconds=int(heygen.get("poll_interval_seconds", 30)),
        timeout_minutes=int(heygen.get("timeout_minutes", 120)),
        output_dir=Path(output.get("dir", "output")),
        crf=int(output.get("crf", 18)),
        preset=output.get("preset", "medium"),
        burn_captions=bool(output.get("burn_captions", True)),
        captions=CaptionStyle(**cap_raw),
    )
