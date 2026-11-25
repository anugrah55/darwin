# Darwin lab notebook

Every generation of `shopbot_brain`: what the Coach saw, what it changed, what Flagsmith measured and who decided.
Written by Darwin as it runs.

## gen1 — champion (2026-11-25 19:43 IST)

Baseline. Darwin starts watching this generation.

- Model `claude-haiku-5-5`, effort `medium`

## gen2 — challenger to gen1 (2026-11-25 19:47 IST)

**Goal:** quality  
**Hypothesis:** If gen2 resolves late-delivery cases (including 'get me a human' requests) by policy and stops offering escalations the policy doesn't require, the resolved rate rises from 73% to about 85% and escalations fall from 25% to under 10%, because 17 of the 24 failures were unnecessary handoffs on cases the policy fully covers.  
**Expected effect:** Resolved: roughly +10 to +15 points. Escalated: down from 25% to around 8–12% (refunds over $500 and customers who insist remain). Cost per chat: roughly flat to +15%. The longer prompt adds input tokens, but fewer escalation turns and fewer get_policy calls (key rules are now in the prompt) partly offset that, staying well under the +30% cap.

**Evidence** (28 chats read; gen1: 120 chats, 73% resolved, 25% escalated):
  - On late deliveries the agent correctly applies the credit, then offers a human 'investigation' the policy doesn't call for; the customer accepts and the chat is escalated. (×8) — e.g. “If the package still hasn't arrived after you've checked tracking, let me know and I can escalate it to a human specialist to investigate with the carrier.”
  - When an upset customer with a late order demands a human, the agent escalates at once without applying the late-delivery policy. Sometimes it doesn't even look up the order. (×9) — e.g. “I've transferred you to a human support specialist (ticket E-1097). They'll follow up by email within 24 hours.”
  - On price-drop requests the agent steers the customer into a refund return and supplies the return reason itself. It creates the return without confirming the item is unused. (×4) — e.g. “Would you like me to start the return? If so, what's the reason? Since it's not damaged or defective, I'd list it as "changed mind."”
  - The agent offers human review for a request that policy clearly denies (a return outside every window, on an item with no warranty). (×1) — e.g. “If you believe there's a special circumstance, such as a defect or damage, I can connect you with a human support specialist to review it.”
  - The agent sees a late order (6+ days) but doesn't check the policy or issue the credit owed, and defers to a later contact. (×1) — e.g. “If it still hasn't arrived after checking tracking, I can look into our late delivery options for you.”
  - Too many questions before acting: after finding the order the agent asked two separate questions (reason and resolution), and the customer left. (×1) — e.g. “Before I start the return, please tell me: 1. Reason for the return... 2. Preferred resolution...”

**Changes:**
  - Added an explicit rule for when to escalate: only when policy requires it (e.g. refunds over $500), or when the customer insists after the policy outcome has been delivered.
  - Added a playbook for human requests: look up the order, apply policy first, then escalate only if the customer still insists.
  - Stated the late-delivery tiers, return windows, warranty, pricing and >$500 rules, quoting the policy text, so the agent acts in one turn.
  - Price drops: decline, explain return-and-rebuy, never suggest a return reason, and confirm the item is unused before creating a return.
  - A clear policy 'no' counts as a resolution; no consolation escalation.
  - Efficiency rules: act within the same turn, ask at most one question, default to refund within 30 days.
  - Model and effort unchanged (claude-haiku-5-5, medium).
- Model `claude-haiku-5-5`, effort `medium`

**Launched** experiment #1, approved by **claude-dev-run**. Commands run:

    flagsmith --yes feature variant add shopbot_brain --key gen2 --value <gen2 genome, 4,376 chars> --type string --weight 0
    flagsmith --yes api api/v1/environments/aQbRhkxx6rbu5gpJvEw3Qe/experiments/ -X POST --input -   # JSON body on stdin
    flagsmith --yes api 'api/v1/environments/aQbRhkxx6rbu5gpJvEw3Qe/experiments/<new experiment id>/start/' -X POST

### gen2: SHIP (2026-11-25 19:54 IST)

Ship gen2: 91% vs 69% for gen1, 99.8% chance to win.

- resolved: gen2 91% vs 69% for gen1 (lift +32%, 95% CI +10% … +53%, 99.8% chance to win)
- escalations to a human: not enough data yet
- cost per chat: +12% (95% CI +2% … +22%)

