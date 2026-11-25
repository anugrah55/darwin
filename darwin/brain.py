"""Brain: the three-line integration an AI app needs to be evolved by Darwin.

    brain = Brain("shopbot_brain", fallback=GEN1, server_key=..., api_url=..., events_url=...)
    a = brain.assign(user_id)            # one Flagsmith call; records the experiment exposure
    ... run your agent with a.genome.system_prompt / .model / .effort ...
    a.track("resolved"); a.track("tokens_used", value=n)

If Flagsmith is unreachable, the flag is off, or its value isn't a valid genome, `assign` returns the
fallback genome and says why — the app keeps working.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .genome import Genome, GenomeError

log = logging.getLogger("darwin.brain")


@dataclass
class Assignment:
    identifier: str
    genome: Genome
    variant: str  # "control", a challenger key such as "gen3", or "fallback"
    source: str  # "flag" or "fallback"
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
                 events_url: str | None = None, flush_interval_seconds: float = 5.0, client: Any = None):
        self.flag = flag
        self.fallback = fallback
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

    def assign(self, identifier: str, traits: dict | None = None) -> Assignment:
        def fallback(reason: str) -> Assignment:
            log.warning("serving fallback genome %s to %s: %s", self.fallback.name, identifier, reason)
            return Assignment(identifier, self.fallback, "fallback", "fallback", reason=reason, _brain=self)

        try:
            flag = self._fs.get_experiment_flag(self.flag, identifier=identifier, traits=traits)
        except Exception as exc:  # network, auth, Flagsmith down: never take the app down with it
            return fallback(f"flag unavailable ({type(exc).__name__}: {exc})")
        if getattr(flag, "is_default", False):
            return fallback(f"flag {self.flag} not found")
        if not flag.enabled:
            return fallback(f"flag {self.flag} is off")
        try:
            genome = Genome.from_flag_value(flag.value)
        except GenomeError as exc:
            return fallback(str(exc))
        experiment = getattr(flag, "experiment", None)
        return Assignment(
            identifier=identifier,
            genome=genome,
            variant=getattr(flag, "variant", None) or "control",
            source="flag",
            experiment_id=getattr(experiment, "id", None),
            experiment_name=getattr(experiment, "name", None),
            in_experiment=bool(experiment and experiment.in_experiment),
            _brain=self,
        )

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
