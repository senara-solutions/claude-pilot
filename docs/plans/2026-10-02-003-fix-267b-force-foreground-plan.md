---
ticket: cpp#267 REOPENED (mika#2627, mika#1960)
kind: fix
class: Correction de comportement harness (NON admission) — le hook PreToolUse Agent|Task FORCE run_in_background=False (ne le retire pas)
status: landed (clone isolé, branche fix267b-force-foreground-cpp) — gate MPC + preuve servie requis (non admission)
supersedes: docs/plans/2026-10-01-006-fix-267-strip-run-in-background-lethality-plan.md (cpp#269, inoperant)
---

# Le hook PreToolUse Agent FORCE `run_in_background=False` (ne le STRIP pas) — un dispatch de fond détaché ne peut pas survivre à une session headless — Plan (fix267b)

cpp#269 (fusionné) a mis le bon *placement* mais la mauvaise *réécriture* : il **retirait** la clé. Ce plan corrige la réécriture en place sur le même hook `create_subagent_model_inherit_hook` (cpp#263). Aucun nouveau chemin, aucun nouveau câblage.

## Cause racine — pourquoi cpp#269 était INOPERANT

Dans le CLI embarqué, **« Agents run in the background BY DEFAULT »** et la porte de premier plan est `run_in_background !== false`. cpp#269 **retirait** la clé (`_maybe_strip_run_in_background` faisait `pop`). Or retirer la clé = demander le défaut = **toujours en arrière-plan**. Un 2e pilote (`bb9163e1`, mika#2627, 140 tours, $59.72) est mort de la **même** mort APRÈS le déploiement de #269 : sa stderr porte 3 `agent_dispatch_run_in_background_stripped` (le hook a bien tiré), pourtant chaque appel `Agent` est revenu en **11–26 ms** et la session s'est terminée sur « waiting on them ». Les dispatches sans clé étaient en arrière-plan aussi. **`strip ≠ foreground`** : la seule valeur que le CLI lit comme premier plan est la clé **présente et littéralement `False`**.

## Le feu (origine, n=1 fatal, 2026-10-01, pilote 624656b1)

Au tour 113, le pilote lance 8 relecteurs **tous avec `run_in_background: true`**, puis **rend la main**. En headless rien ne le réveille : `ResultMessage` → clôture → relecteurs morts → revue perdue, `PIPELINE_INCOMPLETE`. Classe latente : **5 pilotes sur 9** dispatchent en arrière-plan. Récidive post-#269 : `bb9163e1`.

## La correction (prescrite MPC) — FORCE, pas STRIP

### `permissions.py`
- **`_force_foreground_dispatch(tool_input)`** (renommé depuis `_maybe_strip_run_in_background`) : renvoie un `tool_input` neuf avec `run_in_background = False` **explicite et présent** dès que la valeur courante est autre que `False` littéral — **truthy OU absente** (absente = défaut de fond du CLI). No-op (`None`) **uniquement** si déjà `False` littéral. Ne mute pas l'original. Garde: `if tool_input.get("run_in_background") is False: return None`.
- **`create_subagent_model_inherit_hook`** (étendu, même hook) : compose les deux réécritures dans **un seul** `updatedInput`.
  1. cpp#263 : hérite le modèle de session si fenêtre strictement plus petite → `review_degraded` reason `agent_dispatch_model_inherit`.
  2. cpp#267/fix267b : force `run_in_background = False` → `review_degraded` reason **`agent_dispatch_forced_foreground`** (renommé). `updatedInput` **uniquement**, **jamais** de `permissionDecision`. Déclenchement sur truthy OU absent ; no-op si déjà `False`.

Aucun changement à `agent.py`/`shell.py` : hook identique, déjà câblé.

## Contrôles orthogonaux (rewrite ⊥ effect-verification)

### (i) Audit sur CHAQUE réécriture
`review_degraded` reason dédié `agent_dispatch_forced_foreground` (placement `pre_tool_use_hook`). Model-inherit garde `agent_dispatch_model_inherit` ; les deux co-occurrent, chacun auditable.

### (ii) Tests
- **Réécriture** (`test_cpp267_run_in_background_is_forced_false_when_truthy`, `test_cpp267_absent_run_in_background_is_forced_false`, `test_cpp267_already_false_is_noop`) : la logique met `run_in_background` à `False` **présent** (pas retiré) pour truthy ET pour absent ; no-op si déjà `False` ; sans muter l'entrée.
- **AC3 RED/GREEN** (`test_cpp267_invariant_red_on_strip_green_on_force_false`) : l'invariant Prime *aucune tâche de fond ne survit à la fin de session en headless*, encodé par un modèle de la porte CLI (`run_in_background !== false` via `_dispatch_is_background`). **ROUGE sur le comportement #269** (strip → clé absente → défaut de fond → invariant violé) ; **VERT après fix267b** (clé présente et `False`). Simulation du ROUGE : on reproduit le `pop` de #269 sur la forme fondatrice et on montre `_dispatch_is_background(stripped) is True`.
- **Invariant SDK** (`test_cpp267_invariant_no_background_dispatch_forwarded_via_sdk_control_request`) : assertion sur **sémantique CLI** — l'`updatedInput` transmis porte `run_in_background` **présent et `=== False`** (la seule valeur que la porte accepte comme premier plan ; contrat comportemental : le résultat de l'outil Agent ne revient qu'à la FIN du sous-agent). Atteinte prouvée via `Query._handle_control_request` + `hook_callback` (pas d'appel direct). Modèle de session inconnu pour isoler.
- **Combiné** (`test_cpp267_model_inherit_and_background_strip_combine_on_one_dispatch`) : forme 624656b1/bb9163e1 (`model: sonnet` + `run_in_background: true` sur 1M), piloté SDK : `model` **retiré** ET `run_in_background` **forcé présent-et-`False`**, les deux audits, **aucun** `permissionDecision`.

