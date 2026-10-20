# Codex Anywhere

This repository implements a Codex-only control plane. Read `docs/protocol.md` before changing the node/hub/browser contract. The node owns the Codex process; the hub persists a query projection and durable commands.

- Preserve native Codex Thread / Turn / Item payloads and unknown events.
- Keep runtime processes independent from Flask requests and browser lifetimes.
- Never silently disable Codex approvals or widen paired project roots.
- Journal commands before delivery. Unknown execution outcomes must not be automatically retried.
- Keep credentials, `.venv`, node state, databases, generated frontend output and attachments out of Git.
- For structural code exploration, use the project's CodeGraph tools when indexed. If `.codegraph/` is absent or the server reports not initialized, ask whether to run `codegraph init -i`; do not initialize without the user's answer. Do not delegate architecture exploration.
- Verify Python changes with `.venv/Scripts/python.exe -m pytest` and `.venv/Scripts/python.exe -m ruff check codex_anywhere tests` (POSIX: `.venv/bin/python`). Verify the frontend with `npm run build` in `web/`.
- Tests use fake Codex peers; live inference is not required by the automated suite. Report which operating systems and deployment targets were actually exercised.
