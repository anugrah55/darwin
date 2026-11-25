"""Fixed, explainable decision rules. Flagsmith's Bayesian engine scores; these rules route; a human approves.

The primary metric decides who is better: 95% chance to win to call a winner and 5% to call a loser, the
thresholds in Flagsmith's own statistics glossary. A sample-ratio-mismatch p-value under 0.001 means the
traffic split is broken and nothing is decided.

Guardrails are non-inferiority checks. Each has a margin (how much worse is tolerated) and the direction that
counts as worse. Darwin computes p_bad, the probability that the challenger is worse than the margin:

- from Flagsmith's inference when there is one: the relative change is treated as Normal(lift, se), with the
  standard error read off Flagsmith's 95% interval;
- otherwise, for occurrence metrics, from the raw counts: a Beta(1 + conversions, 1 + misses) posterior per
  arm, compared by seeded Monte Carlo. Flagsmith shows nothing until each arm has 5 conversions, and a rare
  event such as an escalation can stay under that floor while it triples.

A guardrail is `ok` when p_bad <= 5%, a `breach` when p_bad >= 95%, `uncertain` in between, and `no_data` when
there is nothing to compute from. A challenger ships only when it wins and every guardrail is proven `ok`.
Any breach recommends a kill, even before the planned sample is reached.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from statistics import NormalDist

from .results import CONTROL, MetricReadout, Readout

COLLECTING_DATA = "COLLECTING_DATA"
HOLD = "HOLD"
KILL = "KILL"
SHIP = "SHIP"
DISCARD = "DISCARD"
KEEP_RUNNING = "KEEP_RUNNING"

Z95 = 1.959963984540054
DRAWS = 20_000
SEED = 20261126
_NORMAL = NormalDist()


@dataclass(frozen=True)
class Guardrail:
    metric: str
    margin: float  # how much worse is tolerated: rate points for "points" (0.05 = 5 pts), a fraction for "relative"
    worse: str = "up"  # "up" when a rise is bad (escalations, cost), "down" when a fall is bad (resolution)
    scale: str = "points"  # "points" (absolute; for rates, which are small) or "relative"
    label: str = ""
    plural: bool = False  # "escalations aren't proven safe" vs "cost per chat isn't"

    @property
    def name(self) -> str:
        return self.label or self.metric

    def margin_text(self) -> str:
        amount = f"{self.margin * 100:g} pts" if self.scale == "points" else f"{self.margin * 100:g}%"
        return amount

    def describe(self) -> str:
        """The rule in words, for launch cards: 'escalations may not rise more than 5 pts'."""
        return f"{self.name} may not {'rise' if self.worse == 'up' else 'fall'} more than {self.margin_text()}"


@dataclass(frozen=True)
class DecisionPolicy:
    primary: str = "resolved"
    guardrails: tuple[Guardrail, ...] = (
        Guardrail("escalated", 0.05, "up", "points", "escalations", plural=True),
        Guardrail("cost_microusd", 0.30, "up", "relative", "cost per chat"),
    )
    ship_at: float = 0.95
    discard_at: float = 0.05
    srm_alpha: float = 0.001
    breach_at: float = 0.95  # p_bad at or above this: the guardrail is breached
    safe_at: float = 0.05  # p_bad at or below this: the guardrail is proven safe
    min_per_arm: int = 100  # planned chats per arm before Darwin calls a winner or a loser


@dataclass
class GuardrailStatus:
    metric: str
    label: str
    state: str  # ok | uncertain | breach | no_data
    text: str
    p_bad: float | None = None  # probability the challenger is worse than the margin
    margin: str = ""  # e.g. "+5 pts" or "+30%"
    source: str | None = None  # "flagsmith" (its inference) or "counts" (Beta posteriors on raw counts)


@dataclass
class Decision:
    verdict: str
    headline: str
    reasons: list[str] = field(default_factory=list)
    guardrails: list[GuardrailStatus] = field(default_factory=list)
    rollout_decision: str | None = None  # what `complete` would send: "variant" or "control"
    min_per_arm: int = 0
    smallest_arm: int = 0

    @property
    def needs_approval(self) -> bool:
        return self.verdict in (SHIP, DISCARD, KILL)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["needs_approval"] = self.needs_approval
        return d


def _pct(x: float) -> str:
    return f"{x * 100:+.0f}%"


def prob(p: float) -> str:
    """0.0002 -> '<0.1%', 0.003 -> '0.3%', 0.11 -> '11%', 0.996 -> '99.6%', 0.9999 -> '>99.9%'."""
    if p < 0.001:
        return "<0.1%"
    if p > 0.999:
        return ">99.9%"
    return f"{p:.1%}" if p < 0.01 or p > 0.99 else f"{p:.0%}"


def _rate(metric: MetricReadout, arm: str) -> str:
    a = metric.arms.get(arm)
    if not a or not a.n:
        return "–"
    if metric.aggregation == "occurrence":
        return f"{a.mean * 100:.0f}%"
    if metric.name.endswith("_microusd"):
        return f"${a.mean / 1_000_000:.5f}"
    return f"{a.mean:,.0f}"


LABELS = {"resolved": "resolved", "escalated": "handed to a human", "cost_microusd": "cost per chat"}
WINS = {"resolved": "resolves more chats", "cost_microusd": "is cheaper", "escalated": "escalates less"}


# ---------------------------------------------------------------- p_bad
def p_bad_from_inference(lift: float, ci_low: float, ci_high: float, g: Guardrail, control_mean: float | None) -> float | None:
    """P(challenger worse than the margin), with the relative change ~ Normal(lift, se) from Flagsmith's 95% interval."""
    if g.scale == "points":
        if not control_mean:
            return None
        threshold = g.margin / control_mean  # the relative change that equals the margin in rate points
    else:
        threshold = g.margin
    se = (ci_high - ci_low) / (2 * Z95)
    if se <= 0:
        bad = lift > threshold if g.worse == "up" else lift < -threshold
        return 1.0 if bad else 0.0
    if g.worse == "up":
        return 1.0 - _NORMAL.cdf((threshold - lift) / se)
    return _NORMAL.cdf((-threshold - lift) / se)


