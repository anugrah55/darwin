"""A genome is an AI agent's whole configuration — the thing Darwin evolves.

It is stored as the JSON value of a Flagsmith multivariate flag: the flag's control value is the current
champion, and each challenger is a variant. Changing an agent's behaviour is a flag change, not a deploy.
"""

from __future__ import annotations

import difflib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# Flagsmith's default FEATURE_VALUE_LIMIT. The serialised genome must fit in one flag value.
FLAG_VALUE_LIMIT = 20_000
MAX_PROMPT_CHARS = 12_000

# Models the Coach may choose between. A team can widen this; Darwin only refuses what isn't listed.
ALLOWED_MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5")
Effort = Literal["low", "medium", "high"]


class GenomeError(ValueError):
    pass


class Genome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(description="Generation key, e.g. 'gen3'. Also the Flagsmith variant key.")
    generation: int = Field(ge=1)
    parent: str | None = None
    model: str
    effort: Effort = "medium"
    max_tokens: int = Field(default=2048, ge=256, le=16_000)
    system_prompt: str
    summary: str = Field(default="", description="One line: what this generation changed.")

    @field_validator("system_prompt")
    @classmethod
    def _prompt_size(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("system_prompt is empty")
        if len(value) > MAX_PROMPT_CHARS:
            raise ValueError(f"system_prompt is {len(value)} chars; the limit is {MAX_PROMPT_CHARS}")
        return value

    def to_flag_value(self) -> str:
        value = json.dumps(self.model_dump(), ensure_ascii=False, separators=(",", ":"))
        if len(value) > FLAG_VALUE_LIMIT:
            raise GenomeError(f"genome {self.name} is {len(value)} chars; a flag value holds {FLAG_VALUE_LIMIT}")
        return value

    @classmethod
    def from_flag_value(cls, value: object) -> Genome:
        if not isinstance(value, str) or not value.strip():
            raise GenomeError("flag value is empty or not a string")
        try:
            return cls.model_validate(json.loads(value))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise GenomeError(f"flag value is not a valid genome: {exc}") from exc

    def child(self, *, name: str, system_prompt: str, model: str | None = None, effort: Effort | None = None,
              summary: str = "") -> Genome:
        return Genome(
            name=name,
            generation=self.generation + 1,
            parent=self.name,
            model=model or self.model,
            effort=effort or self.effort,
            max_tokens=self.max_tokens,
            system_prompt=system_prompt,
            summary=summary,
        )

    def diff(self, other: Genome) -> dict:
        """What changes going from self (parent) to other (child): settings and a unified prompt diff."""
        settings = {
            key: {"from": getattr(self, key), "to": getattr(other, key)}
            for key in ("model", "effort", "max_tokens")
            if getattr(self, key) != getattr(other, key)
        }
        lines = list(
            difflib.unified_diff(
                self.system_prompt.splitlines(),
                other.system_prompt.splitlines(),
                fromfile=self.name,
                tofile=other.name,
                lineterm="",
                n=1,
            )
        )
        added = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
        return {"settings": settings, "prompt_diff": lines, "lines_added": added, "lines_removed": removed}
