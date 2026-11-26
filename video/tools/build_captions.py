"""Build the burned-in captions scene, the SRT and the narration text from the narration script.

Display text is the script below (numerals for numbers); timings come from the whisper word
timings of each scene's voice clip (assets/voice/words/wNN.json), matched word by word.

    python3 tools/build_captions.py

Keep SCENES in step with the scene hosts in index.html (start, voice offset).
"""
import difflib
import html
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT.parent / "docs" / "video"

# id, scene start (s), voice offset inside the scene (s), caption text
SCENES = [
    ("s01", 0.0, 1.2, "This support bot has been rewritten five times, mostly by an AI coach. Every new version had to beat the champion in a live Flagsmith experiment, and a person approves every change."),
    ("s02", 12.8, 0.6, "Today, teams change the prompts behind their AI features on gut feel. Tweak the prompt, read a few answers, deploy, and hope. Nobody measures what customers actually experience."),
    ("s03", 24.8, 0.6, "Darwin puts the agent's whole brain, its prompt, model and effort, into one Flagsmith multivariate flag. The champion is the control, and every challenger is a variant. So changing the agent is a flag change, with no redeploy."),
    ("s04", 40.7, 0.6, "Our first version handed a quarter of its chats to humans. Here, it fixes a late order, then offers a specialist anyway. The customer says yes, and a human gets a ticket that was already solved."),
    ("s05", 53.3, 0.6, "Darwin's Coach, built on Claude Opus, read 28 failed chats. It found the patterns, with counts and real quotes, and wrote a challenger with a hypothesis that Flagsmith can test."),
    ("s06", 65.6, 0.6, "Nothing goes live until a person approves. Every card shows the evidence, the prompt diff, and the exact Flagsmith CLI commands it will run, in the approver's name."),
    ("s07", 77.4, 0.6, "Flagsmith splits the traffic 50/50, across 160 simulated customers, and its Bayesian engine scores both versions. The challenger resolved 91% of chats, against 69%, with a 99.8% chance to win. One more approval, and Flagsmith rolls it out to everyone. No deploy."),
    ("s08", 98.2, 0.6, "Then the Coach cut cost by 8%. A teammate's two-line prompt cut another 27%, but it had dropped the escalation rules. So the Coach put back only what mattered: 100% resolved."),
    ("s09", 111.3, 0.6, "In a live run, a teammate tried skipping policy lookups to save money, and Anugrah approved the test. Flagsmith, scoring cost alone, gave it a 94% chance to win. But resolution collapsed, from 99% to 45%. Darwin's guardrail recommended a kill, Anugrah approved it, and every customer stayed on the champion."),
    ("s10", 133.8, 0.6, "Six generations, five Flagsmith experiments, and about a dollar of Claude spend. Darwin. Your AI agent evolves. Flagsmith decides who survives. You approve every change."),
]
# the spoken script (numbers as words), for narration.txt
SPOKEN = json.loads((ROOT / "tools" / "narration.json").read_text())
TOTAL = 149.8


def norm(w):
    return re.sub(r"[^a-z0-9]", "", w.lower().replace("'s", ""))


def groups(text, max_words=6):
    out = []
    for phrase in re.split(r"(?<=[,.:])\s+", text):
        words = phrase.split()
        while len(words) > max_words:
            cut = max_words if len(words) - max_words >= 2 else len(words) - 2
            out.append(words[:cut])
            words = words[cut:]
        if words:
            out.append(words)
    merged = []
    for g in out:  # fold one-word fragments into the previous group when it stays short
        if merged and len(g) == 1 and len(merged[-1]) + 1 <= 7 and not merged[-1][-1].endswith("."):
            merged[-1] = merged[-1] + g
        else:
            merged.append(g)
    return merged


