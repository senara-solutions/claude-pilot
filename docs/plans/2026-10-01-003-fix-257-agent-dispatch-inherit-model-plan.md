---
ticket: cpp#257 (mika#2606)
kind: fix
class: Agent-dispatch model rewrite (admission/létalité Bash inchangées)
status: landed — gate-rework ; placement ATTEINT = hook PreToolUse SDK (Agent|Task) ; branche can_use_tool RETIRÉE (condition MPC : un Allow pour un Agent réécrit court-circuite tier1+policy = élargissement latent) ; preuve d'atteinte par le dispatch de hook du SDK
---

# L'Agent dispatch hérite du modèle de session quand la fenêtre du modèle demandé est plus petite — Plan

Reprend `docs/solutions/tooling-decisions/a-context-inheriting-dispatch-must-not-force-a-smaller-window-model.md`. Backstop harness de cpp#257 ; le fix racine vit dans le plugin CE (hors-dépôt). Cause racine CONFIRMÉE par la sonde (relayée + dans l'issue).

## Gate KO → rework (le point central)

Le premier jet câblait la réécriture dans `can_use_tool`. **Placement INERTE** (mesuré MPC) : un dispatch `Agent` du pilote principal est autorisé **en amont** de `can_use_tool` (liste d'outils autorisés côté SDK / settings), donc le callback ne le voit jamais — **0/26 dispatchs `Agent` atteints sur 4 pilotes** (73e6f3ee, 189c0147, e9cb7b6b, 9aba7764 ; `[tool:request]` ne montrait que Bash/Edit/Write). Même classe que `harness-runtime-tools-bypass-can-use-tool.md`. Le test e2e dé-skippé appelait le handler **directement** → vert, mais atteste la **fonction**, pas le **chemin**.

**Le placement atteint = un hook `PreToolUse` du SDK** sur `Agent|Task`. Le CLI déclenche `PreToolUse` pour chaque appel d'outil, indépendamment de la décision de permission ; un hook qui renvoie `updatedInput` réécrit l'entrée transmise. Le hook ne renvoie **aucun `permissionDecision`** → l'admission est inchangée (axe tier1/policy/egress/létalité intact).

## Le fix

### `permissions.py`
- **`_SUBAGENT_DISPATCH_TOOLS`** = `{"Agent", "Task"}`.
- **`_model_context_window(model)`** : fenêtre (ordre relatif, pas un compte exact). `[1m]` → 1M (vérifié en premier, n'importe quelle base) ; **`opus` → 1M avec OU sans `[1m]`** (opus = le tier large / modèle de session ; sa fenêtre native est 1M, et une session `claude-opus-5` nue dispatchant un `sonnet` forcé est exactement le feu cpp#257 — opus ne doit pas être mal-classé au petit tier, sinon le `sonnet` paraît "pas plus petit" et rien n'est réécrit) ; `sonnet`/`haiku` **sans** `[1m]` → 200k ; sinon `None` (jamais deviné).
- **`_maybe_inherit_session_model(tool_input, config)`** : renvoie un `tool_input` neuf **sans** `model` quand la réécriture s'impose, sinon `None` (inchangée par le rework).
- **`create_subagent_model_inherit_hook(config, task_id)`** : fabrique le hook `PreToolUse` (signature SDK `HookCallback`). Sur réécriture → `audit.emit("review_degraded", {reason:"agent_dispatch_model_inherit", placement:"pre_tool_use_hook", ...})` + `{"hookSpecificOutput": {"hookEventName":"PreToolUse", "updatedInput": <réécrit>}}`. Sinon → `{}`. **Jamais de `permissionDecision`.**
- **Branche `can_use_tool`** : **RETIRÉE** (condition bloquante du gate MPC). Un `Allow` pour un `Agent` réécrit court-circuite tier1 + policy (qui refusent `Agent` par défaut) = élargissement latent, pas un no-op inoffensif. Le hook `PreToolUse` (updatedInput seul, jamais de `permissionDecision`) est la seule place, admission-neutre.

### `agent.py` / `shell.py`
- `ClaudeAgentOptions(..., hooks={"PreToolUse": [HookMatcher(matcher="Agent|Task", hooks=[create_subagent_model_inherit_hook(config=pilot_config, task_id=task_id)])]})`.
- **Modèle de session** : vit sur le `PilotConfig`, **pas** sur `guardrails.config` (un `ResolvedGuardrailConfig` de timings). Threadé depuis `cli.py._run` → `run_agent(pilot_config=config)` → `_run_agent_inner` → la fabrique du hook. (shell.py reçoit déjà un `PilotConfig`.)

## Preuve d'atteinte (AC7, exigence MPC 2)

`test_cpp257_pre_tool_use_hook_reached_via_sdk_control_request_rewrites_sonnet` pilote l'entrypoint **du SDK lui-même** : `claude_agent_sdk._internal.query.Query._handle_control_request` avec un `SDKControlRequest` de sous-type `hook_callback` — la méthode exacte que le canal de contrôle du CLI invoque quand un hook `PreToolUse` se déclenche sur un vrai dispatch `Agent`. Le hook est enregistré via le `_hooks_to_internal_format` du SDK + le câblage de `callback_id` de `Query.initialize()`, puis invoqué par le SDK via `self.hook_callbacks[callback_id](...)`. **Pas** un appel direct à la fonction. Asserte : (a) `updatedInput` sans `model`, (b) **aucun** `permissionDecision`, (c) marqueur `review_degraded` émis. Un second test (`..._leaves_larger_or_equal_override_untouched`) prouve le no-op sur un override égal/plus grand (sortie `{}`, pas d'audit).

