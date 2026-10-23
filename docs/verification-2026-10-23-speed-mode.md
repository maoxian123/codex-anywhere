# Speed mode controls verification — 2026-10-23

## Behavior

- Existing native Thread metadata does not expose serviceTier. Opening history does not resume or acquire the writer. Unknown current mode remains explicitly unread; installation defaults are reference information only.
- The composer and execution settings offer normal/fast for nodes advertising serviceTierWrite. Desired settings apply on the next new turn; actual mode remains the last native readback until the command is accepted. Offline and older nodes cannot edit mode.
- Public fast maps to native serviceTier=priority, as advertised by the deployed native model catalog; normal sends default. Null removes the desired override and preserves inheritance. No global config write or inference is used to change the saved preference.
- Native mode events arriving during the turn RPC take precedence over its earlier baseline; cached pre-RPC modes cannot hide an accepted explicit change. Native rejections do not fabricate effective settings or retry commands.

## Local validation

- Windows / Python: full pytest suite, 736 passed, 2 skipped. Fake Codex peers only.
- Windows: Ruff check codex_anywhere tests and git diff --check passed.
- Windows / frontend: 210 tests passed; TypeScript, production build and Prettier check passed.
- Independent review and 14 focused node integration regressions passed.

## Deployment boundaries

The tested committed release is deployed using docs/server-deployment.md. Production receives only installation, backup, process and health verification, with no tests or inference. The Linux node retains its existing systemd service, native Codex CLI and state directory. The local node runs this very conversation, so its restart must wait for this turn to finish and all running/approval state to clear; a bounded local update helper performs the existing authenticated local stop/start lifecycle and records its result outside Git.
