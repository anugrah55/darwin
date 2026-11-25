"""Every Flagsmith admin action Darwin takes goes through the official Flagsmith CLI (v2).

Commands are built first and executed second, so an approval card can show the exact commands a click
will run. Each executed command is appended to data/flagsmith-commands.jsonl.

The CLI has first-class commands for features, variants and flags; experiments and metrics (beta) go
through `flagsmith api`, which calls the Admin API with the CLI's credentials.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import Settings


class FlagsmithError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.detail = detail


class Throttled(FlagsmithError):
    """Flagsmith allows one results/exposures refresh per experiment every 5 minutes."""


@dataclass
class Command:
    args: list[str]
    summary: str
    body: dict | None = None
    write: bool = True
    labels: dict[str, str] = field(default_factory=dict)  # real arg value -> what to print instead

    def argv(self) -> list[str]:
        flags = ["--no-input", "--json"] + (["--yes"] if self.write else [])
        return ["flagsmith", *flags, *self.args]

    def display(self) -> str:
        """The command as run, minus the output-format flags (--no-input --json); long values abbreviated."""
        shown = [self.labels.get(a, a) for a in self.args]
        parts = ["flagsmith", *(["--yes"] if self.write else [])]
        parts += [a if a.startswith("<") and a.endswith(">") else shlex.quote(a) for a in shown]
        text = " ".join(parts)
        if self.body is not None:
            text += "   # JSON body on stdin"
        return text

    def display_body(self) -> str | None:
        if self.body is None:
            return None
        def shorten(v: Any) -> Any:
            if isinstance(v, str) and v in self.labels:
                return self.labels[v]
            if isinstance(v, dict):
                return {k: shorten(x) for k, x in v.items()}
            if isinstance(v, list):
                return [shorten(x) for x in v]
            return v
        return json.dumps(shorten(self.body), indent=2)

    def to_dict(self) -> dict:
        return {"summary": self.summary, "command": self.display(), "body": self.display_body()}


_STATUS = re.compile(r"returned (\d{3})")


class FlagsmithAdmin:
    def __init__(self, settings: Settings, *, runner: Callable[..., subprocess.CompletedProcess] | None = None,
                 journal_path: Path | None = None):
        self.settings = settings
        self.env = settings.flagsmith_environment
        self._runner = runner or subprocess.run
        self.journal_path = journal_path or (settings.data_dir / "flagsmith-commands.jsonl")

    # ---------- execution ----------
    def run(self, cmd: Command) -> Any:
        started = time.time()
        proc = self._runner(
            cmd.argv(),
            input=json.dumps(cmd.body) if cmd.body is not None else None,
            capture_output=True,
            text=True,
            env=self.settings.cli_env(),
            timeout=60,
        )
        out = (proc.stdout or "").strip()
        try:
            parsed = json.loads(out) if out else None
        except json.JSONDecodeError:
            parsed = out
        ok = proc.returncode == 0
        if cmd.write:
            self._journal(cmd, ok=ok, seconds=time.time() - started, error=None if ok else (proc.stderr or "")[:300])
        if ok:
            return parsed
        m = _STATUS.search(proc.stderr or "")
        status = int(m.group(1)) if m else None
        detail = parsed.get("detail") if isinstance(parsed, dict) and "detail" in parsed else parsed
        message = f"{cmd.summary}: Flagsmith returned {status}: {detail or (proc.stderr or '').strip()[:200]}"
        if status == 429:
            raise Throttled(message, status=status, detail=detail)
        raise FlagsmithError(message, status=status, detail=detail)

    def run_all(self, commands: list[Command]) -> list[Any]:
        return [self.run(c) for c in commands]

    def _journal(self, cmd: Command, *, ok: bool, seconds: float, error: str | None) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.journal_path, "a") as f:
            f.write(json.dumps({"ts": time.time(), "summary": cmd.summary, "command": cmd.display(), "ok": ok,
                                "seconds": round(seconds, 2), "error": error}) + "\n")

    def journal(self) -> list[dict]:
        if not self.journal_path.exists():
            return []
        return [json.loads(line) for line in self.journal_path.read_text().splitlines() if line.strip()]

    # ---------- paths ----------
    def _env_path(self, suffix: str) -> str:
        return f"api/v1/environments/{self.env}/{suffix}"

    def _api(self, path: str, *, summary: str, method: str = "GET", body: dict | None = None, labels=None) -> Command:
        args = ["api", self._env_path(path), "-X", method]
        if body is not None:
            args += ["--input", "-"]
        return Command(args, summary=summary, body=body, write=method != "GET", labels=labels or {})

    # ---------- reads ----------
    def feature(self, name: str) -> dict | None:
        try:
            return self.run(Command(["feature", "get", name], summary=f"Read feature {name}", write=False))
        except FlagsmithError as exc:
            if exc.status == 404 or "not found" in str(exc).lower():
                return None
            raise

    def flag(self, name: str) -> dict:
        return self.run(Command(["flag", "get", name], summary=f"Read flag {name}", write=False))

    def metrics(self) -> list[dict]:
        data = self.run(self._api("experiment-metrics/?page_size=100", summary="List metrics"))
        return data.get("results", data) if isinstance(data, dict) else (data or [])

    def experiments(self) -> list[dict]:
        data = self.run(self._api("experiments/?page_size=100", summary="List experiments"))
        return data.get("results", data) if isinstance(data, dict) else (data or [])

    def experiment(self, experiment_id: int) -> dict:
        return self.run(self._api(f"experiments/{experiment_id}/", summary=f"Read experiment {experiment_id}"))

    def results(self, experiment_id: int) -> dict | None:
        return (self.run(self._api(f"experiments/{experiment_id}/results/", summary="Read results")) or {}).get("results")

    def exposures(self, experiment_id: int) -> dict | None:
        return (self.run(self._api(f"experiments/{experiment_id}/exposures/", summary="Read exposures")) or {}).get("exposures")

    # ---------- write commands (built, not run) ----------
    def cmd_create_feature(self, name: str, value: str, label: str) -> Command:
        return Command(["feature", "create", name, "--value", value, "--enabled",
                        "--description", "Darwin: the AI agent's whole config (champion = control, challengers = variants)"],
                       summary=f"Create flag {name} with {label} as its value", labels={value: f"<{label}>"})

    def cmd_add_variant(self, feature: str, key: str, value: str) -> Command:
        return Command(["feature", "variant", "add", feature, "--key", key, "--value", value, "--type", "string", "--weight", "0"],
                       summary=f"Add {key} to {feature} as a variant, at 0% until the experiment starts",
                       labels={value: f"<{key} genome, {len(value):,} chars>"})

    def cmd_set_default(self, feature: str, value: str, label: str, variant_keys: list[str]) -> Command:
        args = ["flag", "update", feature, "--value", value, "--type", "string"]
        if variant_keys:
            args += ["--weight", ",".join(f"{k}=0" for k in variant_keys)]
        return Command(args, summary=f"Make {label} the flag's default value; every variant at 0%",
                       labels={value: f"<{label} genome, {len(value):,} chars>"})

    def cmd_create_metric(self, name: str, event: str, aggregation: str, direction: str, description: str) -> Command:
        body = {"name": name, "description": description, "aggregation": aggregation, "direction": direction,
                "definition": {"version": 1, "event": event}}
        return self._api("experiment-metrics/", summary=f"Create metric {name} ({aggregation} of '{event}', {direction} is good)",
                         method="POST", body=body)

    def cmd_create_experiment(self, *, feature_id: int, name: str, hypothesis: str, metrics: list[dict], control_value: str,
                              control_label: str, weights: dict[int, float], weight_labels: dict[int, str]) -> Command:
        body = {
            "feature": feature_id,
            "name": name,
            "hypothesis": hypothesis,
            "metrics": metrics,
            "experiment_rollout": {
                "enabled": False,  # switched on by `start`, so no traffic is split before it counts
                "rollout_percentage": 100,
                "feature_state_value": {"type": "string", "value": control_value},
                "multivariate_feature_state_values": [
                    {"multivariate_feature_option": oid, "percentage_allocation": w} for oid, w in weights.items()
                ],
            },
        }
        split = ", ".join(f"{weight_labels.get(oid, oid)} {w:g}%" for oid, w in weights.items() if w)
        return self._api("experiments/", summary=f"Create experiment '{name}' (control {control_label}, {split})", method="POST",
                         body=body, labels={control_value: f"<{control_label} genome>"})

    def cmd_start(self, experiment_id: int | str) -> Command:
        return self._api(f"experiments/{experiment_id}/start/", summary="Start the experiment (traffic splits now)", method="POST")

    def cmd_complete(self, experiment_id: int | str, decision: str, variant_id: int | None = None, label: str = "") -> Command:
        body: dict[str, Any] = {"rollout_decision": decision}
        if decision == "variant":
            body["rollout_variant"] = variant_id
        what = {"variant": f"roll out {label} to everyone", "control": "keep the champion for everyone",
                "disable": "turn the flag off"}[decision]
        return self._api(f"experiments/{experiment_id}/complete/", summary=f"Complete the experiment and {what}",
                         method="POST", body=body)

    def cmd_update_hypothesis(self, experiment_id: int | str, hypothesis: str, note: str) -> Command:
        """PATCH the experiment's hypothesis, so Flagsmith itself records who approved what."""
        return self._api(f"experiments/{experiment_id}/", summary=f"Record in Flagsmith: “{note}”", method="PATCH",
                         body={"hypothesis": hypothesis})

    def cmd_refresh_results(self, experiment_id: int) -> Command:
        return self._api(f"experiments/{experiment_id}/results/refresh/", summary="Ask Flagsmith to recompute results", method="POST")

    def cmd_refresh_exposures(self, experiment_id: int) -> Command:
        return self._api(f"experiments/{experiment_id}/exposures/refresh/", summary="Ask Flagsmith to recount exposures", method="POST")
