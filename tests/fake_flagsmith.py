"""An in-memory Flagsmith that executes the real Command objects FlagsmithAdmin builds."""

from __future__ import annotations

import itertools
import json
import re

from darwin.flagsmith_admin import Command, FlagsmithAdmin, FlagsmithError, Throttled


class FakeFlagsmith(FlagsmithAdmin):
    def __init__(self, settings):
        super().__init__(settings)
        self.features: dict[str, dict] = {}
        self.metric_list: list[dict] = []
        self.exps: dict[int, dict] = {}
        self.payloads: dict[int, dict] = {}  # experiment id -> results payload the test wants served
        self.log: list[Command] = []
        self.throttle_refresh = False
        self._ids = itertools.count(1)

    # reads
    def feature(self, name):
        f = self.features.get(name)
        return json.loads(json.dumps(f)) if f else None

    def flag(self, name):
        return self.feature(name)

    def metrics(self):
        return list(self.metric_list)

    def experiments(self):
        return list(self.exps.values())

    def experiment(self, experiment_id):
        return self.exps[experiment_id]

    def results(self, experiment_id):
        payload = self.payloads.get(experiment_id)
        return {"as_of": "2026-11-26T10:00:00Z", "payload": payload, "is_final": False, "last_error_at": None} if payload else None

    def journal(self):
        return [{"ok": True, "command": c.display()} for c in self.log if c.write]

    # writes
    def run(self, cmd: Command):
        self.log.append(cmd)
        a = cmd.args
        if a[:2] == ["feature", "create"]:
            self.features[a[2]] = {"id": next(self._ids), "name": a[2], "type": "standard", "enabled": True,
                                   "default_value": a[a.index("--value") + 1], "variants": []}
            return self.features[a[2]]
        if a[:3] == ["feature", "variant", "add"]:
            f = self.features[a[3]]
            f["type"] = "multivariate"
            f["variants"].append({"id": next(self._ids), "key": a[a.index("--key") + 1],
                                  "value": a[a.index("--value") + 1], "weight": float(a[a.index("--weight") + 1])})
            return None
        if a[:2] == ["flag", "update"]:
            f = self.features[a[2]]
            if "--value" in a:
                f["default_value"] = a[a.index("--value") + 1]
            if "--weight" in a:
                for pair in a[a.index("--weight") + 1].split(","):
                    key, w = pair.split("=")
                    next(v for v in f["variants"] if v["key"] == key)["weight"] = float(w)
            return f
        if a[0] == "api":
            return self._handle_api(a[1].split("/environments/")[1].split("/", 1)[1], cmd.body)
        raise AssertionError(f"unexpected command {a}")

    def _handle_api(self, path: str, body):
        if path.startswith("experiment-metrics/"):
            m = {"id": next(self._ids), "name": body["name"], "aggregation": body["aggregation"], "direction": body["direction"]}
            self.metric_list.append(m)
            return m
        if path == "experiments/":
            feature = next(f for f in self.features.values() if f["id"] == body["feature"])
            if any(e["status"] != "completed" and e["feature"]["id"] == feature["id"] for e in self.exps.values()):
                raise FlagsmithError("conflict", status=409)
            by_id = {m["id"]: m for m in self.metric_list}
            eid = next(self._ids)
            self.exps[eid] = {"id": eid, "name": body["name"], "status": "created", "feature": {"id": feature["id"]},
                              "body": body, "metrics": [{"metric": m["metric"], "metric_name": by_id[m["metric"]]["name"],
                                                         "aggregation": by_id[m["metric"]]["aggregation"],
                                                         "direction": by_id[m["metric"]]["direction"],
                                                         "expected_direction": m["expected_direction"]} for m in body["metrics"]]}
            for v in feature["variants"]:
                v["weight"] = 0  # Flagsmith resets default allocations when an experiment's rollout is created
            return self.exps[eid]
        m = re.match(r"experiments/(\d+)/(start|complete|results/refresh)/", path)
        eid, action = int(m.group(1)), m.group(2)
        exp = self.exps[eid]
        if action == "start":
            exp["status"] = "running"
        elif action == "complete":
            exp["status"], exp["decision"] = "completed", body
            feature = next(f for f in self.features.values() if f["id"] == exp["feature"]["id"])
            for v in feature["variants"]:
                v["weight"] = 100 if body["rollout_decision"] == "variant" and v["id"] == body.get("rollout_variant") else 0
        elif action == "results/refresh" and self.throttle_refresh:
            raise Throttled("throttled", status=429)
        return exp

    def served_value(self, name: str) -> str:
        """What a user outside any experiment gets."""
        f = self.features[name]
        full = [v for v in f["variants"] if v["weight"] == 100]
        return full[0]["value"] if full else f["default_value"]


def payload(metric_ids: dict, challenger: str, *, resolved=(40, 70), n=(100, 100), chance=0.99, lift=0.75,
            esc_chance=0.6, cost_chance=0.5):
    def inf(c, l):
        return {"lift": l, "ci_low": l - 0.3, "ci_high": l + 0.3, "chance_to_win": c}
    return {
        "srm_p_value": None,
        "metrics": [
            {"metric_id": metric_ids["resolved"], "variants": {"control": {"n": n[0], "sum": resolved[0], "sum_squares": resolved[0]},
                                                               challenger: {"n": n[1], "sum": resolved[1], "sum_squares": resolved[1]}},
             "inference": {challenger: inf(chance, lift)}},
            {"metric_id": metric_ids["escalated"], "variants": {"control": {"n": n[0], "sum": 30, "sum_squares": 30},
                                                                challenger: {"n": n[1], "sum": 10, "sum_squares": 10}},
             "inference": {challenger: {"lift": -0.6, "ci_low": -0.8, "ci_high": -0.3, "chance_to_win": esc_chance}}},
            {"metric_id": metric_ids["cost_microusd"], "variants": {"control": {"n": n[0], "sum": 60000, "sum_squares": 4e7},
                                                                    challenger: {"n": n[1], "sum": 50000, "sum_squares": 3e7}},
             "inference": {challenger: {"lift": -0.15, "ci_low": -0.3, "ci_high": 0.0, "chance_to_win": cost_chance}}},
        ],
        "exposures_timeseries": {"points": [{"new_identities": {"control": n[0], challenger: n[1]}}]},
    }