Experiment #1 completed; approved by **claude-dev-run**.

## gen3 — challenger to gen2 (2026-11-25 19:57 IST)

**Goal:** cost  
**Hypothesis:** If ShopBot creates returns without asking for a reason (defaulting to "other"), stops inviting escalation, skips get_policy for covered topics, and runs at low effort, then cost per chat falls by at least 30% and resolution rises, because the drop-offs and unneeded handoffs seen in 7 of 7 failures disappear while fewer tool calls, turns and reasoning tokens are used.  
**Expected effect:** Resolved rises from about 91% toward 96% or more as the return-reason drop-offs are removed. Escalated falls from about 5% toward 1–2%, since the remaining handoffs should only be refunds over $500 or explicit repeat requests. Cost per chat falls about 30–45%, driven by low effort, fewer get_policy calls and fewer turns per chat.

**Evidence** (17 chats read; gen2: 79 chats, 91% resolved, 5% escalated):
  - The agent asks the customer for a return reason after they have already asked for a return (and confirmed the resolution), and the customer abandons the chat before the return is created. (×5) — e.g. “Happy to set that up. What's the reason for the return? Options include damaged, defective, wrong item, or other.”
  - The agent invites escalation and then treats an ambiguous or accepting reply as a demand for a human, handing off after the policy was already fully applied. (×2) — e.g. “Do you still want me to transfer you to a human specialist, or is there anything else I can help with?”
  - The agent calls get_policy for topics whose rules it could already know (returns, final_sale, cancellations), adding tool calls and tokens to each chat. (×2) — e.g. “[tool] get_policy(topic=final_sale) -> policy[final_sale]: - Items marked final_sale (clearance) can't be returned”

**Changes:**
  - Returns: never ask for a reason; use the customer's reason or "other" and create the return in the same reply.
  - Days 31–45: one yes/no confirmation for store credit, then create the return immediately on yes.
  - Escalation: removed the 'tell me if you still want a human' invitation; escalate only on an explicit repeated request; ambiguous or accepting replies mean close.
  - Added the final-sale, damaged-item and cancellation rules to the prompt, and told the agent not to call get_policy for covered topics.
  - Tightened the wording to keep the prompt compact.
  - Effort lowered from medium to low; model unchanged (claude-haiku-5-5).
- Model `claude-haiku-5-5`, effort `low`

**Launched** experiment #2, approved by **claude-dev-run**. Commands run:

    flagsmith --yes feature variant add shopbot_brain --key gen3 --value <gen3 genome, 4,505 chars> --type string --weight 0
    flagsmith --yes api api/v1/environments/aQbRhkxx6rbu5gpJvEw3Qe/experiments/ -X POST --input -   # JSON body on stdin
    flagsmith --yes api 'api/v1/environments/aQbRhkxx6rbu5gpJvEw3Qe/experiments/<new experiment id>/start/' -X POST

### gen3: SHIP (2026-11-25 20:04 IST)

Ship gen3: cost per chat $0.00067 vs $0.00073 for gen2 (-8%), 99.7% chance it's lower.

