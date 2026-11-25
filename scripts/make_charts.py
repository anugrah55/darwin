"""Render docs/img/results-{light,dark}.svg from Darwin's lab state: one row per Flagsmith experiment,
champion vs challenger on each metric (a dumbbell per cell), the experiment's primary metric shaded.

    uv run python scripts/make_charts.py

Palette: categorical slots 1-2 (champion blue, challenger orange) and the fixed status colours, validated for
both modes with the dataviz validator. Text uses ink tokens, never series colours.
"""

from __future__ import annotations

import json
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "lab.json"
OUT = ROOT / "docs" / "img"

THEMES = {
    "light": dict(surface="#fcfcfb", ink="#0b0b0b", ink2="#52514e", muted="#898781", grid="#e1e0d9", axis="#c3c2b7",
                  champ="#2a78d6", chall="#eb6834", shade="#f1f0ec", good="#006300", critical="#d03b3b", running="#52514e"),
    "dark": dict(surface="#1a1a19", ink="#f0efec", ink2="#c3c2b7", muted="#898781", grid="#2c2c2a", axis="#383835",
                 champ="#3987e5", chall="#d95926", shade="#232321", good="#0ca30c", critical="#e66767", running="#c3c2b7"),
}
PANELS = [  # metric, title, min, max, ticks, formatter
    ("resolved", "Resolved by ShopBot", 0.5, 1.0, [0.5, 0.75, 1.0], lambda v: f"{v * 100:.0f}%"),
    ("escalated", "Handed to a human", 0.0, 0.3, [0.0, 0.15, 0.3], lambda v: f"{v * 100:.0f}%"),
    ("cost_microusd", "Cost per 1,000 chats", 0.0, 800.0, [0.0, 400.0, 800.0], lambda v: f"${v / 1000:.2f}"),
]
PRIMARY = {"quality": "resolved", "cost": "cost_microusd"}
FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"


def rows_from_state(state: dict) -> list[dict]:
    rows = []
    for name, g in state["generations"].items():
        result = g.get("result") or {}
        readout = result.get("readout")
        if not readout:
            continue
        rows.append({"id": result.get("experiment_id"), "champion": g["parent"], "challenger": name, "goal": g.get("goal", "quality"),
                     "author": g.get("author") or "Coach", "verdict": result.get("verdict"), "readout": readout})
    exp = state.get("experiment")
    if exp and exp.get("readout"):
        rows.append({"id": exp["id"], "champion": exp["champion"], "challenger": exp["challenger"], "goal": exp["goal"],
                     "author": state["generations"][exp["challenger"]].get("author") or "Coach", "verdict": "RUNNING",
                     "readout": exp["readout"]})
    return sorted(rows, key=lambda r: r["id"] or 0)


def mean(readout: dict, metric: str, arm: str) -> float | None:
    a = (readout.get("metrics", {}).get(metric) or {}).get("arms", {}).get(arm)
    return a["total"] / a["n"] if a and a["n"] else None


