---
ticket: cpp#267 follow-up (mika#2630)
kind: chore
class: Correction de comportement harness (NON admission) — relève le plafond MAX du timeout Bash (`BASH_MAX_TIMEOUT_MS=1800000`, 30 min) dans la MÊME `ClaudeAgentOptions.env` que `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`
status: landed (clone isolé, branche fix-bash-max-timeout-cpp) — non admission
follows: docs/plans/2026-10-02-003-fix-267b-force-foreground-plan.md (cpp#267b, #275 fusionné — couche env)
---

# Relever `BASH_MAX_TIMEOUT_MS` à 30 min pour les builds longs en premier plan — Plan (suite cpp#267)

Petite suite de cpp#267/#275 (fusionnés). **Non admission.** Purement `agent.py` (env) + son test. Aucun changement de classifier ; `tier1.py` + `permissions.py` à **0 ligne modifiée**.

## Pourquoi

#275 a posé `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` dans `ClaudeAgentOptions.env` (constante `_CLI_FORCE_FOREGROUND_ENV`, câblée via `env=dict(_CLI_FORCE_FOREGROUND_ENV)`). Effet de bord **documenté et accepté** : la même variable retire `run_in_background` du schéma de l'outil **Bash**, donc une commande Bash longue (p.ex. `cargo test --workspace`) ne peut plus se détacher et s'exécute désormais **au premier plan**, soumise au **timeout** de l'outil Bash.

Le rejeu de transcripts (gate MPC) confirme que l'effet est réel : **11/60 sessions** lançaient un build long en arrière-plan. Sans relever le plafond du timeout Bash, un build long au premier plan peut dépasser le plafond MAX intégré du CLI (10 min) et être tué. MPC demande d'ajouter `BASH_MAX_TIMEOUT_MS=1800000` (30 min) à côté de la variable existante.

## STEP 0 — Vérifier le nom EXACT de la variable à la source (fait AVANT l'édition)

Le CLI embarqué est un binaire bun-compilé sous `.venv/.../claude_agent_sdk/_bundled/claude` (≈207 Mo). Grep du binaire épinglé — les **deux** symboles sont présents (4 occurrences chacun). La logique décompilée lève l'ambiguïté :

```js
var OOo=120000, DOo=600000;                         // défaut 2 min, plafond MAX intégré 10 min
function Owe(e=process.env){                          // lecteur du DÉFAUT
  let n=e.BASH_DEFAULT_TIMEOUT_MS;
  if(n){ let r=al(n); if(!isNaN(r)&&r>0) return r } return OOo }
function H5e(e=process.env){                          // lecteur du plafond MAX
  let n=e.BASH_MAX_TIMEOUT_MS;
  if(n){ let r=al(n); if(!isNaN(r)&&r>0) return Math.max(r, Owe(e)) } return DOo }
```

- `BASH_DEFAULT_TIMEOUT_MS` = **défaut** (repli `OOo`=120000 ms = 2 min).
- `BASH_MAX_TIMEOUT_MS` = **plafond MAX** (`Math.max(r, défaut)`, repli `DOo`=600000 ms = 10 min).

Nous voulons le **plafond MAX** pour qu'un build long (timeout explicite ou défaut) puisse tourner jusqu'à 30 min au premier plan → **`BASH_MAX_TIMEOUT_MS`** confirmé. Le défaut (`BASH_DEFAULT_TIMEOUT_MS`) est laissé intact pour que les commandes courtes gardent le défaut de 2 min.

## La correction

`agent.py` — ajout à la constante `_CLI_FORCE_FOREGROUND_ENV` (qui coule déjà dans `ClaudeAgentOptions.env` via `env=dict(...)`) :

```python
_CLI_FORCE_FOREGROUND_ENV: dict[str, str] = {
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
    "BASH_MAX_TIMEOUT_MS": "1800000",   # 30 min
}
```

- `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` **inchangé**.
- Bloc de commentaire de la constante mis à jour (pourquoi : l'effet de bord DISABLE_BACKGROUND_TASKS force les builds longs au premier plan ; plafond 30 min ; nom vérifié à la source, `BASH_MAX_TIMEOUT_MS` = plafond MAX ≠ `BASH_DEFAULT_TIMEOUT_MS` défaut).
- Même mécanisme de livraison env que #275 (`ClaudeAgentOptions.env`, fusionné dans l'env effectif du sous-processus CLI, copie via `dict(...)`).

## Test

Extension du test de présence-env de #275 (même mécanisme — `ClaudeAgentOptions` espion, pilotage de `run_agent`) :

- L'`env` capturé porte `BASH_MAX_TIMEOUT_MS == "1800000"` **ET** `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS == "1"`, et reste une **copie distincte** de la constante.
- Valeur assertée comme l'entier-de-30-min attendu (`int(env["BASH_MAX_TIMEOUT_MS"]) == 30 * 60 * 1000`).
- Le test de constante (`test_cpp267_force_foreground_env_constant_is_canonical_truthy`) est mis à jour pour la nouvelle paire exacte.

## Invariants (non-régression)

- **NON admission.** `tier1.py` + `permissions.py` à **0 ligne modifiée**. Aucun changement de classifier. Octroi Bash byte-identique ; aucun `permissionDecision`.
- Purement `agent.py` (env) + son test + docs.

## Acceptance criteria

- [x] **AC0 — nom vérifié à la source.** Grep du binaire CLI épinglé : `BASH_MAX_TIMEOUT_MS` (plafond MAX) et `BASH_DEFAULT_TIMEOUT_MS` (défaut) tous deux présents ; la logique décompilée confirme que `BASH_MAX_TIMEOUT_MS` est le plafond MAX. Pas de variable no-op.
- [x] **AC1 — variable ajoutée à la MÊME env.** `BASH_MAX_TIMEOUT_MS=1800000` dans `_CLI_FORCE_FOREGROUND_ENV`, qui coule dans `ClaudeAgentOptions.env`. `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` inchangé.
- [x] **AC2 — test de présence-env étendu.** Assertion sur le vrai mécanisme : les deux clés présentes dans l'env capturé, valeur = entier-30-min, copie distincte.
- [x] **AC3 — invariants.** `tier1.py` + `permissions.py` 0 ligne ; suite verte, ruff/mypy propres, verify-pipeline OK (queues en rapport de remise).

## Vérif de sortie

`uv run pytest` / `uv run ruff check .` / `uv run mypy src` / `./scripts/verify-pipeline.sh main` — tails en rapport de remise.

## Références

- Plan parent : `docs/plans/2026-10-02-003-fix-267b-force-foreground-plan.md` (cpp#267b, #275).
- Solution (section Side-effects mise à jour avec le plafond Bash relevé) : `docs/solutions/tooling-decisions/a-detached-background-dispatch-cannot-outlive-a-headless-session.md`.
