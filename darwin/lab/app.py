"""Darwin Lab server: JSON API over the Steward, plus the single-page dashboard.

    uv run darwin lab      # http://127.0.0.1:8200

Slow actions (the Coach, Flagsmith command sequences) run on a worker thread; the page polls /api/state.
While an experiment runs, the Lab checks Flagsmith's results on its own every DARWIN_AUTO_CHECK_SECONDS.
"""

from __future__ import annotations

import os
import threading
import time
import traceback
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from darwin import transcripts
from darwin.config import Settings
from darwin.steward import Steward

STATIC = Path(__file__).parent / "static"


class Act(BaseModel):
    actor: str = "you"
    card_id: str | None = None
    note: str | None = None
    goal: str = "quality"
    reason: str = ""


class Lab:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.steward = Steward(settings)
        self.toasts: list[dict] = []
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.auto_every = float(os.environ.get("DARWIN_AUTO_CHECK_SECONDS", "60"))
        threading.Thread(target=self._auto_check, daemon=True).start()

    def toast(self, kind: str, text: str) -> None:
        self.toasts = (self.toasts + [{"ts": time.time(), "kind": kind, "text": text}])[-6:]

    def run(self, label: str, fn, *args, **kwargs) -> None:
        with self._lock:
            if self._worker and self._worker.is_alive():
                raise HTTPException(409, "Darwin is busy with another action")

            def work():
                try:
                    fn(*args, **kwargs)
                    self.toast("ok", f"{label}: done")
                except Exception as exc:  # surfaced to the page as a toast
                    traceback.print_exc()
                    self.toast("error", f"{label}: {exc}")

            self._worker = threading.Thread(target=work, daemon=True)
            self._worker.start()

    def _auto_check(self) -> None:
        while not self._stop.wait(self.auto_every):
            try:
                s = self.steward.store.read()
                if (s.get("experiment") or s.get("finalize")) and not (self._worker and self._worker.is_alive()):
                    # "auto": ask Flagsmith to recompute only once there are 50 chats per arm and new chats since
                    # the last request, so the 5-minute refresh window isn't spent right after launch.
                    self.steward.check("darwin", refresh="auto")
            except Exception:
                traceback.print_exc()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    app = FastAPI(title="Darwin Lab")
    lab = Lab(settings)

    @app.get("/")
    def page():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    def state():
        st = lab.steward.status()
        st["toasts"] = lab.toasts
        st["shopbot_url"] = settings.shopbot_url
        st["working"] = bool(lab._worker and lab._worker.is_alive())
        st["auto_check_seconds"] = lab.auto_every
        return st

    @app.post("/api/evolve")
    def evolve(body: Act):
        lab.run("Coach proposal", lab.steward.propose, goal=body.goal, actor=body.actor)
        return {"started": True}

    @app.post("/api/approve")
    def approve(body: Act):
        lab.run("Approval", lab.steward.approve, body.actor, card_id=body.card_id)
        return {"started": True}

    @app.post("/api/reject")
    def reject(body: Act):
        lab.run("Revision" if body.note else "Rejection", lab.steward.reject, body.actor, note=body.note)
        return {"started": True}

    @app.post("/api/check")
    def check(body: Act):
        lab.run("Results check", lab.steward.check, body.actor)
        return {"started": True}

    @app.post("/api/stop")
    def stop(body: Act):
        lab.run("Stop", lab.steward.abort, body.actor, body.reason)
        return {"started": True}

    @app.get("/api/transcripts")
    def chats(generation: str | None = None, outcome: str | None = None, limit: int = 40):
        rows = transcripts.load(settings.data_dir / "transcripts.jsonl", generation=generation)
        if outcome == "failed":
            rows = [r for r in rows if not r.get("resolved")]
        elif outcome == "resolved":
            rows = [r for r in rows if r.get("resolved")]
        rows = rows[-limit:][::-1]
        return [{"session_id": r["session_id"], "generation": r["generation"], "variant": r.get("variant"),
                 "channel": r.get("channel"), "resolved": r["resolved"], "escalated": r["escalated"], "note": r.get("note"),
                 "turns": r["turns"], "cost_usd": r.get("cost_usd"), "ended_at": r.get("ended_at")} for r in rows]

    return app
