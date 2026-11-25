"""ShopBot server: a customer-support agent whose whole brain is one Flagsmith flag.

    uv run darwin shopbot        # http://127.0.0.1:8100 — chat page for humans, JSON API for the simulator

Per conversation: one `get_experiment_flag` call picks the genome (and records the exposure); at the end,
`resolved`, `escalated` and `cost_microusd` events go to Flagsmith, and the transcript goes to
data/transcripts.jsonl for the Coach.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from darwin.brain import Assignment, Brain
from darwin.config import Settings
from darwin.genome import Genome
from darwin.llm import BudgetExceeded, make_claude, usage_cost

from .agent import run_turn
from .store import Store, demo_orders

HERE = Path(__file__).parent
# The bundled genome: served when the flag is off (the kill switch) or missing, and when Flagsmith is unreachable
# before Brain has seen a good genome. Otherwise an unreachable Flagsmith means the last good genome.
GEN1 = Genome.model_validate_json(Path(os.environ.get("SHOPBOT_FALLBACK_GENOME", HERE / "genomes" / "gen1.json")).read_text())


@dataclass
class Session:
    id: str
    user_id: str
    channel: str
    assignment: Assignment
    started_at: float = field(default_factory=time.time)
    messages: list = field(default_factory=list)
    turns: list = field(default_factory=list)
    usage: dict = field(default_factory=lambda: {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0})
    cost_usd: float = 0.0
    escalated: bool = False
    ended: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def tokens(self) -> int:
        return sum(self.usage.values())


class StartBody(BaseModel):
    user_id: str | None = None
    channel: str = "web"


class MessageBody(BaseModel):
    text: str


class EndBody(BaseModel):
    resolved: bool
    note: str | None = None


class SeedBody(BaseModel):
    orders: list[dict]


class ShopBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.claude = make_claude(settings.data_dir, settings.budget_usd)
        self.store = Store()
        for order in demo_orders(self.store.today):
            self.store.add(order)
        self.brain = (
            Brain(settings.flag, fallback=GEN1, server_key=settings.flagsmith_server_key,
                  api_url=settings.flagsmith_sdk_api_url, events_url=settings.flagsmith_events_url,
                  cache_path=settings.data_dir / "brain-last-good.json")
            if settings.flagsmith_server_key else None
        )
        self.sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        self._transcripts = settings.data_dir / "transcripts.jsonl"
        self._transcripts.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()

    def start(self, body: StartBody) -> Session:
        user_id = body.user_id or f"web-{uuid.uuid4().hex[:10]}"
        if self.brain is not None:
            assignment = self.brain.assign(user_id)
        else:
            assignment = Assignment(user_id, GEN1, "fallback", "fallback", reason="FLAGSMITH_SERVER_KEY not set")
        session = Session(uuid.uuid4().hex[:12], user_id, body.channel, assignment)
        with self._lock:
            self.sessions[session.id] = session
        return session

    def get(self, sid: str) -> Session:
        session = self.sessions.get(sid)
        if session is None:
            raise HTTPException(404, "unknown session")
        return session

    @staticmethod
    def _add_cost(session: Session, usage: dict | None) -> None:
        if not usage:
            return
        for k, v in usage.items():
            session.usage[k] += v
        session.cost_usd += usage_cost(session.assignment.genome.model, {
            "input_tokens": usage["input"], "output_tokens": usage["output"],
            "cache_read_input_tokens": usage["cache_read"], "cache_creation_input_tokens": usage["cache_write"]})

    def message(self, session: Session, text: str) -> dict:
        with session.lock:
            if session.ended:
                raise HTTPException(409, "conversation has ended")
            messages_before, turns_before = len(session.messages), len(session.turns)
            session.messages.append({"role": "user", "content": text})
            session.turns.append({"role": "customer", "text": text})
            try:
                turn = run_turn(self.claude, session.assignment.genome, session.messages, self.store,
                                purpose=f"shopbot:{session.channel}")
            except BudgetExceeded as exc:
                # The cap stopped this turn partway. Drop the customer's message and everything the agent added after
                # it (a tool_use without its result would make the next request invalid), but keep what it cost.
                del session.messages[messages_before:]
                del session.turns[turns_before:]
                self._add_cost(session, getattr(exc, "usage", None))
                raise HTTPException(402, str(exc)) from exc
            self._add_cost(session, turn.usage)
            session.escalated = session.escalated or turn.escalated
            session.turns.append({"role": "bot", "text": turn.reply, "tools": turn.tool_calls, "stop_reason": turn.stop_reason})
            return {"reply": turn.reply, "tools": turn.tool_calls, "escalated": session.escalated,
                    "generation": session.assignment.genome.name}

    def end(self, session: Session, body: EndBody) -> dict:
        with session.lock:
            if session.ended:
                return {"ok": True, "already_ended": True}
            session.ended = True
        a = session.assignment
        events = []
        if body.resolved:
            a.track("resolved")
            events.append("resolved")
        if session.escalated:
            a.track("escalated")
            events.append("escalated")
        cost_microusd = round(session.cost_usd * 1_000_000)
        a.track("cost_microusd", cost_microusd)
        events.append(f"cost_microusd={cost_microusd}")
        record = {
            "session_id": session.id, "user_id": session.user_id, "channel": session.channel,
            **{k: v for k, v in a.to_dict().items() if k != "identifier"},
            "started_at": session.started_at, "ended_at": time.time(),
            "resolved": body.resolved, "escalated": session.escalated, "note": body.note,
            "turns": session.turns, "usage": session.usage, "tokens": session.tokens, "cost_usd": round(session.cost_usd, 6),
        }
        with self._write_lock, open(self._transcripts, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        with self._lock:
            self.sessions.pop(session.id, None)
        return {"ok": True, "events": events}

    def close(self) -> None:
        if self.brain is not None:
            self.brain.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    holder: dict[str, ShopBot] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        holder["bot"] = ShopBot(settings)
        yield
        holder["bot"].close()

    app = FastAPI(title="ShopBot", lifespan=lifespan)
    bot = lambda: holder["bot"]  # noqa: E731

    @app.get("/")
    def page():
        return FileResponse(HERE / "static" / "chat.html")

    @app.get("/health")
    def health():
        b = bot()
        return {"ok": True, "flag": settings.flag, "flagsmith": b.brain is not None, "fallback": GEN1.name,
                "open_sessions": len(b.sessions), "spend": b.claude.ledger.snapshot()}

    @app.get("/api/demo-orders")
    def demo():
        b = bot()
        return [o.view(b.store.today) for o in demo_orders(b.store.today)]

    @app.post("/chat/start")
    def start(body: StartBody):
        s = bot().start(body)
        return {"session_id": s.id, **s.assignment.to_dict()}

    @app.post("/chat/{sid}/message")
    def message(sid: str, body: MessageBody):
        b = bot()
        return b.message(b.get(sid), body.text)

    @app.post("/chat/{sid}/end")
    def end(sid: str, body: EndBody):
        b = bot()
        return b.end(b.get(sid), body)

    @app.post("/_sim/seed")
    def seed(body: SeedBody):
        b = bot()
        for raw in body.orders:
            b.store.add_dict(raw)
        return {"added": len(body.orders), "orders": len(b.store)}

    @app.post("/_sim/flush")
    def flush():
        b = bot()
        if b.brain is not None:
            b.brain.flush()
        return {"ok": True}

    return app
