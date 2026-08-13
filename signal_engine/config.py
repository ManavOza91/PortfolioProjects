"""Configuration loading.

Everything that shapes behaviour comes from config.yaml and data/taxonomy.yaml.
Nothing here or downstream hardcodes a company, signal type or weight.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("SIGNAL_ENGINE_CONFIG", PROJECT_ROOT / "config.yaml"))
TAXONOMY_PATH = Path(
    os.environ.get("SIGNAL_ENGINE_TAXONOMY", PROJECT_ROOT / "data" / "taxonomy.yaml")
)


def _load_dotenv() -> None:
    """Read a .env file next to config.yaml, without adding a dependency.

    Existing environment variables always win, so `ANTHROPIC_API_KEY=... ./run.sh`
    overrides the file.
    """
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class TierRule:
    name: str
    min_score: float


@dataclass
class Config:
    raw: dict[str, Any]
    path: Path

    # --- convenience accessors -------------------------------------------------

    @property
    def user_name(self) -> str:
        return str(self.raw["user"]["name"])

    @property
    def tenant_id(self) -> int:
        return int(self.raw["user"].get("tenant_id", 1))

    @property
    def products(self) -> dict[str, dict[str, str]]:
        return self.raw.get("products", {})

    @property
    def scoring(self) -> dict[str, Any]:
        return self.raw["scoring"]

    @property
    def decay(self) -> dict[str, Any]:
        return self.scoring["decay"]

    @property
    def compounding(self) -> dict[str, Any]:
        return self.scoring["compounding"]

    @property
    def tiers(self) -> list[TierRule]:
        rules = [TierRule(t["name"], float(t["min_score"])) for t in self.scoring["tiers"]]
        # Highest threshold first so the first match wins.
        return sorted(rules, key=lambda r: r.min_score, reverse=True)

    @property
    def auto_review_threshold(self) -> float:
        return float(self.scoring.get("auto_review_threshold", 0.7))

    @property
    def manual_default_confidence(self) -> float:
        return float(self.scoring.get("manual_default_confidence", 0.9))

    @property
    def buddy(self) -> dict[str, Any]:
        return self.raw["buddy_score"]

    @property
    def llm(self) -> dict[str, Any]:
        return self.raw.get("llm", {})

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm.get("enabled", True)) and bool(os.environ.get("ANTHROPIC_API_KEY"))

    @property
    def detection(self) -> dict[str, Any]:
        return self.raw.get("detection", {})

    @property
    def icp(self) -> dict[str, Any]:
        return self.raw.get("icp", {})

    @property
    def server(self) -> dict[str, Any]:
        return self.raw.get("server", {})

    @property
    def database_url(self) -> str:
        url = os.environ.get("SIGNAL_ENGINE_DATABASE_URL") or self.raw["database"]["url"]
        # Resolve a relative sqlite path against the project root so the app can be
        # launched from any working directory.
        prefix = "sqlite:///"
        if url.startswith(prefix) and not url.startswith(prefix + "/"):
            rel = url[len(prefix) :]
            resolved = (PROJECT_ROOT / rel).resolve()
            resolved.parent.mkdir(parents=True, exist_ok=True)
            return f"{prefix}{resolved}"
        return url


@dataclass
class SignalTypeSpec:
    """One row of the taxonomy, as authored in data/taxonomy.yaml."""

    key: str
    label: str
    product_fit: str
    base_weight: float
    sources: str
    decays: bool
    description: str
    examples: list[str] = field(default_factory=list)


@dataclass
class Taxonomy:
    version: int
    product_fit_labels: dict[str, str]
    types: list[SignalTypeSpec]

    def by_key(self, key: str) -> SignalTypeSpec | None:
        for t in self.types:
            if t.key == key:
                return t
        return None


@lru_cache(maxsize=1)
def get_config() -> Config:
    _load_dotenv()
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Config file not found at {CONFIG_PATH}. "
            "Copy config.yaml from the project root, or set SIGNAL_ENGINE_CONFIG."
        )
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    _validate(data)
    return Config(raw=data, path=CONFIG_PATH)


def _validate(data: dict[str, Any]) -> None:
    """Fail loudly at startup rather than subtly at scoring time."""
    for section in ("user", "scoring", "buddy_score", "database"):
        if section not in data:
            raise ValueError(f"config.yaml is missing the required '{section}' section")

    components = data["buddy_score"].get("components", {})
    total = sum(float(v) for v in components.values())
    if abs(total - 100.0) > 0.001:
        raise ValueError(
            f"buddy_score.components must sum to 100, got {total}. "
            f"Components: {components}"
        )

    if not data["scoring"].get("tiers"):
        raise ValueError("config.yaml scoring.tiers must define at least one tier")

    decay = data["scoring"].get("decay", {})
    floor = float(decay.get("floor", 0.4))
    if not 0 < floor <= 1:
        raise ValueError(
            f"scoring.decay.floor must be between 0 (exclusive) and 1, got {floor}. "
            "A signal must never decay to zero."
        )


@lru_cache(maxsize=1)
def get_taxonomy() -> Taxonomy:
    if not TAXONOMY_PATH.exists():
        raise FileNotFoundError(f"Taxonomy file not found at {TAXONOMY_PATH}")
    data = yaml.safe_load(TAXONOMY_PATH.read_text(encoding="utf-8")) or {}
    types = [
        SignalTypeSpec(
            key=t["key"],
            label=t["label"],
            product_fit=str(t.get("product_fit", "both")),
            base_weight=float(t["base_weight"]),
            sources=str(t.get("sources", "both")),
            decays=bool(t.get("decays", True)),
            description=" ".join(str(t.get("description", "")).split()),
            examples=list(t.get("examples", []) or []),
        )
        for t in data.get("signal_types", [])
    ]
    keys = [t.key for t in types]
    duplicates = {k for k in keys if keys.count(k) > 1}
    if duplicates:
        raise ValueError(f"Duplicate signal type keys in taxonomy.yaml: {sorted(duplicates)}")
    return Taxonomy(
        version=int(data.get("version", 1)),
        product_fit_labels=data.get("product_fit_labels", {}),
        types=types,
    )


def reset_caches() -> None:
    """Used by tests and by the reseed command."""
    get_config.cache_clear()
    get_taxonomy.cache_clear()
