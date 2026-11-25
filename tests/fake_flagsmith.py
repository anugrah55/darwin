"""An in-memory Flagsmith that executes the real Command objects FlagsmithAdmin builds.

It follows the rules of Flagsmith 2.295 that Darwin depends on:

- experiments move created → running ⇄ paused → completed, and a completed experiment can't move again;
- `complete` needs a rollout decision, records it on the experiment and rewrites the environment default;
- creating an experiment zeroes every variant's weight on the environment default;
- results refresh at most once every 5 minutes (429 otherwise), never before start or once final;
- no inference until both arms have 50 identities, and 5 conversions for occurrence metrics;
- `feature get` is the project-level initial value, `flag get` what the environment serves.
"""

from __future__ import annotations

import copy
import itertools
import json
import re
import time
from datetime import datetime, timezone

from darwin.flagsmith_admin import Command, FlagsmithAdmin, FlagsmithError, Throttled

MIN_IDENTITIES = 50
MIN_CONVERSIONS = 5
REFRESH_INTERVAL = 300
TRANSITIONS = {"created": {"running"}, "running": {"paused", "completed"}, "paused": {"running", "completed"},
               "completed": set()}


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _no_cli(*args, **kwargs):
    raise AssertionError("a test tried to run the real Flagsmith CLI")