## Invariants (non-régression)

- **NON admission** : octroi Bash byte-identique ; `tier1.py` **0 ligne modifiée** ; `is_tier1_auto_approve` / `is_tier3_dangerous` / `TIER3_PATTERNS` / egress / `_denial_is_terminal` intouchés ; **aucun** `permissionDecision`.
- **cpp#263 intact** : model-inherit seul et en combinaison (test combiné). Les deux réécritures composent dans un seul `updatedInput`.

## État de livraison

- **LANDÉ** (clone isolé) : `_force_foreground_dispatch` (force, pas strip ; absent → `False` aussi) + hook/audit renommés `forced_foreground` + tests cpp#267 réécrits (dont AC3 RED/GREEN). Suite verte, ruff/mypy propres, verify-pipeline OK (queues en rapport de remise).
- Aucun edit bloqué (non-admission ; classifier hors diff).

## Acceptance criteria

- [x] **AC1 — force `False` explicite.** Le hook met `run_in_background=False` sur un dispatch truthy OU absent ; déjà-`False` → inchangé (`_force_foreground_dispatch` + tests de réécriture).
- [x] **AC2 — test sur sémantique CLI, pas présence de clé.** L'`updatedInput` réécrit a `run_in_background` **présent et `=== False`** (`test_cpp267_invariant_..._via_sdk_control_request`, assertion `updated["run_in_background"] is False`). Cadré comme : avec cette entrée, le résultat de l'outil Agent ne revient qu'à la fin du sous-agent ; preuve au niveau entrée = `run_in_background is False`.
- [x] **AC3 — invariant Prime ROUGE-avant/VERT-après.** `test_cpp267_invariant_red_on_strip_green_on_force_false` : ROUGE sur le strip de #269 (clé absente == fond == invariant violé), VERT sur force-`False`. RED-before noté dans le corps de PR.
- [ ] **AC4 — preuve SERVIE après déploiement** (résultats de l'outil Agent en secondes/minutes, pas en ms) : lue par MPC sur le premier pilote. **Hors de ce clone** (pas de déploiement ici) ; noté pour MPC.
- [x] **Atteinte** : chemin de hook SDK conservé (`Query._handle_control_request` + `hook_callback`, pas d'appel direct) ; l'entrée transmise porte `run_in_background is False`.

## Fire-Disposition

- **Feu** : cpp#269 (strip) **inoperant** — retirer la clé demande le défaut de fond du CLI (`!== false`), donc le dispatch reste en arrière-plan et meurt à la clôture headless → revue perdue, `PIPELINE_INCOMPLETE` (récidive `bb9163e1`). **Traité** : le hook atteint **force `run_in_background=False` présent** (truthy OU absent) → tout dispatch au premier plan, bloquant dans le tour ; audit dédié `agent_dispatch_forced_foreground` ; preuve d'atteinte SDK + invariant AC3 ROUGE/VERT orthogonal à la réécriture. **Cause racine enregistrée : `strip ≠ foreground` car le défaut CLI est l'arrière-plan.**
- **Vérif de sortie** : voir rapport de remise (pytest / ruff / mypy / verify-pipeline). AC4 (preuve servie) remis à MPC post-déploiement.
- **Résidu** : (1) parallélisme multi-message perdu (compromis piste (a)). (2) Fix racine plugin CE (interdiction par prompt) — ne tient pas seul. (3) Piste (b) (`_merge_stream`) non retenue : plus fidèle mais plus risquée.

## Références

- Solution : `docs/solutions/tooling-decisions/a-detached-background-dispatch-cannot-outlive-a-headless-session.md` (mise à jour : force `run_in_background=False`, ne pas strip — retirer la clé demande le défaut de fond).
- Plan superseded : `docs/plans/2026-10-01-006-fix-267-strip-run-in-background-lethality-plan.md` (cpp#269, strip, inoperant).
- Hook parent : `a-context-inheriting-dispatch-must-not-force-a-smaller-window-model.md` (cpp#257/263) ; `harness-runtime-tools-bypass-can-use-tool.md` (borne).
- Preuve : issue cpp#267 (rouverte) ; pilote 624656b1 (origine) ; `bb9163e1` (mika#2627, récidive post-#269).
