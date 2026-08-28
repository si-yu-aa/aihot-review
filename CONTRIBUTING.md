# Contributing

## Local checks

AI HOT Review has no runtime dependencies beyond Python 3.10+.

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile server.py
node --check app.js
```

Keep pull requests focused. Do not commit files from `data/`, personal Miner
trees, review decisions, logs, cookies, tokens, or API credentials.

## Design constraints

- Preserve the zero-runtime-dependency path when possible.
- Keep the default listener on `127.0.0.1`.
- Treat upstream summaries as generated summaries, not verified quotations.
- Keep raw pull snapshots separate from append-only user decisions and views.
- Add regression coverage for inbox merging, caching, and state writes.
