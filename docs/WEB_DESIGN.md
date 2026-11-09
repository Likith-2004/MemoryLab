# Web design notes (M7)

One page: what we take from the tools around us, what we reject, and why. The direction itself comes from the spec ("instrument
panel, not marketing"): neutral surface, **one accent for memory everywhere**, monospace for file content and step numbers,
pixel-crisp canvas, no gradients, glass or decorative motion, `prefers-reduced-motion` honoured.

## Sources and how far they were checked

| Source | What I looked at (11-09) | Limit of the check |
|---|---|---|
| LangSmith | docs.langchain.com *observability concepts*: a **run** is one unit of work, runs form a **trace**, traces of a session form a **thread**; a **trajectory** flattens a session into the ordered path of messages. | Docs text only; I did not use the product UI. |
| Langfuse | docs.langfuse.com *Agent Graphs*: the same trace drawn **Aggregated** (one node per kind of step, with counts like "llm.chat (3/3)") or **Expanded** (as it happened), the choice remembered across traces; *Log Levels*. | Docs text only. |
| Weights & Biases | docs.wandb.ai *Panels / Line plots*: metrics over time, several runs on one plot, custom x axes, zoom, hidden legend, per-run colours. | Docs text only. |
| PokeAgent Challenge | pokeagent-challenge.com answered 200 but renders client-side: **no readable content was fetched**. Nothing below is claimed about its UI. | Not studied. Revisit with a browser if the owner wants it. |
| Twitch "plays" overlays | Not fetched (no stable public source). What follows is general knowledge of the pattern (game left, chat/log right, a status strip), not a reading of a specific page. | Treat as opinion. |

## What we take

1. **A trace is an ordered list you can scrub** (LangSmith trajectory, Langfuse expanded view). Our event log is already ordered
   and gapless, so the Log panel is that list, the timeline scrubs it, and Replay is the same layout with the cursor set by `?at=`.
2. **Aggregated vs expanded** (Langfuse). The log has a "group repeats" toggle: runs of identical no-effect `action` events collapse
   to one row with a count; the choice is remembered in `localStorage` (not the server).
3. **Several runs on one x axis, aligned** (W&B). Experiment compare draws every visible run on one step axis, with the run's own colour
   and a legend that can be hidden. Our x axis is the step, never wall time, because arms must be compared on equal footing.
4. **One highlighted dimension that follows you across panels**. Memory has the accent: log chips for `mem.*` events, ticks on the
   timeline where memory changed, highlighted rows in the file tree, the diff gutter. Nothing else uses it.
5. **Status strip above the game** (the streaming-overlay pattern): state, step, tokens vs cap, wall clock vs cap, and Stop. It is the
   only element that stays fixed on phones.

## What we reject

- **Dashboards of many small KPI cards** (common in observability products). A run has one question: what did the agent remember and
  did it help. We show counters only in the Budget tab.
- **Auto-playing chat-style narration** (stream overlays). Model text is evidence, not entertainment: plain text nodes, monospace,
  truncated with "show full", never rendered as markdown or HTML (security rule, enforced by lint).
- **Rendering model output as rich text** in trace views. LangSmith/Langfuse render markdown/JSON nicely; we cannot, because model,
  memory and title text are attacker-influenced.
- **Smoothed or interpolated charts.** Series are step functions (value carries forward until the next observation), as the API
  returns them; no spline smoothing that invents values.
- **A leaderboard that looks like a verdict.** Median + range + n always, and "n < 5" warnings on experiments: the banner names the one
  changed field and refuses to look like a finding.
- **Decorative motion, gradients, glass, emoji.** Only the newest log row animates, and not under `prefers-reduced-motion`.

## Concrete tokens and layout

- Surface `#f6f6f4` / ink `#16181b` (dark: `#121315` / `#e8e8e6`); one accent for memory `#0b6bcb` (dark `#5aa2f0`); status colours only
  for run state and never alone (always with text or an icon). Contrast ≥ 4.5:1 for text, 3:1 for UI, checked by axe.
- Type: system sans for UI, `ui-monospace` for file content, step numbers, hashes and ids; 14 px base, 12 px mono minimum.
- Game canvas: `image-rendering: pixelated`, integer scaling where space allows, aspect 160×144.
- Desktop: game | log on top, timeline under the game, memory tree + file right. < 768 px: status strip, game, then tabs
  **Log · Memory · Budget · Checkpoints**; Stop stays in the strip.
- Keyboard: every control reachable in a logical order, visible focus ring, Escape closes the drawer and modal, `←/→` step the
  timeline, `[`/`]` jump between memory events.

## Components cloned (source-copied, recorded in THIRD_PARTY.md)

Radix UI primitives (MIT) used through shadcn/ui-style wrappers (MIT): Dialog (fork modal, delete confirmation), Tabs (mobile run
tabs), Tooltip, Toast. Copied files carry their origin and licence in a header comment.
