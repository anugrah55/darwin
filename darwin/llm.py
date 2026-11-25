"""Claude API access with a hard spend cap shared by every Darwin process.

Every call is checked against the cap *before* it is sent (using a worst-case estimate) and its real cost
is recorded afterwards from `response.usage`. The ledger is a JSON file guarded by a file lock, so ShopBot,
the simulator and the Coach can run as separate processes and still share one budget.
"""

from __future__ import annotations

import fcntl
import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import anthropic

# USD per million tokens: (input, output, cache write, cache read).
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
    "claude-opus-5": (5.00, 25.00, 6.25, 0.50),
    "claude-opus-4-8": (5.00, 25.00, 6.25, 0.50),
    "claude-sonnet-5": (2.00, 10.00, 2.50, 0.20),
}
_UNKNOWN_MODEL_PRICE = (5.00, 25.00, 6.25, 0.50)
_CHARS_PER_TOKEN = 3.0  # deliberately pessimistic for the pre-call estimate


class BudgetExceeded(RuntimeError):
    """Raised instead of making a call that could take spend past DARWIN_BUDGET_USD."""


def price_of(model: str) -> tuple[float, float, float, float]:
    return PRICES.get(model, _UNKNOWN_MODEL_PRICE)


def usage_cost(model: str, usage: Any) -> float:
    p_in, p_out, p_write, p_read = price_of(model)
    get = (lambda k: getattr(usage, k, None) or 0) if not isinstance(usage, dict) else (lambda k: usage.get(k) or 0)
    return (
        get("input_tokens") * p_in
        + get("output_tokens") * p_out
        + get("cache_creation_input_tokens") * p_write
        + get("cache_read_input_tokens") * p_read
    ) / 1_000_000


def _approx_chars(obj: Any) -> int:
    if obj is None:
        return 0
    if isinstance(obj, str):
        return len(obj)
    if isinstance(obj, dict):
        return sum(_approx_chars(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return sum(_approx_chars(v) for v in obj)
    dump = getattr(obj, "model_dump_json", None)
    if callable(dump):
        return len(dump())
    return len(str(obj))


def estimate_cost(model: str, *, request: dict) -> float:
    """Worst case for one request: every input char a token-third, and the full max_tokens of output."""
    p_in, p_out, _, _ = price_of(model)
    chars = _approx_chars([request.get("system"), request.get("messages"), request.get("tools")])
    return (chars / _CHARS_PER_TOKEN) * p_in / 1_000_000 + request.get("max_tokens", 0) * p_out / 1_000_000


class SpendLedger:
    def __init__(self, path: Path, cap_usd: float):
        self.path = Path(path)
        self.cap_usd = cap_usd
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.path.with_suffix(".lock")
        self._log_path = self.path.with_suffix(".jsonl")

    @contextmanager
    def _locked(self):
        with open(self._lock_path, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _read(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except json.JSONDecodeError:
                pass
        return {"spent_usd": 0.0, "calls": 0, "by_purpose": {}, "by_model": {}}

    def check(self, estimate_usd: float, purpose: str) -> None:
        with self._locked():
            spent = self._read()["spent_usd"]
        if spent + estimate_usd > self.cap_usd:
            raise BudgetExceeded(
                f"{purpose}: this call could cost up to ${estimate_usd:.4f}; ${spent:.4f} of the "
                f"${self.cap_usd:.2f} budget is spent. Raise DARWIN_BUDGET_USD to continue."
            )

    def record(self, *, model: str, usage: Any, purpose: str) -> float:
        cost = usage_cost(model, usage)
        with self._locked():
            data = self._read()
            data["spent_usd"] = round(data["spent_usd"] + cost, 8)
            data["calls"] += 1
            for bucket, key in (("by_purpose", purpose), ("by_model", model)):
                entry = data[bucket].setdefault(key, {"usd": 0.0, "calls": 0})
                entry["usd"] = round(entry["usd"] + cost, 8)
                entry["calls"] += 1
            self.path.write_text(json.dumps(data, indent=2))
            with open(self._log_path, "a") as log:
                log.write(json.dumps({"ts": time.time(), "purpose": purpose, "model": model, "usd": round(cost, 8),
                                      "input": getattr(usage, "input_tokens", 0), "output": getattr(usage, "output_tokens", 0),
                                      "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0}) + "\n")
        return cost

    def snapshot(self) -> dict:
        with self._locked():
            data = self._read()
        data["cap_usd"] = self.cap_usd
        data["remaining_usd"] = round(max(0.0, self.cap_usd - data["spent_usd"]), 6)
        return data


class Claude:
    """`messages.create` / `beta.messages.parse` behind the spend cap.

    The Anthropic client is created on the first call, so processes that never call Claude (the Lab, a check,
    tests) never construct one.
    """

    def __init__(self, ledger: SpendLedger, client: anthropic.Anthropic | None = None):
        self.ledger = ledger
        self._client = client

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic(max_retries=4)
        return self._client

    def create(self, *, purpose: str, **request: Any):
        self.ledger.check(estimate_cost(request["model"], request=request), purpose)
        response = self.client.messages.create(**request)
        self.ledger.record(model=response.model or request["model"], usage=response.usage, purpose=purpose)
        return response

    def parse(self, *, purpose: str, **request: Any):
        """Structured output (beta parse). Pass `fallbacks`/`betas` through for refusal fallbacks."""
        self.ledger.check(estimate_cost(request["model"], request=request), purpose)
        response = self.client.beta.messages.parse(**request)
        self.ledger.record(model=response.model or request["model"], usage=response.usage, purpose=purpose)
        return response


def make_claude(data_dir: Path, cap_usd: float) -> Claude:
    return Claude(SpendLedger(Path(data_dir) / "spend.json", cap_usd))
