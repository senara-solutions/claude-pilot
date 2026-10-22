---
title: "chore(deps): monter pyjwt, mcp et anyio dans uv.lock"
type: chore
status: completed
date: 2026-10-22
---

# chore(deps): monter pyjwt, mcp et anyio dans uv.lock

## Pourquoi

Les alertes Dependabot ouvertes sur `uv.lock` portent sur trois paquets seulement :
`pyjwt`, `mcp` et `anyio`. Les versions verrouillées sur `main` (711b5dc) sont
sous la première version corrigée de chaque alerte. Les PR Dependabot ouvertes
(#181, #182, #191, #192, #193) ne règlent pas ce lock : elles relèvent des
planchers dans `pyproject.toml` ou montent d'autres paquets, et leur fusion
seule laisserait le lock tel qu'il est.

## Ce qui change

| Paquet | Avant | Après | Origine dans l'arbre |
|---|---|---|---|
| `pyjwt` | 2.13.0 | 2.15.0 | direct (`pyjwt>=2.13.0`) et `mcp[crypto]` |
| `mcp` | 1.27.0 | 1.30.0 | `claude-agent-sdk` |
| `anyio` | 4.13.0 | 4.14.2 | `claude-agent-sdk`, `mcp`, `httpx`, `starlette`, `sse-starlette` |

La cible de chaque paquet est la plus petite version qui couvre toutes ses
alertes ouvertes : 2.15.0 pour `pyjwt` (une alerte exige 2.15.0, les autres
2.14.0), 1.30.0 pour `mcp`, 4.14.2 pour `anyio`.

## Comment

- `uv lock --upgrade-package '<paquet>==<version>'`, un paquet à la fois. Pas de
  `uv lock --upgrade` global : il monterait des paquets sans lien avec les alertes.
- `pyproject.toml` ne change pas. Aucune de ses contraintes ne bloque ces
  versions : la résolution simulée (`uv lock --dry-run`) monte les trois paquets
  et rien d'autre.

## Vérification

- `uv sync --all-extras`
- `uv run ruff check`, `uv run mypy src`, `uv run pytest` (suite complète)
- `uv run claude-pilot --help`. Pas de `uv tool install` depuis le worktree : ce
  geste remplacerait l'outil déployé.
- Relire `uv.lock` : chaque paquet est à une version hors des plages signalées.

## Hors périmètre

- Relever les planchers de `pyproject.toml` (#181, #182, #192, #193) et monter
  `claude-agent-sdk` (#191) : ces PR restent ouvertes, à trancher séparément.
