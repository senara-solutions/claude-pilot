---
title: "fix(agent): une session que l'API a tuée (is_error, 401 ou 502) ne sort plus en status: success (#319)"
type: fix
status: done
date: 2026-11-07
origin: senara-solutions/claude-pilot#319
---

# Mort par l'API : ne plus sortir en succès (#319)

## Contexte

Le SDK rend un appel d'API en échec comme un `ResultMessage` avec
`subtype: "success"`, `is_error: true`, `api_error_status` (le code HTTP) et
`terminal_reason: "api_error"`. `agent.py` calculait le statut sur le seul
`subtype` et ne lisait jamais `is_error`. Une session tuée par l'API sortait donc
en `status: success`, avec `[done] Success`, et passait par `post_handoff`. Le
ticket relève 7 cas depuis le 21/10, dont 3 enregistrés comme réussis.

La fixture de cpp#54 construisait ce cas avec `subtype="error"`, qui n'est pas
la forme du SDK. C'est pourquoi le défaut n'avait pas été vu.

## Décisions

### D1. Un bras `api_error` avant cpp#144 et mika#940 (AC1, AC4)

Dans la branche `ResultMessage`, juste après le calcul du statut :
`status == "success"` et `is_error is True` donnent `status: "error"`,
`subtype: "api_error"` (constante `API_ERROR_SUBTYPE`), et un
`termination_reason` qui nomme `api_error_status` et `terminal_reason`. Le
texte `result` de l'API n'y est pas recopié.

Ce bras passe avant cpp#144 et mika#940, qui ne se déclenchent que sur
`status == "success"`. Une mort par l'API garde donc son `subtype` et ne devient
jamais `pipeline_incomplete`. La sortie existante fait le reste : code 1, pas de
`post_handoff`, `log_error` au lieu de `[done] Success`.

### D2. Les autres fins `is_error` ne changent pas (AC2)

`error_max_turns` et `error_during_execution` portent aussi `is_error: true`,
mais leur `subtype` n'est pas `success` : le bras ne les touche pas. Ils restent
`terminated` et `error`, et la reprise de cpp#151 n'est pas concernée.
`api_error_status` reste sur le `ResultJson` (cpp#54).

### D3. Tests à la forme réelle (AC3)

- La fixture `_result_with_api_error` prend la forme du SDK :
  `subtype="success"`, `is_error=True`, `terminal_reason="api_error"`. Le test
  cpp#54 (429 sur le `ResultJson`) passe toujours.
- 401 puis 502 : `status: error`, `subtype: api_error`, code 1, aucun
  `post_handoff`, pas de `[done]`. Dans le même test, le contrôle négatif
  (`is_error=False`) reste un succès avec un `post_handoff`.
- `error_max_turns` et `error_during_execution` gardent leur classement.
- Sous `CLAUDE_PILOT_REQUIRE_PR=1`, une session saine sans PR sort toujours en
  `pipeline_incomplete`, et une mort par l'API garde `api_error`.

Contre-épreuve : sans le changement de `src/`, les tests AC1 et AC4 échouent.

### D4. Sonde (AC5)

Hors code : sur les 14 jours qui suivent le deploy, sur le ticket.

## Observation hors claude-pilot

Les 3 dev-pilots enregistrés comme réussis n'ont lancé aucun `gh pr create`
(compte sur leurs transcripts), et ne sont pourtant pas sortis en
`pipeline_incomplete`. Cause probable, non prouvée sur ces vols : sous le bac à
sable, `dispatch-lib.sh` passe `--clearenv` et ne réinjecte que les variables de
`_PILOT_SANDBOX_ENV_ALLOWLIST`, où `CLAUDE_PILOT_REQUIRE_PR` ne figure pas.
`dispatch-lib.sh` est un fichier du plan de contrôle : ce vol ne le modifie pas,
et l'observation va en commentaire sur le ticket.

## Hors périmètre

- Le renouvellement du jeton OAuth et l'heure des morts.
- L'aval dans `dispatch-lib.sh`.
- Aucun ticket ouvert (gel jusqu'au 01/12).

## Acceptance criteria

- AC1 : `subtype: "success"` avec `is_error: true` sort en `status: error`,
  `subtype: api_error`, code 1, sans `post_handoff` ni `[done] Success`.
- AC2 : les autres fins `is_error` gardent leur classement ; `api_error_status`
  reste sur le `ResultJson`.
- AC3 : tests à la forme réelle, 401 et 502, avec un contrôle négatif.
- AC4 : le contrat de mika#940 ne change pas.