class FakeFlagsmith(FlagsmithAdmin):
    def __init__(self, settings):
        super().__init__(settings, runner=_no_cli)
        self.features: dict[str, dict] = {}
        self.metric_list: list[dict] = []
        self.exps: dict[int, dict] = {}
        self.payloads: dict[int, dict] = {}  # experiment id -> results payload the test wants served
        self.as_of: dict[int, str] = {}  # experiment id -> when those results were "computed"
        self.refreshed_at: dict[int, float] = {}
        self.log: list[Command] = []
        self.throttle_refresh = False  # force a 429 on every refresh
        self.fail_next = None  # predicate on a Command: fail that command once with a 502
        self.clock = time.time  # tests can pin or advance time
        self._ids = itertools.count(1)

    # ---------- reads ----------
    def feature(self, name):
        """Like `flagsmith feature get`: project-level — the value the feature was created with."""
        f = self.features.get(name)
        if not f:
            return None
        out = copy.deepcopy(f)
        out["default_value"] = f["initial_value"]
        return out

    def flag(self, name):
        """Like `flagsmith flag get`: what this environment serves."""
        f = self.features.get(name)
        if not f:
            return None
        out = {k: v for k, v in copy.deepcopy(f).items() if k not in ("default_value", "initial_value")}
        out["value"] = f["default_value"]
        return out

    def metrics(self):
        return copy.deepcopy(self.metric_list)

    def experiments(self):
        return [copy.deepcopy(e) for e in self.exps.values()]

    def experiment(self, experiment_id):
        if experiment_id not in self.exps:
            raise FlagsmithError(f"Read experiment {experiment_id}: Flagsmith returned 404: Not found.", status=404)
        return copy.deepcopy(self.exps[experiment_id])

    def results(self, experiment_id):
        payload = self.payloads.get(experiment_id)
        requested = self.refreshed_at.get(experiment_id)
        if payload is None and requested is None:
            return None
        exp = self.exps[experiment_id]
        as_of = self.as_of.setdefault(experiment_id, iso(self.clock())) if payload is not None else None
        final = bool(exp.get("ended_at") and as_of and parse(as_of) >= parse(exp["ended_at"]))
        return {"as_of": as_of, "payload": self._with_floor(payload) if payload is not None else None, "is_final": final,
                "last_error_at": None, "refresh_requested_at": iso(requested) if requested else None}

    def _with_floor(self, payload: dict) -> dict:
        """Flagsmith shows no inference until both arms reach its minimum sample (and conversions)."""
        payload = copy.deepcopy(payload)
        aggregation = {m["id"]: m["aggregation"] for m in self.metric_list}
        for m in payload.get("metrics", []):
            arms = m.get("variants") or {}
            small = any(a["n"] < MIN_IDENTITIES for a in arms.values()) or len(arms) < 2
            few = aggregation.get(m["metric_id"]) == "occurrence" and any(a["sum"] < MIN_CONVERSIONS for a in arms.values())
            control_zero = arms.get("control", {}).get("sum", 1) <= 0
            if small or few or control_zero:
                m["inference"] = {k: None for k in (m.get("inference") or {})}
        return payload

    def journal(self):
        return [{"ok": True, "command": c.display(), "summary": c.summary} for c in self.log if c.write]

    # ---------- writes ----------
    def run(self, cmd: Command):
        if self.fail_next and self.fail_next(cmd):
            self.fail_next = None
            raise FlagsmithError(f"{cmd.summary}: Flagsmith returned 502: injected failure", status=502)
        self.log.append(cmd)
        a = cmd.args
        if a[:2] == ["feature", "create"]:
            value = a[a.index("--value") + 1]
            self.features[a[2]] = {"id": next(self._ids), "name": a[2], "type": "standard", "enabled": True,
                                   "default_value": value, "initial_value": value, "variants": []}
            return copy.deepcopy(self.features[a[2]])
        if a[:3] == ["feature", "variant", "add"]:
            f = self.features[a[3]]
            key = a[a.index("--key") + 1]
            if any(v["key"] == key for v in f["variants"]):
                raise FlagsmithError(f"{cmd.summary}: Flagsmith returned 400: a variant with key {key} exists", status=400)
            f["type"] = "multivariate"
            f["variants"].append({"id": next(self._ids), "key": key, "value": a[a.index("--value") + 1],
                                  "weight": float(a[a.index("--weight") + 1])})
            return None
        if a[:2] == ["flag", "update"]:
            f = self.features[a[2]]
            if "--value" in a:
                f["default_value"] = a[a.index("--value") + 1]
            if "--weight" in a:
                for pair in a[a.index("--weight") + 1].split(","):
                    key, w = pair.split("=")
                    next(v for v in f["variants"] if v["key"] == key)["weight"] = float(w)
            return copy.deepcopy(f)
        if a[0] == "api":
            method = a[a.index("-X") + 1] if "-X" in a else "GET"
            return self._handle_api(a[1].split("/environments/")[1].split("/", 1)[1], method, cmd.body)
        raise AssertionError(f"unexpected command {a}")

    def _feature_by_id(self, feature_id: int) -> dict:
        return next(f for f in self.features.values() if f["id"] == feature_id)

    def _handle_api(self, path: str, method: str, body):
        if path.startswith("experiment-metrics/"):
            m = {"id": next(self._ids), "name": body["name"], "aggregation": body["aggregation"], "direction": body["direction"]}
            self.metric_list.append(m)
            return copy.deepcopy(m)
        if path == "experiments/":
            feature = self._feature_by_id(body["feature"])
            if any(e["status"] != "completed" and e["feature"]["id"] == feature["id"] for e in self.exps.values()):
                raise FlagsmithError("Create experiment: Flagsmith returned 409: An active experiment already exists "
                                     "for this feature.", status=409)
            if feature["type"] != "multivariate":
                raise FlagsmithError("Create experiment: Flagsmith returned 400: multivariate flags only", status=400)
            by_id = {m["id"]: m for m in self.metric_list}
            eid = next(self._ids)
            self.exps[eid] = {"id": eid, "name": body["name"], "hypothesis": body["hypothesis"], "status": "created",
                              "feature": {"id": feature["id"], "name": feature["name"]}, "body": copy.deepcopy(body),
                              "started_at": None, "ended_at": None, "rollout_decision": None, "rollout_variant": None,
                              "metrics": [{"metric": m["metric"], "metric_name": by_id[m["metric"]]["name"],
                                           "aggregation": by_id[m["metric"]]["aggregation"],
                                           "direction": by_id[m["metric"]]["direction"],
                                           "expected_direction": m["expected_direction"]} for m in body["metrics"]]}
            for v in feature["variants"]:
                v["weight"] = 0  # Flagsmith resets default allocations when an experiment's rollout is created
            return copy.deepcopy(self.exps[eid])
        if m := re.fullmatch(r"experiments/(\d+)/", path):
            exp = self._exp(int(m.group(1)))
            if method == "PATCH":
                for key in ("name", "hypothesis"):
                    if key in (body or {}):
                        exp[key] = body[key]
            return copy.deepcopy(exp)
        m = re.fullmatch(r"experiments/(\d+)/(start|pause|complete|results/refresh|exposures/refresh)/", path)
        if not m:
            raise AssertionError(f"unexpected API path {path}")
        eid, action = int(m.group(1)), m.group(2)
        exp = self._exp(eid)
        if action in ("start", "pause", "complete"):
            target = {"start": "running", "pause": "paused", "complete": "completed"}[action]
            if target not in TRANSITIONS[exp["status"]]:
                raise FlagsmithError(f"{action}: Flagsmith returned 400: Unable to transition experiment status.", status=400)
            if action == "complete":
                self._complete(exp, body or {})
            exp["status"] = target
            if target == "running" and not exp["started_at"]:
                exp["started_at"] = iso(self.clock())
            return copy.deepcopy(exp)
        # refreshes
        if not exp["started_at"]:
            raise FlagsmithError("refresh: Flagsmith returned 400: Cannot refresh results before the experiment starts.", status=400)
        current = self.results(eid)
        if current and current["is_final"]:
            raise FlagsmithError("refresh: Flagsmith returned 400: Results are final for this completed experiment.", status=400)
        last = self.refreshed_at.get(eid)
        if self.throttle_refresh or (last is not None and self.clock() - last < REFRESH_INTERVAL):
            raise Throttled("refresh: Flagsmith returned 429: A refresh was requested recently. Try again later.", status=429)
        self.refreshed_at[eid] = self.clock()
        if exp["ended_at"] and eid in self.payloads:
            self.as_of[eid] = iso(self.clock() + 1)  # recomputed after completion: the numbers are final now
        return None

    def _exp(self, eid: int) -> dict:
        if eid not in self.exps:
            raise FlagsmithError(f"experiment {eid}: Flagsmith returned 404: Not found.", status=404)
        return self.exps[eid]

    def _complete(self, exp: dict, body: dict) -> None:
        decision = body.get("rollout_decision")
        feature = self._feature_by_id(exp["feature"]["id"])
        if decision not in ("control", "variant", "disable"):
            raise FlagsmithError("complete: Flagsmith returned 400: rollout_decision is required", status=400)
        variant = body.get("rollout_variant")
        if decision == "variant" and not any(v["id"] == variant for v in feature["variants"]):
            raise FlagsmithError("complete: Flagsmith returned 400: Variant not found for the experiment's feature.", status=400)
        if decision != "variant" and variant is not None:
            raise FlagsmithError("complete: Flagsmith returned 400: Only allowed when rolling out a variant.", status=400)
        exp.update(decision=copy.deepcopy(body), rollout_decision=decision, rollout_variant=variant if decision == "variant" else None,
                   ended_at=iso(self.clock()))
        if decision == "disable":
            feature["enabled"] = False
            return
        feature["enabled"] = True
        for v in feature["variants"]:
            v["weight"] = 100 if decision == "variant" and v["id"] == variant else 0

    # ---------- test helpers ----------
    def complete_in_dashboard(self, eid: int, decision: str, variant_key: str | None = None) -> None:
        """Someone completes the experiment in Flagsmith's dashboard, outside Darwin."""
        exp = self._exp(eid)
        feature = self._feature_by_id(exp["feature"]["id"])
        variant = next(v["id"] for v in feature["variants"] if v["key"] == variant_key) if variant_key else None
        self._complete(exp, {"rollout_decision": decision, **({"rollout_variant": variant} if variant else {})})
        exp["status"] = "completed"

    def served_value(self, name: str) -> str | None:
        """What a user outside any experiment gets (None when the flag is off)."""
        f = self.features[name]
        if not f.get("enabled", True):
            return None
        full = [v for v in f["variants"] if v["weight"] == 100]
        return full[0]["value"] if full else f["default_value"]


