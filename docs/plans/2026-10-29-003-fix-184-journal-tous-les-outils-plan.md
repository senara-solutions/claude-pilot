---
title: "fix(agent): chaque appel d'outil est journalisé (cpp#184, défaut 1)"
type: fix
status: active
date: 2026-10-29
issue: senara-solutions/claude-pilot#184
---

# Chaque appel d'outil est journalisé

## Pourquoi

Le journal pilote ne montre que les décisions du callback `can_use_tool`. Un outil admis en
amont (règles d'autorisation des settings, Read/Glob/Grep) ne laisse aucune ligne. Mesuré le
2026-10-29 sur le pilote 67156183 : 15 `tool_use` au transcript (12 Read, 3 Bash), une seule
ligne d'outil au journal (un `policy:deny`).

## Décisions

1. **Source** : le flux `AssistantMessage`, seul endroit qui voit tous les appels du modèle.
   Une ligne `[tool:use] Nom: résumé` par bloc `tool_use`, dédupliquée par id.
2. **Résumé** : `permissions._summarize_input` (troncature et masquage des secrets déjà en
   place), sauts de ligne repliés pour rester sur une ligne grep-able (cpp#262).
3. **Sous-agents** : journalisés, marqués `(subagent)`.
4. Observabilité seule : aucune décision d'admission ni de létalité ne change.

## Défaut 2 du ticket : non codé

Le détecteur « N tours sans écriture worktree => halte » n'est pas implémenté, mesures à l'appui :

- 3587fe25 (14/09) : un groom, qui n'écrit pas au worktree par nature ; tours espacés d'environ
  6 min par le TTFT (classe cpp#177) ; il progressait.
- 2a6c2112 (29/09) : des `Edit`, puis une boucle de sondage d'un build cargo en arrière-plan
  (`du -sm` / `tail -2` toutes les 2 s, 28 appels identiques d'affilée) jusqu'à `error_max_turns`.
  Ce motif est fermé à la source par cpp#267 (arrière-plan désactivé, 2026-10-02) : sur 363
  pilotes depuis le 2026-10-03, la plus longue série d'appels identiques est 3.
- Le prédicat « pas d'écriture worktree » est celui que mika#2277 a mesuré faux : il a tué deux
  pilotes sains en phase de lecture.

Avec ce correctif, une récidive deviendrait visible au journal.

## Tests

`tests/test_tool_use_log.py`, par `run_agent` sans aucun appel au callback : appel admis en
amont journalisé, commande multi-ligne sur une ligne, bloc vu deux fois journalisé une fois,
sous-agent marqué, secret masqué. Les cinq échouent sans le correctif.
