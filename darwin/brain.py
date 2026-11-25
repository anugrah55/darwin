"""Brain: the three-line integration an AI app needs to be evolved by Darwin.

    brain = Brain("shopbot_brain", fallback=GEN1, server_key=..., api_url=..., events_url=...)
    a = brain.assign(user_id)            # one Flagsmith call; records the experiment exposure
    ... run your agent with a.genome.system_prompt / .model / .effort ...
    a.track("resolved"); a.track("tokens_used", value=n)

What `assign` serves when Flagsmith can't give a usable genome:

- Flagsmith unreachable (network, auth, outage): the last good genome Brain saw, kept in memory and, with
  `cache_path`, in a small JSON file so a restart doesn't lose it. Only the champion is cached (the control arm
  or the value outside an experiment), never a challenger. With nothing cached yet, the bundled fallback.
- Flag turned off: the bundled fallback. Turning the flag off is the kill switch, so a cached genome is not used.
- Flag missing: the bundled fallback.
- Value isn't a valid genome (a bad edit): the last good genome, else the bundled fallback.

The app keeps working in every case, and the assignment says what it served and why.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .genome import Genome, GenomeError

log = logging.getLogger("darwin.brain")


@dataclass
class Assignment:
    identifier: str
    genome: Genome
    variant: str  # "control", a challenger key such as "gen3", "fallback" or "last-good"
    source: str  # "flag", "cache" (last good genome) or "fallback" (bundled)
    experiment_id: int | None = None
    experiment_name: str | None = None
    in_experiment: bool = False
    reason: str = ""
    _brain: Brain | None = field(default=None, repr=False, compare=False)

    def track(self, event: str, value: Any = None) -> None:
        if self._brain is not None:
            self._brain.track(self.identifier, event, value)

    def to_dict(self) -> dict:
        return {
            "identifier": self.identifier,
            "generation": self.genome.name,
            "variant": self.variant,
            "source": self.source,
            "experiment_id": self.experiment_id,
            "experiment_name": self.experiment_name,
            "in_experiment": self.in_experiment,
            "reason": self.reason,
        }


class Brain:
    def __init__(self, flag: str, *, fallback: Genome, server_key: str | None = None, api_url: str | None = None,
                 events_url: str | None = None, flush_interval_seconds: float = 5.0, client: Any = None,
                 cache_path: Path | str | None = None):
        self.flag = flag
        self.fallback = fallback
        self.cache_path = Path(cache_path) if cache_path else None
        self._last_good: Genome | None = self._load_cache()
        if client is None:
            from flagsmith import Flagsmith
            from flagsmith.analytics import EventProcessorConfig

            if not server_key:
                raise ValueError("Brain needs a Flagsmith server-side SDK key (FLAGSMITH_SERVER_KEY)")
            kwargs: dict[str, Any] = {"environment_key": server_key, "enable_events": True}
            if api_url:
                kwargs["api_url"] = api_url
            config = {"flush_interval_seconds": flush_interval_seconds}
            if events_url:
                config["events_api_url"] = events_url
            kwargs["event_processor_config"] = EventProcessorConfig(**config)
            client = Flagsmith(**kwargs)
        self._fs = client

    # ---------- last good genome ----------
    def _load_cache(self) -> Genome | None:
        if not self.cache_path or not self.cache_path.exists():
            return None
        try:
            data = json.loads(self.cache_path.read_text())
            return Genome.from_flag_value(data["value"]) if data.get("flag") == self.flag else None
        except (OSError, ValueError, KeyError, GenomeError) as exc:
            log.warning("ignoring unreadable genome cache %s: %s", self.cache_path, exc)
            return None

    def _remember(self, genome: Genome) -> None:
        if genome == self._last_good:
            return
        self._last_good = genome
        if not self.cache_path:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"flag": self.flag, "value": genome.to_flag_value(), "saved_at": time.time()}))
            tmp.replace(self.cache_path)
        except OSError as exc:
            log.warning("could not save the last good genome to %s: %s", self.cache_path, exc)

    @property
    def last_good(self) -> Genome | None:
        return self._last_good

    # ---------- assignment ----------
    def assign(self, identifier: str, traits: dict | None = None) -> Assignment:
        def fallback(reason: str) -> Assignment:
            log.warning("serving fallback genome %s to %s: %s", self.fallback.name, identifier, reason)
            return Assignment(identifier, self.fallback, "fallback", "fallback", reason=reason, _brain=self)

        def last_good(reason: str) -> Assignment:
            if self._last_good is None:
                return fallback(f"{reason}; no last good genome yet")
            log.warning("serving last good genome %s to %s: %s", self._last_good.name, identifier, reason)
            return Assignment(identifier, self._last_good, "last-good", "cache", reason=reason, _brain=self)

        try:
            flag = self._fs.get_experiment_flag(self.flag, identifier=identifier, traits=traits)
        except Exception as exc:  # network, auth, Flagsmith down: never take the app down with it
            return last_good(f"Flagsmith unreachable ({type(exc).__name__}: {exc})")
        if getattr(flag, "is_default", False):
            return fallback(f"flag {self.flag} not found")
        if not flag.enabled:
            return fallback(f"flag {self.flag} is off (kill switch)")
        try:
            genome = Genome.from_flag_value(flag.value)
        except GenomeError as exc:
            return last_good(str(exc))
        experiment = getattr(flag, "experiment", None)
        assignment = Assignment(
            identifier=identifier,
            genome=genome,
            variant=getattr(flag, "variant", None) or "control",
            source="flag",
            experiment_id=getattr(experiment, "id", None),
            experiment_name=getattr(experiment, "name", None),
            in_experiment=bool(experiment and experiment.in_experiment),
            _brain=self,
        )
        if not assignment.in_experiment or assignment.variant == "control":
            self._remember(genome)  # the champion: what everyone outside the experiment gets
        return assignment

    def track(self, identifier: str, event: str, value: Any = None) -> None:
        try:
            self._fs.track_event(event, identifier=identifier, value=value)
        except Exception as exc:
            log.warning("could not track %s for %s: %s", event, identifier, exc)

    def flush(self) -> None:
        flush = getattr(self._fs, "flush_events", None)
        if callable(flush):
            flush()

    def close(self) -> None:
        close = getattr(self._fs, "close", None)
        if callable(close):
            close()
