---
ticket: cpp#267 (mika#1960)
kind: fix
class: Correction de comportement harness (NON admission) — le hook PreToolUse Agent|Task retire aussi run_in_background
status: landed (clone isolé, branche fix267-strip-run-in-background-cpp) — gate MPC suffit (pas de signature Vincent : non admission)
---

# Le hook PreToolUse Agent retire aussi `run_in_background` — un dispatch de fond détaché ne peut pas survivre à une session headless — Plan

Piste **(a)** ratifiée par Prime (2026-10-01 20:00). Étend le hook `create_subagent_model_inherit_hook` de cpp#263 (déjà fusionné) ; aucun nouveau chemin, aucun nouveau câblage. Le fix racine (interdiction par prompt dans `ce-code-review`) ne tient pas — la correction appartient au substrat.

## Le feu (n=1 fatal, 2026-10-01, pilote 624656b1, mika#1960 phase 2)

Au tour 113, le pilote lance les 8 relecteurs de `/ce:code-review` **tous avec `run_in_background: true`**, puis **rend la main**. En headless rien ne réveille le pilote : le SDK émet un `ResultMessage`, claude-pilot clôt la session (`[done] Success | 113 turns | $90.40`), et les relecteurs de fond **meurent avec elle** — revue perdue, aucun compound, aucune PR, moteur `PIPELINE_INCOMPLETE`. En interactif, la notification de fin des agents **réveille** le tour ; en headless, « dispatch-puis-rends-la-main » est létal. La classe est latente : **5 pilotes sur 9** (sur les 40 derniers) dispatchent en arrière-plan ; seul celui qui a *attendu* en rendant la main est mort.

## La correction — piste (a)

Le hook `PreToolUse` sur `Agent|Task` de cpp#263 réécrit déjà `updatedInput` **sans aucun `permissionDecision`** (admission-neutre). Il retire désormais **aussi** `run_in_background` quand il est présent-et-truthy : en headless, tout dispatch devient bloquant, donc se termine dans le tour et ne peut pas être orphelin à la clôture. Un message d'assistant portant plusieurs `Agent` les exécute **toujours en parallèle** (le strip ne retire que le mode détaché/sondé, pas la concurrence intra-message) : la revue reste parallèle-dans-un-message ; seule la mort « dispatch-puis-rends-la-main » est fermée.

### `permissions.py`
- **`_maybe_strip_run_in_background(tool_input)`** (nouveau, à côté de `_maybe_inherit_session_model`) : renvoie un `tool_input` neuf **sans** `run_in_background` quand il est présent-et-truthy ; sinon `None` (absent ou falsy → déjà bloquant). Ne mute pas l'original.
- **`create_subagent_model_inherit_hook`** (étendu, même hook) : compose les deux réécritures dans **un seul** `updatedInput`.
  1. cpp#263 : hérite le modèle de session si fenêtre strictement plus petite → `audit.emit("review_degraded", {reason:"agent_dispatch_model_inherit", placement:"pre_tool_use_hook", ...})`.
  2. cpp#267 : retire `run_in_background` truthy → `audit.emit("review_degraded", {reason:"agent_dispatch_run_in_background_stripped", placement:"pre_tool_use_hook", ...})`.
  - Chaque réécriture émet **son propre** événement d'audit ; les deux peuvent co-occurrer sur un même dispatch, chacune auditable (contrôle Prime (i)). Si aucune ne s'applique → `{}`. **Jamais de `permissionDecision`** (contrôle Prime invariant : non admission).

Aucun changement à `agent.py`/`shell.py` : le hook est le même, déjà câblé dans `ClaudeAgentOptions.hooks`.

## Contrôles orthogonaux de Prime (les deux requis)

### (i) Audit sur CHAQUE réécriture
`review_degraded` étendu d'un `reason` dédié `agent_dispatch_run_in_background_stripped` (placement `pre_tool_use_hook`). Mécaniquement lisible. Un model-inherit ET un strip run_in_background peuvent co-occurrer sur un dispatch : **deux** événements, chacun auditable.