def payload(metric_ids: dict, challenger: str, *, resolved=(40, 70), n=(100, 100), chance=0.99, lift=0.75,
            esc_chance=0.6, cost_chance=0.5, escalated=(30, 10)):
    def inf(c, l):
        return {"lift": l, "ci_low": l - 0.3, "ci_high": l + 0.3, "chance_to_win": c}
    return {
        "srm_p_value": None,
        "metrics": [
            {"metric_id": metric_ids["resolved"], "variants": {"control": {"n": n[0], "sum": resolved[0], "sum_squares": resolved[0]},
                                                               challenger: {"n": n[1], "sum": resolved[1], "sum_squares": resolved[1]}},
             "inference": {challenger: inf(chance, lift)}},
            {"metric_id": metric_ids["escalated"], "variants": {"control": {"n": n[0], "sum": escalated[0], "sum_squares": escalated[0]},
                                                                challenger: {"n": n[1], "sum": escalated[1], "sum_squares": escalated[1]}},
             "inference": {challenger: {"lift": -0.6, "ci_low": -0.8, "ci_high": -0.3, "chance_to_win": esc_chance}}},
            {"metric_id": metric_ids["cost_microusd"], "variants": {"control": {"n": n[0], "sum": 60000, "sum_squares": 4e7},
                                                                    challenger: {"n": n[1], "sum": 50000, "sum_squares": 3e7}},
             "inference": {challenger: {"lift": -0.15, "ci_low": -0.3, "ci_high": 0.0, "chance_to_win": cost_chance}}},
        ],
        "exposures_timeseries": {"points": [{"new_identities": {"control": n[0], challenger: n[1]}}]},
    }
