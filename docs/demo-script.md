# Darwin — demo video script (target 2:45, hard cap 3:00)

Judges score: the problem, what was built, and how Flagsmith fits in. Every scene shows Flagsmith doing work.

| Time | Scene | On screen | Narration (draft) |
|---|---|---|---|
| 0:00–0:12 | Hook | ShopBot chat; Darwin Lab tree with 3 generations | "This support bot rewrote its own prompt. Then it had to prove to Flagsmith that the new version was better before it was allowed to keep it." |
| 0:12–0:30 | Problem | Slide: tweak → eyeball → deploy → hope | "Teams change the prompts behind their AI features on gut feel. Offline evals miss how real users react, and nobody checks after shipping." |
| 0:30–0:45 | Setup | Flagsmith dashboard: flag `shopbot_brain`, value = JSON config | "Darwin keeps the bot's whole brain in one Flagsmith flag. Changing it needs no redeploy." |
| 0:45–1:00 | Gen 1 fails | Live chat: bot sends a customer to email instead of creating a return. Lab: resolution 41% (TODO real) | "Generation one resolves 41% of chats." |
| 1:00–1:20 | Coach | Diagnosis card with patterns + counts; prompt diff | "The Coach reads every failed chat, finds the patterns, and writes a fix." |
| 1:20–1:35 | Approval 1 | Approval card showing the exact CLI commands → click Approve → Flagsmith shows new variant + running experiment | "Nothing goes live until I approve. Here's exactly what it will run." |
| 1:35–1:55 | Selection | Time-lapse: simulated customers; Flagsmith results page: chance to win, lift, CI | "Flagsmith splits the traffic and its Bayesian engine scores both versions." |
| 1:55–2:10 | Approval 2 + ship | Ship card → Approve → flag default changes → next live chat uses gen2 | "97% chance to beat the old version, guardrails healthy. One click, and every user gets it. No redeploy." |
| 2:10–2:30 | Twist: guardrail | Gen3 cost-cutting mutation; escalation guardrail breaks; kill card → Approve | "Next, the Coach tries to cut cost. It escalates more chats to humans, so Darwin recommends killing it." (or, if gen3 wins: "Cheaper and just as good, so it ships.") |
| 2:30–2:45 | Close | Architecture slide → family tree + lab notebook | "Your AI agent evolves. Flagsmith decides who survives. You approve every change." |

## Record during the Day 3 full run
- Screen capture of the Lab for the whole run (time-lapse material).
- Flagsmith dashboard: flag page before/after ship, experiment results page, metrics list.
- One real live chat per generation (the human path).
- Terminal: CLI commands executing after Approve.

## Rules for the cut
- Show real numbers only; say on screen that customers are simulated ("300 simulated customers").
- Keep Flagsmith UI on screen at least 40% of the video.
- Captions on; music low.
