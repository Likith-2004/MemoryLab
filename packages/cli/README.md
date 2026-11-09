# memorylab (CLI)

A thin client of `/api/v1`: it holds a user session and nothing else (no database, no service keys).

```bash
export MEMORYLAB_API_URL=http://127.0.0.1:8000
export MEMORYLAB_AUTH_URL=http://127.0.0.1:54321/auth/v1
export MEMORYLAB_PUBLISHABLE_KEY=...        # public by design
export MEMORYLAB_WEB_URL=http://localhost:3000   # the Origin the API's WebSocket allowlist expects
memorylab signup / login [--magic-link] / me / games / presets / models
memorylab runs new --config run.json --api-key-stdin --watch
memorylab runs watch RUN_ID        # exit 0 completed|capped, 20 failed, 21 killed
memorylab runs events|checkpoints|score|frame|export|dream|memory ls|cat|diff RUN_ID   # dream: what the dream job changed, before/after
memorylab fork RUN --checkpoint CP --set preset_ref='"none@1"' --max-steps 50 --max-tokens 100000 --max-wall-s 600
memorylab experiments new|list|show|compare|add|remove
memorylab leaderboard / admin runs|kill|breaker|reports|resolve|unverify / delete-account
```

Every command takes `--json` (data on stdout, errors as one JSON document on stderr). Exit codes are stable per API error
`code` (`memorylab_cli/exitcodes.py`; 3 login, 4 not found, 5 invalid, 6 rate limited, 7 key rejected, 11 export, 14 network, 15 sha mismatch, 16 fork disabled).

**The provider key is never an argument.** Give it on stdin (`--api-key-stdin`), in `MEMORYLAB_PROVIDER_KEY` (removed from the
environment once read), or at a hidden prompt. A config file containing a key field is refused. The key is not logged, printed,
stored in the session file, or put in `--json` output. The session is `~/.config/memorylab/session.json` (mode 0600, directory 0700) and is
refreshed automatically. Server text (titles, memory files, model summaries) is shown with terminal control characters escaped.
