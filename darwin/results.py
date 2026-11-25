"""Turn Flagsmith Experimentation results into a readout Darwin (and a human) can reason about."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

CONTROL = "control"
SRM_MIN_TOTAL = 100  # same floor Flagsmith uses before judging the traffic split


@dataclass(frozen=True)
class Arm:
    key: str
    n: int
    total: float
    sum_squares: float

    @property
    def mean(self) -> float:
        return self.total / self.n if self.n else 0.0


@dataclass(frozen=True)
class Inference:
    lift: float
    ci_low: float
    ci_high: float
    chance_to_win: float  # already flipped by Flagsmith for lower-is-better metrics


@dataclass
class MetricReadout:
    metric_id: int
    name: str
    aggregation: str
    direction: str  # up | down | informational
    expected_direction: str
    arms: dict[str, Arm]
    inference: Inference | None

    @property
    def lower_is_better(self) -> bool:
        return self.direction == "down"


@dataclass
class Readout:
    experiment_id: int
    status: str
    challenger: str
    as_of: str | None
    is_final: bool
    last_error_at: str | None
    metrics: dict[str, MetricReadout] = field(default_factory=dict)
    exposures: dict[str, int] = field(default_factory=dict)
    srm_p_value: float | None = None
    srm_source: str | None = None  # "flagsmith" or "darwin"

    @property
    def total_exposed(self) -> int:
        return sum(self.exposures.values())

    def to_dict(self) -> dict:
        d = asdict(self)
        for m in d["metrics"].values():
            for arm in m["arms"].values():
                arm["mean"] = arm["total"] / arm["n"] if arm["n"] else 0.0
        d["total_exposed"] = self.total_exposed
        return d


def chi_squared_p(observed: list[int], expected_shares: list[float]) -> float | None:
    """Sample-ratio-mismatch p-value (Pearson chi-squared), the same test Flagsmith runs."""
    total = sum(observed)
    if len(observed) < 2 or total == 0 or any(s <= 0 for s in expected_shares):
        return None
    stat = sum((o - total * s) ** 2 / (total * s) for o, s in zip(observed, expected_shares))
    if stat <= 0:
        return 1.0
    dof = len(observed) - 1
    y = stat / 2.0
    if dof % 2:
        a, survival = 0.5, math.erfc(math.sqrt(y))
    else:
        a, survival = 1.0, math.exp(-y)
    while a + 1.0 <= dof / 2.0:
        survival += math.exp(a * math.log(y) - y - math.lgamma(a + 1.0))
        a += 1.0
    return survival


def parse_readout(experiment: dict, results: dict | None, *, challenger: str, expected_split: dict[str, float]) -> Readout:
    """`experiment` is Flagsmith's experiment detail; `results` its results object (or None)."""
    names = {m["metric"]: m for m in experiment.get("metrics", [])}
    readout = Readout(
        experiment_id=experiment["id"],
        status=experiment.get("status", "?"),
        challenger=challenger,
        as_of=(results or {}).get("as_of"),
        is_final=bool((results or {}).get("is_final")),
        last_error_at=(results or {}).get("last_error_at"),
    )
    payload = (results or {}).get("payload") or {}
    for m in payload.get("metrics", []):
        meta = names.get(m["metric_id"], {})
        arms = {k: Arm(k, int(v["n"]), float(v["sum"]), float(v["sum_squares"])) for k, v in (m.get("variants") or {}).items()}
        raw = (m.get("inference") or {}).get(challenger)
        name = meta.get("metric_name", str(m["metric_id"]))
        readout.metrics[name] = MetricReadout(
            metric_id=m["metric_id"],
            name=name,
            aggregation=meta.get("aggregation", "?"),
            direction=meta.get("direction", "up"),
            expected_direction=meta.get("expected_direction", "increase"),
            arms=arms,
            inference=Inference(**{k: float(raw[k]) for k in ("lift", "ci_low", "ci_high", "chance_to_win")}) if raw else None,
        )
    for point in (payload.get("exposures_timeseries") or {}).get("points", []):
        for key, count in (point.get("new_identities") or {}).items():
            readout.exposures[key] = readout.exposures.get(key, 0) + int(count)
    if payload.get("srm_p_value") is not None:
        readout.srm_p_value, readout.srm_source = float(payload["srm_p_value"]), "flagsmith"
    elif readout.total_exposed >= SRM_MIN_TOTAL and expected_split:
        # Flagsmith (2.295) reads the expected split from the environment default, which an experiment
        # zeroes, so it reports no SRM. Darwin knows the split it configured and runs the same test.
        keys = list(expected_split)
        p = chi_squared_p([readout.exposures.get(k, 0) for k in keys], [expected_split[k] for k in keys])
        if p is not None:
            readout.srm_p_value, readout.srm_source = p, "darwin"
    return readout