### (ii) Test de l'INVARIANT, orthogonal à la réécriture (rewrite ⊥ effect-verification)
- **Test de réécriture** (`test_cpp267_run_in_background_is_stripped_when_truthy` + `..._absent_or_falsy_passes_through_unchanged`) : prouve que la LOGIQUE de réécriture (`_maybe_strip_run_in_background`) retire un `run_in_background` truthy et laisse absent/falsy intact.
- **Test d'invariant** (`test_cpp267_invariant_no_background_dispatch_forwarded_via_sdk_control_request`) : prouve l'invariant qui gouverne la létalité — *en headless, aucune tâche de fond détachée ne survit à la session* — asserté au niveau qui le gouverne : **après le hook atteint, aucun dispatch `Agent` transmis par le pilote ne porte `run_in_background: true`** (donc aucune tâche détachée à orpheliner à la clôture). **Atteinte prouvée par le chemin de hook du SDK** : `Query._handle_control_request` avec un `SDKControlRequest` de sous-type `hook_callback` (comme le test d'atteinte cpp#263) — **PAS** un appel direct à la fonction. Modèle de session laissé inconnu pour isoler le strip du model-inherit.
- **Test combiné** (`test_cpp267_model_inherit_and_background_strip_combine_on_one_dispatch`) : forme exacte 624656b1 (`model: sonnet` + `run_in_background: true` sur session 1M), piloté par le SDK : l'unique `updatedInput` a `model` ET `run_in_background` retirés, **les deux** événements d'audit émis, **aucun** `permissionDecision` (cpp#263 model-inherit intact).

## Invariants (non-régression)

- **NON admission** : octroi Bash byte-identique ; `tier1.py` 0 ligne modifiée ; `is_tier1_auto_approve` / `is_tier3_dangerous` / `TIER3_PATTERNS` / egress / `_denial_is_terminal` intouchés. Le hook n'ajoute **aucun** `permissionDecision`.
- **cpp#263 intact** : le model-inherit fonctionne toujours seul et en combinaison (test combiné). Les deux réécritures composent dans un seul `updatedInput`.

## État de livraison

- **LANDÉ** (clone isolé) : `_maybe_strip_run_in_background` + hook étendu + 4 tests cpp#267. Suite verte (1652 passed, 0 skipped), ruff/mypy propres, verify-pipeline OK.
- Aucun edit bloqué (le strip est additif, non-admission : classifier hors du diff).

## Acceptance criteria

- [x] **AC1 — plus de dispatch en arrière-plan (piste a).** Après le hook, un dispatch `Agent` portant `run_in_background: true` est transmis **sans** le flag (`test_cpp267_invariant_...`, prouvé via `Query._handle_control_request`). La forme 624656b1 n'arrive plus en arrière-plan.
- [x] **AC2 — audit sur chaque réécriture.** `review_degraded` reason `agent_dispatch_run_in_background_stripped`, émis sur le chemin de hook (asserté dans le test d'invariant et le test combiné). Le model-inherit garde son propre `agent_dispatch_model_inherit` ; les deux co-occurrent et sont auditables séparément.
- [ ] **AC3 — gate MPC avant merge** (selon la recette de rejeu hors ligne). Hors scope de ce clone (pas de push/PR) ; remis à MPC.

## Fire-Disposition

- **Feu** : en headless, un dispatch `Agent` détaché (`run_in_background: true`) suivi d'un abandon de tour clôt la session et tue les agents de fond → revue perdue, `PIPELINE_INCOMPLETE`. **Traité** : le hook `PreToolUse` atteint (chemin réellement traversé) retire `run_in_background` truthy → tout dispatch devient bloquant et ne peut pas survivre à la clôture ; audit dédié ; preuve d'atteinte SDK + invariant orthogonal à la réécriture.
- **Vérif de sortie** : `uv run pytest` → 1652 passed, 0 skipped ; `uv run ruff check .` → clean ; `uv run mypy src` → clean ; `./scripts/verify-pipeline.sh main` → passed.
- **Résidu** : (1) parallélisme multi-message perdu (compromis assumé de la piste (a) : la revue devient séquentielle *par appel* ; intra-message reste parallèle). (2) Fix racine plugin CE (interdiction par prompt) — ne tient pas seul, reste du ressort du plugin. (3) Piste (b) (`_merge_stream` attend les tâches de fond en vol) non retenue : plus fidèle mais plus risquée (borner l'attente, définir ce qui la compte).

## Références

- Solution : `docs/solutions/tooling-decisions/a-detached-background-dispatch-cannot-outlive-a-headless-session.md`.
- Hook parent : `docs/solutions/tooling-decisions/a-context-inheriting-dispatch-must-not-force-a-smaller-window-model.md` (cpp#257/263) ; `harness-runtime-tools-bypass-can-use-tool.md` (borne : pourquoi le hook PreToolUse, pas `can_use_tool`).
- Preuve : issue cpp#267 ; pilote 624656b1 (mika#1960 phase 2) ; 5/9 pilotes dispatchent en arrière-plan.
