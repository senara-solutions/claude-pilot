---
title: "feat(permissions): permission nommée d'extraction de plage vers skills/bundled/_shared/tests/ (cpp#325)"
type: feat
status: active
date: 2026-11-27
issue: senara-solutions/claude-pilot#325
---

# Permission nommée d'extraction de plage (cpp#325)

## Pourquoi

mika#3178 scinde l'agrégat `skills/bundled/_shared/test-dispatch-lib.sh`
(19 070 lignes) en suites autonomes sous `skills/bundled/_shared/tests/`. Les
blocs doivent se déplacer mot pour mot (18 965 lignes). La décision D1(a) du
corps de mika#3178 (2026-11-27) demande une permission nommée et bornée dans
la politique de claude-pilot, pour que le mot pour mot soit prouvé par
construction.

Mesuré sur `main` (9c4a282) : `sed -n '10,20p' skills/bundled/_shared/test-dispatch-lib.sh > skills/bundled/_shared/tests/test_dispatch_lib_x.sh`
rend `deny` (« no matching policy rule »), `_bash_allow_is_chain_safe` rend
`False`, et `_segment_write_kind` classe déjà le segment `bash-redirect`.

## Ce qui change

1. `src/claude_pilot/policies/permissions.yaml` : règle
   `bash-sed-range-extract-3178`, placée juste après `bash-git-show-redirect`.
   Motif ancré `^…$`, séparateurs en espaces littéraux seulement (pas de `\s`,
   qui admettrait un saut de ligne), script sed exactement `'<N>,<M>p'`, option
   `-n` seule, source littérale de l'agrégat, cible
   `skills/bundled/_shared/tests/test_dispatch_lib_[a-z0-9_]+\.sh`, `>` ou `>>`.
2. `src/claude_pilot/permissions.py` : `_bash_allow_is_chain_safe` honore ce
   `rule_id` juste après `bash-git-show-redirect`, donc après les vetos de
   substitution, heredoc, here-string et `&`. Couplage par `rule_id`, échec
   fermé.
3. Destination : `_segment_write_kind` classe déjà ce segment comme écriture
   (`bash-redirect`, via `_segment_redirect_targets`), et
   `_destination_veto_reason` résout alors la cible (`is_within_project`,
   sensible aux liens symboliques) et la liste du plan de contrôle au point
   d'admission de `create_permission_handler`. Aucun nouveau write-kind n'est
   nécessaire ; un test épingle cette classification pour qu'elle ne puisse pas
   disparaître en silence.

4. Amendement du 2026-11-27 (exception au plan de contrôle, ratifiée par
   l'opérateur) : `skills/bundled/` est sur la liste cpp#42, donc la
   revalidation refusait toute cible admise, en terminal. `permissions.py`
   ouvre une exception unique, à côté de `_CONTROL_PLANE_PATTERNS` :
   `_CPP325_RANGE_EXTRACT_TARGET_RE` (ancré aux deux bouts, appliqué au chemin
   RÉSOLU relatif au worktree), et `_is_cpp325_control_plane_exception`, qui
   exige en plus le write-kind `bash-redirect`, le `rule_id`
   `bash-sed-range-extract-3178` passé par le seul appelant d'admission
   (`create_permission_handler`), et la forme exacte du segment
   (`_CPP325_RANGE_EXTRACT_SEGMENT_RE`, égale au motif YAML, égalité épinglée
   par un test). Tout autre appelant passe `rule_id=None` : l'exception ne
   s'applique pas. Renommer ou retirer la règle la fait tomber (échec fermé).

## Critères (cpp#325)

- AC1 : les deux formes admises, `rule_id` neuf, chaîne sûre, et admises par le
  handler de production.
- AC2 : contrôles négatifs dans le même test (liste du ticket).
- AC3 : test commité seul d'abord, rouge sur `main`, rouge cité dans la PR.
- AC4 : `tests/` symlinké hors du worktree : le motif admet, la chaîne est
  sûre, et le handler refuse par `destination-veto` avec le message de
  `_destination_veto_reason`.
- AC5 : rejeu hors ligne du corpus de transcripts, ancien contre neuf.
- AC6 : `reason` et commentaire nomment mika#3178 seul consommateur et la
  condition de retrait ; ticket dormant ouvert en même temps.
- AC7 : le positif admis de bout en bout par le handler de production.
- AC8 : refusés par le plan de contrôle : une autre forme de redirection vers
  une cible conforme ; une redirection vers `skills/bundled/_shared/dispatch-lib.sh` ;
  la forme admise sous un autre `rule_id`.
- AC9 : commentaire voisin de l'exception nommant cpp#42, cpp#325, mika#3178
  et la condition de retrait.
- AC10 : l'exception vit dans `permissions.py` de claude-pilot, au lieu de la
  liste cpp#42 ; aucun fichier éditable par la boucle mika ne la porte.

## Hors champ

`sed -i` reste refusé, y compris sur l'agrégat. Retirer les blocs de l'agrégat
revient au plan de mika#3178.
