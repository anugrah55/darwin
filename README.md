<!-- DRAFT — numbers, screenshots and links marked TODO get filled from real runs during the hackathon. -->

# Darwin

**Your AI agent evolves in production. Flagsmith decides who survives. You approve every mutation.**

Built for Toggle Wars (WeMakeDevs × Flagsmith) — Track 02: AI Agents and Feature Flags.

▶️ Demo video: TODO · 🧪 Lab notebook from our run: [`LAB_NOTEBOOK.md`](LAB_NOTEBOOK.md) (TODO)

![Darwin Lab dashboard](docs/img/dashboard.png) <!-- TODO -->

---

## The problem

Teams change the prompts behind their AI features on gut feel: tweak, read five outputs, deploy, hope.
Offline evals don't show how real users react, and nobody re-checks after shipping. Working out *why* the
agent fails, writing the fix, setting up an A/B test, watching it and rolling out the winner are all manual.

## What Darwin does

ShopBot, a customer-support agent, reads its whole brain — system prompt, model and effort — from **one
Flagsmith multivariate flag**. Darwin's **Coach** agent reads the chats ShopBot failed, finds the patterns,
and writes a challenger. **Flagsmith Experimentation** runs champion vs challenger on live traffic and its
Bayesian engine decides who's better. **A human approves** every step that touches live traffic.

| Generation | Change | Resolution rate | Chance to beat parent | Decision |
|---|---|---|---|---|
| gen1 | baseline prompt | TODO | — | — |
| gen2 | TODO (from Coach) | TODO | TODO | shipped ✅ |
| gen3 | TODO | TODO | TODO | killed by guardrail 🛑 / shipped ✅ |

## How it works

```
 Flagsmith flag `shopbot_brain`:  control = champion   ·   variant = challenger
        │ get_experiment_flag (records exposure)            ▲ results: win probability, lift, CI, SRM
        ▼                                                   │
 customers ─► ShopBot ─► events: resolved · escalated · tokens_used ─► Flagsmith Experimentation
                 │                                                   │
          transcripts ─► Coach (diagnose → mutate) ─► proposal ─► HUMAN ─► Darwin runs Flagsmith CLI
```

1. **Observe** — ShopBot logs every chat and sends exposures and outcome events through the Flagsmith SDK.
2. **Diagnose** — the Coach groups failed chats into patterns ("38% of failures: refund window never stated").
3. **Mutate** — the Coach writes a challenger prompt with a diff, its evidence and the expected effect.
4. **Approve** — you review it. Approving adds the variant to the flag and creates and starts the experiment.
5. **Select** — Flagsmith splits traffic and scores both versions. Darwin watches two guardrails:
   escalations to a human and tokens used, both "should not increase".
6. **Approve** — at ≥ 95% chance to win, Darwin proposes shipping. Approving ends the experiment and makes
   the winner the flag's default value. Everyone gets it instantly — **no redeploy**.

## The safety model

- **The AI never decides.** Flagsmith's stats engine scores; fixed rules (Flagsmith's own 95% / 5% thresholds,
  minimum sample sizes, sample-ratio-mismatch check) route; a human approves.
- **Nothing touches live traffic without a click.** Every approval card shows the evidence and the exact
  Flagsmith CLI commands it will run.
- **Guardrails can veto a winner.** A challenger that resolves more chats but escalates more, or costs much
  more, gets a kill proposal instead.
- Flagsmith's MCP server deliberately won't start, end or roll out experiments. Darwin keeps that line: the
  Coach only proposes; gated actions run only after approval.

## How Darwin uses Flagsmith

| Flagsmith piece | What it does in Darwin |
|---|---|
| Multivariate flag `shopbot_brain` | Holds the agent's config: control = champion, variant = challenger |
| SDK `get_experiment_flag` / `track_event` | Exposures, and the `resolved`, `escalated`, `tokens_used` events |
| Experimentation metrics | `resolved` (primary), `escalated` + `tokens_used` (guardrails) |
| Experiments API (via `flagsmith api`) | Create, start, refresh and read results, complete |
| CLI `feature variant add`, `flag update` | Add each challenger; ship the winner as the flag's default |
| Bayesian results | Win probability, lift, credible interval, SRM — the basis of every decision |

## Run it yourself

TODO — setup steps (Flagsmith Cloud project + warehouse, keys, `flagsmith` CLI, `uv sync`,
`scripts/setup_flagsmith.py`, start ShopBot, start the dashboard, run a round).

## What's real and what's simulated

A hackathon has no real customers, so `sim/` plays them: each simulated customer has a hidden goal and a
**rule-based success check** (for example: "the bot created a return for order A1001"), so results don't rest
on one AI grading another. The Coach never sees those rules, and order IDs, dates and phrasing change every
round, so a better prompt has to generalise rather than teach to the test. The chat page sends real events
into the same experiment, and in production real
users' "this solved my problem" clicks replace the simulator without code changes. Everything on the Flagsmith
side — flags, exposures, events, experiments, statistics — is real.

## Limitations

- Experimentation is in beta; endpoints may change.
- Results refresh at most once every 5 minutes.
- Remote evaluation only (an Experimentation requirement).
- TODO: anything we hit during the build.

## Built with

Flagsmith (Cloud, Experimentation, CLI v2, Python SDK) · Claude API (Coach: Claude Opus 5.5) · Python ·
FastAPI

AI coding tools were used to help build this project.
