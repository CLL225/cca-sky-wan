"""Runtime configuration.

The knowledge base deliberately keeps configuration small: a project root,
an embedder choice, and the two hybrid-ranking weights.  Every value can be
overridden by an environment variable so that the same package works from a
container, a CI job, or an embedding agent framework.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def _detect_root(explicit: str | Path | None = None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser().resolve()

    env_root = os.environ.get("CCA_KB_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / "knowledge").is_dir() and (candidate / "schemas").is_dir():
            return candidate

    return PACKAGE_ROOT


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Resolved paths and retrieval defaults."""

    root: Path
    embedder: str = "auto"
    model_name: str = "intfloat/multilingual-e5-small"
    keyword_weight: float = 0.55
    semantic_weight: float = 0.45

    @classmethod
    def from_env(cls, root: str | Path | None = None) -> "Settings":
        return cls(
            root=_detect_root(root),
            embedder=os.environ.get("CCA_KB_EMBEDDER", "auto"),
            model_name=os.environ.get(
                "CCA_KB_MODEL", "intfloat/multilingual-e5-small"
            ),
            keyword_weight=_env_float("CCA_KB_KEYWORD_WEIGHT", 0.55),
            semantic_weight=_env_float("CCA_KB_SEMANTIC_WEIGHT", 0.45),
        )

    @property
    def knowledge_dir(self) -> Path:
        return self.root / "knowledge"

    @property
    def experiences_dir(self) -> Path:
        return self.knowledge_dir / "experiences"

    @property
    def sources_dir(self) -> Path:
        return self.knowledge_dir / "sources"

    @property
    def taxonomy_dir(self) -> Path:
        return self.knowledge_dir / "taxonomy"

    @property
    def relations_dir(self) -> Path:
        return self.knowledge_dir / "relations"

    @property
    def schemas_dir(self) -> Path:
        return self.root / "schemas"
