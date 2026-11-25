# Darwin — demo video script (target 2:45, hard cap 3:00)

Judges score the problem, what was built, and how Flagsmith fits in. Every scene shows Flagsmith doing work.
Numbers in quotes are from the development run (see LAB_NOTEBOOK.md). Re-record with your own run and update them.

| Time | Scene | On screen | Narration (draft) |
|---|---|---|---|
| 0:00–0:12 | Hook | Lab lineage: gen1 → gen5, each card with Flagsmith's verdict | "This support bot has rewritten itself four times. Every version had to beat the last one in a Flagsmith experiment before it was allowed to ship." |
| 0:12–0:30 | Problem | Slide: tweak → eyeball → deploy → hope | "Teams change the prompts behind their AI features on gut feel. Offline evals miss how real customers react, and nobody checks after shipping." |
| 0:30–0:45 | Setup | Flagsmith: the `shopbot_brain` flag, its value is JSON (prompt, model, effort) | "Darwin keeps the agent's whole brain in one Flagsmith flag. Changing the agent is a flag change — no redeploy." |
| 0:45–1:00 | Gen 1 fails | ShopBot chat: angry customer, "get me a human" → instant escalation. Lab: 73% resolved, 25% handed to humans | "Generation one hands a quarter of its chats to humans — often ones it could have solved." |
| 1:00–1:20 | Coach | Lab card: patterns with counts and quotes (×9 escalated on first ask, ×8 offered a specialist after fixing it), the prompt diff | "The Coach reads every failed chat, finds the patterns, and writes a fix with a hypothesis we can test." |
| 1:20–1:35 | Approval 1 | Card: the exact Flagsmith CLI commands → click Approve → Flagsmith: new variant, running experiment | "Nothing goes live until I approve, and I can see exactly what will run." |
| 1:35–1:55 | Selection | `make round` time-lapse; Flagsmith results page: >99% winner, +32% lift, credible interval | "Flagsmith splits the traffic and its Bayesian engine scores both versions: 91% resolved versus 69%." |
| 1:55–2:10 | Approval 2 + ship | Ship card → Approve → Flagsmith: "gen2 rolled out to everyone" → a new chat now uses gen2 | "One click and every customer gets the winner. No deploy." |
| 2:10–2:30 | Twist | A teammate's two-line prompt (gen4): 27% cheaper, ships. Next round: the Coach's gen5 restores what it lost → 100% vs 85% | "Darwin doesn't care who wrote the prompt — a teammate's shortcut saved 27%, and the next experiment caught what it cost us." |
| 2:30–2:45 | Close | Lab: 24 Flagsmith changes made for you, 54 chats read; lab notebook | "Your AI agent evolves. Flagsmith decides who survives. You approve every change." |

## Record during the run
- Screen capture of the Lab for the whole run (time-lapse material).
- Flagsmith dashboard: the flag before and after a ship, the experiment results page, the metrics list.
- One real chat per generation on the ShopBot page (the human path).
- Terminal: `darwin approve` showing the commands, or the Lab's command list after an approval.

## Rules for the cut
- Show real numbers only; say on screen that customers are simulated ("160 simulated customers per experiment").
- Keep Flagsmith on screen for at least 40% of the video.
- Captions on; music low.
