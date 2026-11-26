# Darwin demo video: review notes

Source for `docs/video/darwin-demo.mp4` (1920×1080, 30 fps, 2:29.8). Re-render with
`npx hyperframes render --quality delivery --output <file>`; rebuild captions, the SRT and the
narration text with `python3 tools/build_captions.py` after changing any narration clip.

## Shape
| Time | Scene | Real footage |
|---|---|---|
| 0:00 | Hook | `docs/img/hero.png`, Lab lineage with approver Anugrah |
| 0:12.8 | Problem | kinetic type only |
| 0:24.8 | The idea | `diagram-flag.png`, then Flagsmith's Edit Feature panel for `shopbot_brain` |
| 0:40.7 | gen1 fails | 25% stat (lab notebook), a real gen1 transcript in the Lab |
| 0:53.3 | The Coach | the gen2 card rebuilt from the lab notebook's verbatim text (no screenshot of it exists) |
| 1:05.6 | Approval | live gen6 launch card, its 3 CLI commands, "Yes — run 3 commands as Anugrah" |
| 1:17.4 | Flagsmith decides | Flagsmith experiment #1 page: 160 users, >99% winner, 69.1% vs 91.1%, rollout |
| 1:38.2 | More generations | Flagsmith experiments list, results chart (5 experiments) |
| 1:51.3 | The guardrail | live gen6 run: card, experiment #5, Flagsmith 94% banner, Lab 99%→45%, KILL card, approve, Flagsmith record + control rollout |
| 2:13.8 | Close | hero lineage, 6 / 5 / $1.09, tagline end card |

## Calls made
- Voice: Sarvam `bulbul:v4-flash`, `simran_en_narration`, pace 1.0 (Whisper transcribed it most cleanly; `shubh_en_narration` had "Darwin" heard as "Dowing"/"Dhawan").
- Scene 6 uses the live gen6 approval because the dev-run approvals were scripted; its chip says "Live run".
- No music: no royalty-free source was available without a HeyGen sign-in; narration only.
- The tagline is not burned in as captions on the end card (it is printed there); it stays in the SRT.

## Checked
- `hyperframes check`: 0 errors. Kept warnings: the Coach card's long lines are under their reading floor (they are narrated and stay on screen), caption scene density.
- Frames looked at: two full contact-sheet passes over all scenes plus the changed moments.
- Sound was measured (loudness, presence), not heard.

## Weakest shot
- 2:10–2:13 Flagsmith's experiment #5 record: the hypothesis line is small at that framing.
