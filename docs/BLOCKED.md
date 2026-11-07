# Blocked items

Format: item, what is blocked, why (boundary), what is needed from the owner.

| ID | Item | Blocked by | Needed |
|---|---|---|---|
| B-1 | Real Anthropic model calls and the metered pilot (3 runs per arm A and B) | No `.env` with a key (boundary 1: money) | Put `ANTHROPIC_API_KEY` in `.env` (gitignored) and a spend cap; until then every real-model check is "NOT RUN". |
