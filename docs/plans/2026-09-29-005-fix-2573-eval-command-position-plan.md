---
ticket: mika#2573 (case A) / cpp#231
kind: fix
class: lethality-only (regression triage of a syntactic over-refusal)
status: ready-for-qa
---

# `eval` létal seulement en position de commande — Plan (case A, cpp#231)

Suite de la PR case B (#229). La mort de mika#2573 venait du mot `eval`, PAS du sed (correction de prémisse établie a la source).

## Goal Capsule

`cargo test -p mika-agent --test eval <name> 2>&1 | sed -n '/running 1 test/,/test result/p' | head -40` est TERMINAL et a tué plusieurs pilotes (8a3eb33b 20:02Z, aafb92e0 04:11Z). Cause : `\beval\s` (dans `_TIER3_VERB_PATTERNS_FOR_LETHALITY`) matche le bareword `eval` = **nom du binaire de test** (`--test eval`), pas le builtin. But : `eval` létal SEULEMENT en position de commande ; l'argument `--test eval` / `--eval` devient survivable. Classe cpp#205 (terminal réservé au danger prouvé) ; létalité seule, admission byte-identique.

## Le fix

`tier1.py` : swap de la seule entrée `\beval\s` du tuple létalité pour `_EVAL_COMMAND_POSITION_RE = re.compile(r"(?:^|[|&;\n(])\s*eval\s")`. `TIER3_PATTERNS` (admission) inchangé — le REFUS continue de consulter `\beval\s`, la commande reste DENIED ; seule `_denial_is_terminal` bascule.

Édition guardrail refusée par le classifieur auto-mode (`[Self-Modification]`), appliquée sous validation Vincent (Remote Control), voie cpp#223.

## Acceptance criteria

- [x] **AC1 — case A survivable.** verbatim `cargo test … --test eval … | sed -n … | head -40` → `is_tier3_dangerous_for_lethality` False ; idem `foo --eval bar`.
- [x] **AC2 — vrai `eval` en position commande terminal.** `eval "$(x)"`, `x | eval y`, `foo && eval x`, `foo; eval x`, `(eval x)` → True.
- [x] **AC3 — admission byte-identique.** `is_tier3_dangerous('eval "$(x)"')` et `is_tier3_dangerous("cargo test --test eval … | sed -n …")` restent True ; `TIER3_PATTERNS` / `is_tier1_auto_approve` intacts.
- [x] **AC4 — non-régression.** suite complète verte ; case B (#2565), cpp#203/#205/#213 inchangés. Jamais tier1 gate.

## Fire-Disposition

- **Feu** : deny terminal du binaire de test `eval` (mika#2573), loop-breaker de la reprise #2577. **Éteint** : `--test eval` redevient survivable ; le vrai `eval` reste terminal.
- **Vérif de sortie** : QA MPC + re-probe `_denial_is_terminal(caseA)` False. Merge, redeploy, restart n°17, reprise #2577.
- **Résidu** : aucun. `| sh` inchangé (hors scope, angle mort inverse noté ailleurs).

## Vérif (verbatim)

`uv run pytest` → vert ; `ruff check .` → clean ; `mypy src` → clean ; `verify-pipeline.sh main-updated` → passed (docs+source).

## Références

- Solution : `docs/solutions/tooling-decisions/a-read-filter-and-an-in-worktree-source-edit-are-not-proven-danger.md` (section « Case A, LANDED »).
- Case B : #229 (mergé 8ff585b). Doctrine : cpp#205 ; escalade édition : cpp#223. PR : cpp#231.