## État de livraison

- **LANDÉ** : helpers + hook `PreToolUse` + câblage `agent.py`/`shell.py` + threading `PilotConfig` + 12 tests cpp#257 (dont les 2 de preuve d'atteinte SDK). Suite verte (1615 passed, 0 skipped), ruff/mypy propres, verify-pipeline OK.
- **Classifier auto-mode** : aucun edit bloqué dans ce rework (le hook est additif, non-admission).

## Fail-safe

Fail-closed sauf le cas connu : override non classable → jamais touché ; grand override (`[1m]`/opus) → jamais touché ; session connue → réécrit uniquement si **strictement** plus petit ; session inconnue (prod : `config.model` None) → ne retire qu'un connu-petit (sonnet/haiku, 200k) — **c'est le mécanisme vivant primaire aujourd'hui**. La réécriture ne bascule QUE override→hérite. Aucun autre outil touché.

## Hors portée

- **Fix racine (plugin CE, dépôt séparé)** : la skill ne doit pas coder en dur `model: "sonnet"` pour un dispatch héritant du contexte. Suivi séparé.
- Admission/létalité Bash (`is_tier3_dangerous`, `is_tier1_auto_approve`, YAML) : intouchées. Axe egress : intouché. `tier1.py` : 0 ligne modifiée.

## Acceptance criteria

- [x] **AC1 — sonnet/haiku → réécrit.** `_maybe_inherit_session_model` sur session `claude-opus-5[1m]` + `model: sonnet`/`haiku` retire `model` et préserve le reste sans muter l'original (`test_cpp257_sonnet_override_is_rewritten_to_inherit`, `..._haiku_...`).
- [x] **AC2 — sans modèle / modèle de session → inchangé.** (`..._no_model_override_...`, `..._session_model_override_...`).
- [x] **AC3 — fenêtre >= session → inchangé.** Session sonnet (200k), override `opus-5[1m]` (1M) ou `haiku` (200k) → `None` (`..._larger_or_equal_window_override_...`).
- [x] **AC4 — >200k tient après réécriture.** Base 430k > 200k forcé (échoue), < 1M session après retrait (tient) (`..._over_200k_base_fits_after_rewrite_to_inherit`).
- [x] **AC5 — fail-safe session inconnue.** `config` None / `model` None / inconnu + `sonnet` → réécrit ; `opus-5[1m]`/inconnu → inchangé (`..._fail_safe_drops_known_small_...`, `..._fail_safe_leaves_unknown_and_large_...`).
- [x] **AC6 — table de fenêtres.** `[1m]`→1M, `sonnet[1m]`→1M, **opus (avec/sans `[1m]`)→1M**, sonnet/haiku→200k, inconnu/vide/None→None (`..._model_window_classification`).
- [x] **AC7 — placement ATTEINT (hook PreToolUse), prouvé de bout en bout par le SDK.** Le hook `Agent|Task` est câblé dans `ClaudeAgentOptions.hooks` (`agent.py`/`shell.py`) et sa réécriture est prouvée en pilotant `Query._handle_control_request` (sous-type `hook_callback`) — PAS un appel direct : `updatedInput` sans `model`, aucun `permissionDecision`, marqueur `review_degraded` (`test_cpp257_pre_tool_use_hook_reached_via_sdk_control_request_rewrites_sonnet`, `..._leaves_larger_or_equal_override_untouched`). La branche `can_use_tool` est RETIRÉE (condition MPC) ; plus de test de backstop direct.
- [x] **AC8 — non-régression.** Admission/létalité Bash inchangées ; `tier1.py` 0 ligne ; suite complète verte (1615 passed, 0 skipped).

## Fire-Disposition

- **Feu** : classe de dispatch `Agent` échouant `Prompt is too long` par override à fenêtre plus petite que la session → porte qualité en repli silencieux. **Traité** : le hook `PreToolUse` (chemin réellement traversé) hérite le modèle de session si l'override est strictement plus petit + marqueur `review_degraded` + preuve d'atteinte SDK. La branche `can_use_tool` est retirée (condition MPC : élargissement latent).
- **Vérif de sortie** : `uv run pytest` → 1615 passed, 0 skipped ; `uv run ruff check .` → clean ; `uv run mypy src` → clean ; `./scripts/verify-pipeline.sh main` → passed.
- **Résidu** : (1) **Fix racine plugin CE** — dépôt séparé, hors portée. (2) Caveat : si un futur SDK cesse de déclencher `PreToolUse` pour `Agent`, le fix plugin CE reste la seule défense (pas de backstop `can_use_tool` : il élargirait l'admission).

## Références

- Solution : `docs/solutions/tooling-decisions/a-context-inheriting-dispatch-must-not-force-a-smaller-window-model.md`.
- Caveat de portée (point CENTRAL) : `docs/solutions/tooling-decisions/harness-runtime-tools-bypass-can-use-tool.md`.
- Preuve : issue cpp#257 ; dispatch `e9cb7b6b` (mika#2606) ; mesure MPC 0/26 (73e6f3ee, 189c0147, e9cb7b6b, 9aba7764) ; sonde relayée (contexte parent 430–490k échoue, 737k/723k passent).
