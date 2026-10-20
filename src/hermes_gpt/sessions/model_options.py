"""Validated per-turn model choices for managed Hermes tasks."""

from __future__ import annotations

import re
from dataclasses import dataclass

REASONING_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+/-]*")


@dataclass(frozen=True)
class ModelOverrides:
    """Explicit choices only; None lets Hermes resolve its profile default."""

    model: str | None = None
    reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        if self.model is not None:
            if (
                not isinstance(self.model, str)
                or len(self.model) > 256
                or not _MODEL_ID.fullmatch(self.model)
            ):
                raise ValueError("model must be a valid provider/model ID")
            provider, separator, model_name = self.model.partition("/")
            if not separator or not provider or not model_name:
                raise ValueError("model must use provider/model syntax")
        if self.reasoning_effort is not None and (
            not isinstance(self.reasoning_effort, str)
            or self.reasoning_effort not in REASONING_EFFORTS
        ):
            choices = ", ".join(sorted(REASONING_EFFORTS))
            raise ValueError(f"reasoning_effort must be one of: {choices}")