@lru_cache(maxsize=1024)
def p_bad_from_counts(n_control: int, hits_control: int, n_challenger: int, hits_challenger: int, margin: float,
                      worse: str = "up", scale: str = "points", draws: int = DRAWS, seed: int = SEED) -> float:
    """P(challenger worse than the margin) from raw counts: Beta(1 + hits, 1 + misses) per arm, seeded Monte Carlo."""
    hits_control, hits_challenger = min(hits_control, n_control), min(hits_challenger, n_challenger)
    rng = random.Random(seed)
    a_c, b_c = 1 + hits_control, 1 + n_control - hits_control
    a_t, b_t = 1 + hits_challenger, 1 + n_challenger - hits_challenger
    bad = 0
    for _ in range(draws):
        pc = rng.betavariate(a_c, b_c)
        pt = rng.betavariate(a_t, b_t)
        change = pt - pc if scale == "points" else (pt - pc) / pc
        if (change > margin) if worse == "up" else (change < -margin):
            bad += 1
    return bad / draws


def _observed(m: MetricReadout, challenger: str) -> str:
    if m.aggregation == "occurrence":
        c, t = m.arms.get(CONTROL), m.arms.get(challenger)
        if c and t and c.n and t.n:
            points = (t.mean - c.mean) * 100
            return f"{c.mean * 100:.0f}% → {t.mean * 100:.0f}% ({points:+.1f} pts)"
    if m.inference is not None:
        inf = m.inference
        return f"{_pct(inf.lift)} (95% CI {_pct(inf.ci_low)} … {_pct(inf.ci_high)})"
    return f"{_rate(m, CONTROL)} → {_rate(m, challenger)}"


def _guardrail(readout: Readout, g: Guardrail, policy: DecisionPolicy) -> GuardrailStatus:
    m = readout.metrics.get(g.metric)
    sign = "+" if g.worse == "up" else "−"
    margin = f"{sign}{g.margin_text()}"
    p, source = None, None
    if m is not None:
        control = m.arms.get(CONTROL)
        if m.inference is not None:
            p = p_bad_from_inference(m.inference.lift, m.inference.ci_low, m.inference.ci_high, g,
                                     control.mean if control else None)
            source = "flagsmith"
        if p is None and m.aggregation == "occurrence":
            challenger = m.arms.get(readout.challenger)
            if control and challenger and control.n and challenger.n:
                p = p_bad_from_counts(control.n, int(round(control.total)), challenger.n, int(round(challenger.total)),
                                      g.margin, g.worse, g.scale)
                source = "counts"
    if p is None:
        return GuardrailStatus(g.metric, g.name, "no_data", f"{g.name}: no data yet", None, margin, None)
    state = "breach" if p >= policy.breach_at else "ok" if p <= policy.safe_at else "uncertain"
    pronoun, moved = ("they" if g.plural else "it"), ("rose" if g.worse == "up" else "fell")
    verdict = {"ok": "within the margin", "breach": "worse than the margin", "uncertain": "not proven safe yet"}[state]
    text = (f"{g.name}: {_observed(m, readout.challenger)} · {prob(p)} chance {pronoun} {moved} more than "
            f"{g.margin_text()} · {verdict}" + (" (from raw counts; Flagsmith shows no result yet)" if source == "counts" else ""))
    return GuardrailStatus(g.metric, g.name, state, text, p, margin, source)


