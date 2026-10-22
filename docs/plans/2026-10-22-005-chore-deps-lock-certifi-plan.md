---
title: "chore(deps): relever certifi dans uv.lock (GHSA-6q48-6c7w-9gw2)"
type: chore
status: completed
date: 2026-10-22
---

# chore(deps): relever certifi dans uv.lock (GHSA-6q48-6c7w-9gw2)

## Pourquoi

L'alerte Dependabot n° 39 (GHSA-6q48-6c7w-9gw2, critique) vise `certifi`
`>= 2019.03.09, < 2026.10.19` dans `uv.lock`. Le lock sur `main` (9696b02)
épingle 2026.2.25. `certifi` entre dans l'export runtime
(`uv export --locked --no-dev`) par `httpcore` et `httpx`, dont dépend `mcp`,
lui-même tiré par `claude-agent-sdk`. Le déploiement de l'outil installe
désormais les versions du lock (mika-platform#212) : tant que le lock porte
2026.2.25, chaque déploiement installe le `certifi` vulnérable.

## Ce qui change

| Paquet | Avant | Après |
|---|---|---|
| `certifi` | 2026.2.25 | 2026.10.19 |

2026.10.19 est la première version corrigée selon l'alerte. Aucun autre paquet
ne bouge : le diff de `uv.lock` se limite au bloc `certifi` (version, sdist,
wheel).

## Comment

- `uv lock --upgrade-package certifi`, seul. Pas de `uv lock --upgrade` global.
- `pyproject.toml` ne change pas : `certifi` n'y figure pas, aucune contrainte
  ne bloque.

## Vérification

- `git diff uv.lock` : seul le bloc `certifi` change.
- `uv sync --all-extras --locked`, puis `certifi.__version__` == 2026.10.19.
- `uv run ruff check`, `uv run mypy src`, `uv run pytest` (suite complète).
- `uv export --locked --no-dev` porte `certifi==2026.10.19`.
- Pas de `uv tool install` depuis le worktree : ce geste remplacerait l'outil
  déployé, et il revient à MPC.

## Hors périmètre

- Redéployer l'outil après le merge : geste MPC (`make deploy`).