def timed_words(sid, text):
    raw = json.loads((ROOT / "assets" / "voice" / "words" / f"w{sid[1:]}.json").read_text())
    raw = raw["words"] if isinstance(raw, dict) else raw
    script = text.split()
    a = [norm(w) for w in script]
    b = [norm(w["text"]) for w in raw]
    times = [None] * len(script)
    for blk in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
        for k in range(blk.size):
            times[blk.a + k] = (raw[blk.b + k]["start"], raw[blk.b + k]["end"])
    # interpolate unmatched words between anchors
    end_all = raw[-1]["end"]
    for i, t in enumerate(times):
        if t is None:
            prev = next((times[j][1] for j in range(i - 1, -1, -1) if times[j]), 0.0)
            nxt_i = next((j for j in range(i + 1, len(times)) if times[j]), None)
            nxt = times[nxt_i][0] if nxt_i is not None else end_all
            gap = nxt_i - i if nxt_i is not None else len(times) - i
            step = (nxt - prev) / (gap + 1)
            times[i] = (prev + step * 0.5, prev + step)
    return list(zip(script, times))


def main():
    cues = []
    for sid, start, off, text in SCENES:
        tw = timed_words(sid, text)
        k = 0
        for g in groups(text):
            ws = tw[k:k + len(g)]
            k += len(g)
            cues.append([start + off + ws[0][1][0] - 0.08, start + off + ws[-1][1][1] + 0.12, " ".join(g)])
    # one group at a time: close each group when the next opens; hold short gaps
    for i in range(len(cues) - 1):
        nxt = cues[i + 1][0]
        if nxt - cues[i][1] < 0.6:
            cues[i][1] = nxt - 0.02
        cues[i][1] = min(cues[i][1], nxt - 0.02)
    for c in cues:
        c[0], c[1] = round(max(c[0], 0.15), 2), round(c[1], 2)

    rows = []
    script = []
    # the end card prints the tagline itself, so it is not burned in twice (it stays in the SRT)
    burn = [c for c in cues if not (c[0] >= 139.8)]
    for i, (a, b, t) in enumerate(burn):
        rows.append(f'      <div id="cap-{i:03d}" class="cap clip" data-start="{a}" data-duration="{round(b - a, 2)}" data-track-index="1"><span>{html.escape(t)}</span></div>')
        script.append(f'      fade("#cap-{i:03d}", {a}, {b});')
    doc = f"""<template>
  <style>
    #root {{ position: absolute; inset: 0; pointer-events: none; }}
    .cap {{ position: absolute; left: 0; right: 0; bottom: 64px; text-align: center; overflow: visible; z-index: 50; }}
    .cap span {{
      display: inline-block; max-width: 1500px; padding: 10px 26px 12px; border-radius: 14px;
      background: rgba(10, 10, 10, 0.78); color: #f4f3ef;
      font-family: Inter, sans-serif; font-weight: 700; font-size: 44px; line-height: 1.22; letter-spacing: -0.01em;
    }}
  </style>
  <div id="root" data-composition-id="captions" data-width="1920" data-height="1080" data-layout-allow-caption-zone>
{chr(10).join(rows)}
  </div>
  <script>
    const tl = gsap.timeline({{ paused: true }});
    function fade(sel, a, b) {{
      const s = document.querySelector(sel + " span");
      tl.fromTo(s, {{ opacity: 0 }}, {{ opacity: 1, duration: 0.12, ease: "power1.out" }}, a);
      tl.to(s, {{ opacity: 0, duration: 0.1, ease: "power1.in" }}, b - 0.1);
    }}
{chr(10).join(script)}
    window.__timelines["captions"] = tl;
  </script>
</template>
"""
    (ROOT / "compositions" / "captions.html").write_text(doc)

    def ts(x):
        ms = int(round(x * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / "captions.srt").write_text("".join(f"{i + 1}\n{ts(a)} --> {ts(b)}\n{t}\n\n" for i, (a, b, t) in enumerate(cues)))
    names = ["Hook", "The problem", "The idea: one flag holds the agent's brain", "Generation 1 fails",
             "The Coach writes a challenger", "A person approves", "Flagsmith decides", "More generations",
             "The guardrail (live run)", "Close"]
    lines = ["Darwin: demo video narration", "Voice: Sarvam AI bulbul:v4-flash, speaker simran_en_narration (en-IN), pace 1.0", ""]
    for (sid, start, off, _), name, (_, spoken) in zip(SCENES, names, SPOKEN):
        lines += [f"[{int((start + off) // 60)}:{(start + off) % 60:04.1f}] {name}", spoken, ""]
    (DOCS / "narration.txt").write_text("\n".join(lines))
    print(len(cues), "caption groups; last ends", cues[-1][1])


if __name__ == "__main__":
    main()