def _not_safe_yet(g: GuardrailStatus, guardrail: Guardrail) -> str:
    be = "aren't" if guardrail.plural else "isn't"
    if g.state == "no_data":
        return f"{g.label} {be} proven safe yet (no data)"
    pronoun, moved = ("they" if guardrail.plural else "it"), ("rose" if guardrail.worse == "up" else "fell")
    return f"{g.label} {be} proven safe yet ({prob(g.p_bad)} chance {pronoun} {moved} more than {guardrail.margin_text()})"


def _smallest_arm(readout: Readout, primary: MetricReadout | None) -> int:
    arms = primary.arms if primary else {}
    counts = [arms[k].n if k in arms else 0 for k in (CONTROL, readout.challenger)] if arms else \
        [readout.exposures.get(k, 0) for k in (CONTROL, readout.challenger)]
    return min(counts)


# ---------------------------------------------------------------- decide
def decide(readout: Readout | None, policy: DecisionPolicy = DecisionPolicy(), *, champion: str = "champion",
           min_per_arm: int | None = None) -> Decision:
    planned = policy.min_per_arm if min_per_arm is None else min_per_arm
    if readout is None or readout.as_of is None:
        reason = "Flagsmith hasn't computed results yet."
        if readout is not None and readout.last_error_at:
            reason = "Flagsmith's last results refresh failed; it will retry on the next refresh."
        return Decision(COLLECTING_DATA, "Waiting for the first results.", [reason], min_per_arm=planned)

    challenger = readout.challenger
    primary = readout.metrics.get(policy.primary)
    guardrails = [_guardrail(readout, g, policy) for g in policy.guardrails]
    rules = {g.metric: g for g in policy.guardrails}
    smallest = _smallest_arm(readout, primary)

    def make(verdict: str, headline: str, reasons: list[str], rollout: str | None = None) -> Decision:
        return Decision(verdict, headline, reasons, guardrails, rollout, planned, smallest)

    if primary is None or primary.inference is None:
        arms = primary.arms if primary else {}

        def count(a):
            return f"{a.n} chats" + (f" / {a.total:.0f} {policy.primary}" if primary and primary.aggregation == "occurrence" else "")
        counts = ", ".join(f"{k}: {count(a)}" for k, a in sorted(arms.items())) or "no traffic yet"
        return make(COLLECTING_DATA, "Collecting data.",
                    [f"Flagsmith shows a result once each arm has 50 identities and 5 conversions ({counts})."]
                    + [g.text for g in guardrails if g.state != "no_data"])

    if readout.srm_p_value is not None and readout.srm_p_value < policy.srm_alpha:
        split = ", ".join(f"{k} {v}" for k, v in sorted(readout.exposures.items()))
        return make(HOLD, "Traffic split looks broken; don't trust these results.",
                    [f"Sample ratio mismatch: p = {readout.srm_p_value:.2g} ({split}). Check assignment before deciding."])

    inf = primary.inference
    label = LABELS.get(policy.primary, policy.primary)
    better = "lower" if primary.lower_is_better else "better"
    vs = f"{label} {_rate(primary, challenger)} vs {_rate(primary, CONTROL)} for {champion}"
    stats = f"{_pct(inf.lift)}, 95% CI {_pct(inf.ci_low)} … {_pct(inf.ci_high)}, {inf.chance_to_win:.1%} chance it's {better}"
    reasons = [f"{challenger}: {vs} ({stats})"] + [g.text for g in guardrails]

    breaches = [g for g in guardrails if g.state == "breach"]
    if breaches:
        b = breaches[0]
        moved = "rose" if rules[b.metric].worse == "up" else "fell"
        return make(KILL, f"Kill {challenger}: {b.label} {moved} more than the {rules[b.metric].margin_text()} margin "
                          f"({prob(b.p_bad)} chance).", reasons, "control")
    if smallest < planned:
        return make(COLLECTING_DATA, f"Collecting data: {smallest} of {planned} chats per arm so far.",
                    reasons + [f"Darwin calls a winner or a loser once each arm has {planned} chats (planned at launch)."])
    if inf.chance_to_win >= policy.ship_at:
        wins = WINS.get(policy.primary, f"does better on {label}")
        unsafe = [g for g in guardrails if g.state != "ok"]
        if not unsafe:
            return make(SHIP, f"Ship {challenger}: {vs} ({_pct(inf.lift)}), {inf.chance_to_win:.1%} chance it's {better}; "
                              f"every guardrail is within its margin.", reasons, "variant")
        first = unsafe[0]
        return make(KEEP_RUNNING, f"{challenger} {wins} ({inf.chance_to_win:.1%}), but {_not_safe_yet(first, rules[first.metric])} "
                                  f"— keep running.", reasons)
    if inf.chance_to_win <= policy.discard_at:
        return make(DISCARD, f"Discard {challenger}: it does worse than {champion} ({inf.chance_to_win:.1%} chance to win).",
                    reasons, "control")
    return make(KEEP_RUNNING, f"No clear winner yet ({inf.chance_to_win:.0%} chance {challenger} is better). Keep running.", reasons)
