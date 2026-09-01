# `.compound-engineering/`

Repo-local config for the [compound-engineering](https://github.com/EveryInc/compound-engineering-plugin)
plugin (the `/ce:*` skills). This directory holds **configuration only** — the
engineering artifacts themselves live under `docs/`.

## Files

- **`config.yaml`** — tracked team defaults. Everything ships commented out; a
  clone behaves exactly as the plugin defaults until a key is enabled. `docs_root`
  is intentionally left unset, so the artifact root stays the default `docs/`.
- **`config.example.yaml`** — reference copy of the full option set the current
  plugin understands. `/ce-setup` refreshes it; do not hand-edit.
- **`config.local.yaml`** — *(untracked, optional)* machine-local overrides.
  Gitignored via `.compound-engineering/*.local.yaml`. `docs_root` in this layer
  is ignored on purpose.

## Where the artifacts live

The plugin reads and writes under the `docs_root` (default `docs/`):

- `docs/plans/` — numbered-dated implementation plans (`/ce:plan`).
- `docs/solutions/` — captured, durable learnings (`/ce:compound`, `/ce-compound-refresh`).

Run `/ce-setup` to health-check this config and confirm the resolved artifact root.
