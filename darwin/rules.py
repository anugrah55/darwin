"""Fixed, explainable decision rules. Flagsmith's Bayesian engine scores; these rules route; a human approves.

Thresholds follow Flagsmith's own statistics glossary: 95% chance to win to call a winner, 5% to call a
loser, and a sample-ratio-mismatch p-value under 0.001 means the split is broken and results can't be
trusted.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .results import CONTROL, MetricReadout, Readout

COLLECTING_DATA = "COLLECTING_DATA"
HOLD = "HOLD"
KILL = "KILL"
SHIP = "SHIP"
DISCARD = "DISCARD"
KEEP_RUNNING = "KEEP_RUNNING"


@dataclass(frozen=True)
class Guardrail:
    metric: str
    tolerance: float = 0.0  # relative change allowed in the bad direction before it counts as a breach
    label: str = ""


@dataclass(frozen=True)
class DecisionPolicy:
    primary: str = "resolved"
    guardrails: tuple[Guardrail, ...] = (
        Guardrail("escalated", 0.0, "escalations to a human"),
        Guardrail("cost_microusd", 0.30, "cost per chat"),
    )
    ship_at: float = 0.95
    discard_at: float = 0.05
    srm_alpha: float = 0.001


@dataclass
class GuardrailStatus:
    metric: str
    label: str
    state: str  # ok | watch | breach | no_data
    text: str


@dataclass
class Decision:
    verdict: str
    headline: str
    reasons: list[str] = field(default_factory=list)
    guardrails: list[GuardrailStatus] = field(default_factory=list)
    rollout_decision: str | None = None  # what `complete` would send: "variant" or "control"

    @property
    def needs_approval(self) -> bool:
        return self.verdict in (SHIP, DISCARD, KILL)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["needs_approval"] = self.needs_approval
        return d


def _pct(x: float) -> str:
    return f"{x * 100:+.0f}%"


def _rate(metric: MetricReadout, arm: str) -> str:
    a = metric.arms.get(arm)
    if not a or not a.n:
        return "–"
    return f"{a.mean * 100:.0f}%" if metric.aggregation == "occurrence" else f"{a.mean:,.0f}"


def _guardrail(readout: Readout, g: Guardrail) -> GuardrailStatus:
    label = g.label or g.metric
    m = readout.metrics.get(g.metric)
    if m is None or m.inference is None:
        return GuardrailStatus(g.metric, label, "no_data", f"{label}: not enough data yet")
    inf = m.inference
    worse_lift = inf.lift if m.lower_is_better else -inf.lift
    worse_bound = inf.ci_low if m.lower_is_better else -inf.ci_high  # the side that proves harm
    ci = f"95% CI {_pct(inf.ci_low)} … {_pct(inf.ci_high)}"
    if worse_bound > g.tolerance:
        allowed = f", over the {g.tolerance * 100:.0f}% allowed" if g.tolerance else ""
        return GuardrailStatus(g.metric, label, "breach", f"{label}: {_pct(inf.lift)} ({ci}){allowed}")
    if worse_lift > g.tolerance and (1 - inf.chance_to_win) >= 0.8:
        return GuardrailStatus(g.metric, label, "watch", f"{label}: {_pct(inf.lift)} ({ci}), probably worse")
    return GuardrailStatus(g.metric, label, "ok", f"{label}: {_pct(inf.lift)} ({ci})")


def decide(readout: Readout | None, policy: DecisionPolicy = DecisionPolicy(), *, champion: str = "champion") -> Decision:
    if readout is None or readout.as_of is None:
        reason = "Flagsmith hasn't computed results yet."
        if readout is not None and readout.last_error_at:
            reason = "Flagsmith's last results refresh failed; it will retry on the next refresh."
        return Decision(COLLECTING_DATA, "Waiting for the first results.", [reason])

    challenger = readout.challenger
    primary = readout.metrics.get(policy.primary)
    guardrails = [_guardrail(readout, g) for g in policy.guardrails]

    if primary is None or primary.inference is None:
        arms = primary.arms if primary else {}
        counts = ", ".join(f"{k}: {a.n} chats / {a.total:.0f} resolved" for k, a in sorted(arms.items())) or "no traffic yet"
        return Decision(COLLECTING_DATA, "Collecting data.",
                        [f"Flagsmith shows a result once each arm has 50 identities and 5 conversions ({counts})."],
                        guardrails)

    if readout.srm_p_value is not None and readout.srm_p_value < policy.srm_alpha:
        split = ", ".join(f"{k} {v}" for k, v in sorted(readout.exposures.items()))
        return Decision(HOLD, "Traffic split looks broken; don't trust these results.",
                        [f"Sample ratio mismatch: p = {readout.srm_p_value:.2g} ({split}). Check assignment before deciding."],
                        guardrails)

    inf = primary.inference
    vs = f"{_rate(primary, challenger)} vs {_rate(primary, CONTROL)} for {champion}"
    stats = f"lift {_pct(inf.lift)}, 95% CI {_pct(inf.ci_low)} … {_pct(inf.ci_high)}, {inf.chance_to_win:.1%} chance to win"
    reasons = [f"{policy.primary}: {challenger} {vs} ({stats})"] + [g.text for g in guardrails]

    breaches = [g for g in guardrails if g.state == "breach"]
    if breaches:
        return Decision(KILL, f"Kill {challenger}: it makes {breaches[0].label} worse.", reasons, guardrails, "control")
    if inf.chance_to_win >= policy.ship_at:
        return Decision(SHIP, f"Ship {challenger}: {vs}, {inf.chance_to_win:.1%} chance to win.", reasons, guardrails, "variant")
    if inf.chance_to_win <= policy.discard_at:
        return Decision(DISCARD, f"Discard {challenger}: it does worse than {champion} ({inf.chance_to_win:.1%} chance to win).",
                        reasons, guardrails, "control")
    return Decision(KEEP_RUNNING, f"No clear winner yet ({inf.chance_to_win:.0%} chance {challenger} is better). Keep running.",
                    reasons, guardrails)