def render(rows: list[dict], theme: str) -> str:
    t = THEMES[theme]
    left, top, row_h, panel_w, gap = 270, 140, 58, 210, 56
    width = left + len(PANELS) * panel_w + (len(PANELS) - 1) * gap + 70
    height = top + len(rows) * row_h + 56
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
           f'font-family="{FONT}" role="img" aria-label="Flagsmith experiment results per Darwin generation">',
           f'<rect width="100%" height="100%" rx="12" fill="{t["surface"]}"/>',
           f'<text x="24" y="38" font-size="19" font-weight="650" fill="{t["ink"]}">Every generation vs the champion it challenged</text>',
           f'<text x="24" y="62" font-size="13.5" fill="{t["ink2"]}">One row per Flagsmith experiment, ≈160 simulated customers split 50/50. '
           f'Shaded cell = the experiment\'s goal metric.</text>']
    # legend
    lx = left
    for label, color in (("champion (control)", t["champ"]), ("challenger", t["chall"])):
        out.append(f'<circle cx="{lx + 6}" cy="92" r="5" fill="{color}" stroke="{t["surface"]}" stroke-width="2"/>')
        out.append(f'<text x="{lx + 18}" y="96.5" font-size="13" fill="{t["ink2"]}">{label}</text>')
        lx += 170
    for p, (metric, title, lo, hi, ticks, fmt) in enumerate(PANELS):
        x0 = left + p * (panel_w + gap)
        sx = lambda v, x0=x0, lo=lo, hi=hi: x0 + (min(max(v, lo), hi) - lo) / (hi - lo) * panel_w  # noqa: E731
        out.append(f'<text x="{x0}" y="{top - 12}" font-size="13.5" font-weight="600" fill="{t["ink"]}">{escape(title)}</text>')
        for k, tick in enumerate(ticks):
            x = sx(tick)
            anchor = "start" if k == 0 else "end" if k == len(ticks) - 1 else "middle"
            out.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + len(rows) * row_h}" stroke="{t["grid"]}" stroke-width="1"/>')
            out.append(f'<text x="{x:.1f}" y="{top + len(rows) * row_h + 18}" font-size="12" fill="{t["muted"]}" text-anchor="{anchor}">{fmt(tick)}</text>')
        for i, r in enumerate(rows):
            cy = top + i * row_h + row_h / 2
            if PRIMARY.get(r["goal"]) == metric:
                out.append(f'<rect x="{x0 - 8}" y="{top + i * row_h + 4}" width="{panel_w + 16}" height="{row_h - 8}" rx="8" fill="{t["shade"]}"/>')
            c, ch = mean(r["readout"], metric, "control"), mean(r["readout"], metric, r["challenger"])
            if c is None or ch is None:
                continue
            out.append(f'<line x1="{sx(c):.1f}" y1="{cy}" x2="{sx(ch):.1f}" y2="{cy}" stroke="{t["axis"]}" stroke-width="2" stroke-linecap="round"/>')
            for v, color in ((c, t["champ"]), (ch, t["chall"])):
                out.append(f'<circle cx="{sx(v):.1f}" cy="{cy}" r="5.5" fill="{color}" stroke="{t["surface"]}" stroke-width="2"/>')
            if PRIMARY.get(r["goal"]) == metric:  # label only the goal metric: sparse direct labels
                a, b = (c, ch) if sx(c) <= sx(ch) else (ch, c)
                out.append(f'<text x="{sx(a) - 9:.1f}" y="{cy + 4.5}" font-size="12" fill="{t["ink2"]}" text-anchor="end">{fmt(a)}</text>')
                out.append(f'<text x="{sx(b) + 9:.1f}" y="{cy + 4.5}" font-size="12" fill="{t["ink2"]}">{fmt(b)}</text>')
    for i, r in enumerate(rows):
        y = top + i * row_h + row_h / 2
        icon, word, color = {"SHIP": ("✓", "shipped", t["good"]), "KILL": ("✗", "killed", t["critical"]),
                             "DISCARD": ("✗", "discarded", t["critical"])}.get(r["verdict"], ("●", "running", t["running"]))
        author = "Coach" if r["author"] in ("coach", "Coach") else "teammate"
        goal = "resolve more" if r["goal"] == "quality" else "cut cost"
        out.append(f'<text x="24" y="{y - 3}" font-size="14.5" font-weight="600" fill="{t["ink"]}">#{r["id"]}  {r["champion"]} → {r["challenger"]}</text>')
        out.append(f'<text x="24" y="{y + 15}" font-size="12.5" fill="{t["ink2"]}">{author} · {goal} · '
                   f'<tspan fill="{color}" font-weight="600">{icon} {word}</tspan></text>')
    out.append("</svg>")
    return "\n".join(out)


def main() -> None:
    rows = rows_from_state(json.loads(STATE.read_text()))
    OUT.mkdir(parents=True, exist_ok=True)
    for theme in THEMES:
        (OUT / f"results-{theme}.svg").write_text(render(rows, theme))
    print(f"wrote results-light.svg and results-dark.svg ({len(rows)} experiments)")


if __name__ == "__main__":
    main()