- gen3: cost per chat $0.00067 vs $0.00073 for gen2 (-8%, 95% CI -14% … -2%, 99.7% chance it's lower)
- resolution rate: +1% (95% CI -3% … +5%)
- escalations to a human: -1% (95% CI -115% … +114%)

**The Coach predicted:** Resolved rises from about 91% toward 96% or more as the return-reason drop-offs are removed. Escalated falls from about 5% toward 1–2%, since the remaining handoffs should only be refunds over $500 or explicit repeat requests. Cost per chat falls about 30–45%, driven by low effort, fewer get_policy calls and fewer turns per chat.  
**Flagsmith measured:** see above.

Experiment #2 completed; approved by **claude-dev-run**.

## gen4 — challenger to gen3 (2026-11-25 20:05 IST)

**Goal:** cost  
**Hypothesis:** A two-line prompt sends fewer tokens per call, so cost per chat falls without hurting resolution.  
**Expected effect:** Proposed by pm (dev run); not predicted by the Coach.

**Evidence** (0 chats read; gen3: 73 chats, 99% resolved, 7% escalated):


**Changes:**
  - Prompt written by pm (dev run)
- Model `claude-haiku-5-5`, effort `low`

**Launched** experiment #3, approved by **claude-dev-run**. Commands run:

    flagsmith --yes feature variant add shopbot_brain --key gen4 --value <gen4 genome, 343 chars> --type string --weight 0
    flagsmith --yes api api/v1/environments/aQbRhkxx6rbu5gpJvEw3Qe/experiments/ -X POST --input -   # JSON body on stdin
    flagsmith --yes api 'api/v1/environments/aQbRhkxx6rbu5gpJvEw3Qe/experiments/<new experiment id>/start/' -X POST

### gen4: SHIP (2026-11-25 20:12 IST)

Ship gen4: cost per chat $0.00048 vs $0.00066 for gen3 (-27%), 100.0% chance it's lower.

- gen4: cost per chat $0.00048 vs $0.00066 for gen3 (-27%, 95% CI -32% … -23%, 100.0% chance it's lower)
- resolution rate: +1% (95% CI -7% … +9%)
- escalations to a human: -10% (95% CI -80% … +61%)

**The Coach predicted:** Proposed by pm (dev run); not predicted by the Coach.  
**Flagsmith measured:** see above.

Experiment #3 completed; approved by **claude-dev-run**.

## gen5 — challenger to gen4 (2026-11-25 20:12 IST)

**Goal:** quality  
**Hypothesis:** If the prompt restores 'escalate only when policy requires; resolve demands for a human by policy first; never offer escalation' and states the late-delivery tiers, the resolved rate rises from 94% toward 98%+ and escalations drop. The failures are all policy-resolvable late orders that were handed off.  
**Expected effect:** Resolved up about 4–5 points, to about 98–99%. Escalated down from 13% to about 6–8%, leaving only policy-required handoffs such as refunds over $500. Cost per chat up roughly 10–20% from the longer prompt and the extra lookup/credit calls in former instant-escalation chats, partly offset by fewer get_policy calls for late orders. This stays within the 30% cap.

**Evidence** (9 chats read; gen4: 84 chats, 94% resolved, 13% escalated):
  - When an upset customer demands a human about a late order, the agent escalates at once instead of applying the late-delivery policy, which it could resolve itself. (×4) — e.g. “This is ridiculous. Order B10064 was supposed to arrive over a week ago. Get me a human NOW.”
  - After resolving a late order correctly, the agent offers to escalate, and the customer accepts, creating a needless handoff. (×1) — e.g. “If it still hasn't arrived after you check the tracking, let me know and I can escalate it.”

**Changes:**
  - Restored the rule: escalate only when a policy explicitly requires a human (e.g. refunds over $500).
  - Added a rule: a demand for a human (even angry) is not grounds to escalate. Look up the order, apply the policy, and report what was done.
  - Restored the rule: never offer or invite escalation.
  - Stated the late-delivery credit tiers verbatim so the agent applies them without guessing.
  - Kept claude-haiku-5-5 at low effort to control cost.
- Model `claude-haiku-5-5`, effort `low`

**Launched** experiment #4, approved by **claude-dev-run**. Commands run:

    flagsmith --yes feature variant add shopbot_brain --key gen5 --value <gen5 genome, 1,719 chars> --type string --weight 0
    flagsmith --yes api api/v1/environments/aQbRhkxx6rbu5gpJvEw3Qe/experiments/ -X POST --input -   # JSON body on stdin
    flagsmith --yes api 'api/v1/environments/aQbRhkxx6rbu5gpJvEw3Qe/experiments/<new experiment id>/start/' -X POST

### gen5: SHIP (2026-11-25 20:19 IST)

Ship gen5: resolved 100% vs 85% for gen4 (+18%), 100.0% chance it's better.

- gen5: resolved 100% vs 85% for gen4 (+18%, 95% CI +7% … +29%, 100.0% chance it's better)
- escalations to a human: not enough data yet
- cost per chat: +20% (95% CI +12% … +28%)

**The Coach predicted:** Resolved up about 4–5 points, to about 98–99%. Escalated down from 13% to about 6–8%, leaving only policy-required handoffs such as refunds over $500. Cost per chat up roughly 10–20% from the longer prompt and the extra lookup/credit calls in former instant-escalation chats, partly offset by fewer get_policy calls for late orders. This stays within the 30% cap.  
**Flagsmith measured:** see above.

Experiment #4 completed; approved by **claude-dev-run**.

