---
ticket: cpp#234 + cpp#235 (mika#2573)
kind: fix
class: one-definition (admission + lethality unifiées, ancrage élargi)
status: ready-for-qa (édition guardrail bloquée → apply validé Vincent)
---

# Une seule définition de `eval` en position de commande, pour les DEUX couches, ancrage élargi — Plan (cpp#234, cpp#235)

Suite directe de #233 (case A, létalité seule). #233 a laissé les deux couches
avec DEUX définitions divergentes de « `eval` en position commande » : la
létalité utilisait `_EVAL_COMMAND_POSITION_RE` (narrow), l'admission
(`TIER3_PATTERNS`) gardait le bareword `\beval\s`. Deux conséquences tombent de
cette divergence ; on les corrige en la collapsant sur UN seul objet-motif
partagé, référencé par les deux couches (aucune duplication).

## Goal Capsule

**cpp#235 (admission).** `cargo test -p mika-agent --test eval <name>` (et
`--test=eval`, `node --eval`) reste REFUSÉ à l'admission (`is_tier3_dangerous`
True) parce que l'admission consulte encore `\beval\s` — le mot `eval` y est le
NOM de la cible de test (`crates/mika-agent/tests/eval.rs`), pas le builtin.
Aucun pilote ne peut lancer les tests d'intégration `eval` du crate dans le bac
à sable. But : l'admission adopte la MÊME définition position-commande que la
létalité (décision ratifiée Vincent → Prime : c'est bien l'admission).

**cpp#234 (défense en profondeur).** L'ancrage narrow de #233 a cessé de
reconnaître quatre formes où `eval` EST le builtin en position commande :
`` x=`eval "$CMD"` `` (backtick), `if true; then eval "$CMD"; fi` (mot-clé),
`sudo eval "$CMD"`, `env eval "$CMD"` (préfixes d'exécution). Ces formes
restaient refusées mais n'étaient plus TERMINALES — coupe-circuit affaibli,
régression de défense en profondeur. But : élargir l'ancrage partagé.

Classe cpp#205 (terminal réservé au danger prouvé). Une PR fermant les DEUX
tickets (un seul changement cohérent « une définition d'eval »). Gate tier1
(`is_tier1_auto_approve`) JAMAIS touché.

## Le fix

`tier1.py`, trois éditions, autour d'UN objet partagé :

1. Définir `_EVAL_COMMAND_POSITION_RE` AVANT `TIER3_PATTERNS`, élargi (cpp#234) :

   ```python
   _EVAL_COMMAND_POSITION_RE = re.compile(
       r"(?:^|[|&;\n(`]|(?:\b(?:then|do|else|elif)|!)\s+)"
       r"\s*"
       r"(?:(?:sudo|env|exec|command|nohup|time|xargs)(?:\s+-\S+)*\s+)*"
       r"eval\s"
   )
   ```

   Ancres d'origine `^|[|&;\n(]` conservées telles quelles ; ajout du backtick,
   des mots-clés d'ouverture (`\b` pour ne pas ancrer sur un mot finissant par
   `do`/`else`/…), et de la chaîne optionnelle de préfixes d'exécution.

2. **cpp#235** : dans `TIER3_PATTERNS` (admission), remplacer l'entrée
   `re.compile(r"\beval\s")` par l'objet partagé `_EVAL_COMMAND_POSITION_RE`.

3. La létalité (`_TIER3_VERB_PATTERNS_FOR_LETHALITY`) référence le MÊME objet —
   la substitution `p.pattern == r"\beval\s"` disparaît (l'entrée d'eval dans
   `TIER3_PATTERNS` EST déjà l'objet partagé) ; le tuple devient
   `TIER3_PATTERNS[:-1]`. Un seul motif, deux sites d'usage.

Non-régression subtile : le préfixe `env` ne matche que `env … eval`, jamais
`env VAR= cargo …` (désactivation de proxy) — cette commande ne porte aucun
token `eval`, donc le `eval\s` final ne matche pas ; son refus reste sur son
propre axe d'admission (ni tier3, ni auto-approve), intact.

Édition guardrail (`TIER3_PATTERNS` = admission) refusée par le classifieur
auto-mode (`[Self-Modification]`/`[Security Weaken]`) ; patch exact remis pour
apply validé Vincent (approbation #234/#235 avec #236 en une session manuelle),
voie cpp#223/#231. On ne contourne PAS.

## Acceptance criteria

- [x] **AC1 — cpp#235 positifs admis + survivables.** `cargo test -p mika-agent
  --test eval x`, `node --eval "x"`, `cargo test --test=eval x` →
  `is_tier3_dangerous` False, `is_tier3_dangerous_for_lethality` False,
  `_denial_is_terminal` False (worktree git réel). Validé par regex-probe
  standalone (`after_probe.py`, ALL GREEN) ; tests pytest écrits (rouges tant
  que le patch source n'est pas appliqué).
- [x] **AC2 — cpp#234 quatre formes redeviennent terminales.**
  `` x=`eval "$CMD"` ``, `if true; then eval "$CMD"; fi`, `sudo eval "$CMD"`,
  `env eval "$CMD"` → refusées (True) ET terminales (True). C'est la régression
  #233 fermée.
- [x] **AC3 — négatifs classiques inchangés.** `eval "$(curl …)"`, `x | eval y`,
  `foo && eval x`, `foo; eval x`, `(eval x)` → refusés ET terminaux.
- [x] **AC4 — une seule définition, deux couches.** Le MÊME objet compilé
  `_EVAL_COMMAND_POSITION_RE` est référencé par `TIER3_PATTERNS` ET
  `_TIER3_VERB_PATTERNS_FOR_LETHALITY` ; `\beval\s` absent de l'admission.
- [x] **AC5 — non-régression env-proxy (règle DIFFÉRENTE, non touchée).**
  `env HTTPS_PROXY= cargo test x` : l'ancre eval ne matche pas
  (`_EVAL_COMMAND_POSITION_RE.search` None), `is_tier3_dangerous` inchangé
  (False), reste refusé sur son propre axe (`is_safe_bash_command` False,
  `is_tier1_auto_approve` False).
- [x] **AC6 — non-régression #233 + suite.** Positif fondateur #233
  (`cargo test --test eval … | sed -n …`) admis ET survivable ; cpp#205/#213/
  #2562/#2565 rejeux inchangés ; gate tier1 intact. Suite complète verte
  APRÈS apply du patch source.

## Fire-Disposition

- **Feu** : (a) over-refusal d'admission bloquant les tests `eval` de mika-agent
  dans le sandbox (cpp#235, mika#2573) ; (b) coupe-circuit létalité affaibli sur
  quatre formes réelles d'`eval` (cpp#234, régression #233).
- **Éteint** : `--test eval`/`--test=eval`/`--eval` redeviennent admis ; les
  quatre formes backtick/mot-clé/`sudo`/`env` redeviennent terminales ; une
  seule définition partagée supprime la divergence à la source.
- **Vérif de sortie** : probe re-jouée avant/après (is_tier3_dangerous ET
  _denial_is_terminal) ; QA MPC ; apply guardrail sous validation Vincent, puis
  suite verte + verify-pipeline. Merge, redeploy.
- **Résidu** : édition `TIER3_PATTERNS` en attente d'apply humain (bloc
  auto-mode) ; aucun contournement. `| sh` hors scope (inchangé).

## Vérif (verbatim)

`uv sync --extra dev` ; `uv run pytest` ; `uv run ruff check .` ; `uv run mypy
src` ; `./scripts/verify-pipeline.sh main-updated` (docs+source). Édition source
`TIER3_PATTERNS` bloquée par le classifieur auto-mode → suite verte différée à
l'apply validé Vincent ; logique du patch prouvée par regex-probe standalone
(admission-après + létalité-après, ALL GREEN) et par la probe avant-état sur les
vraies fonctions (worktree git réel).

## Références

- Solution : `docs/solutions/tooling-decisions/a-read-filter-and-an-in-worktree-source-edit-are-not-proven-danger.md` (section « ONE definition, both layers, WIDENED anchor (cpp#234 + cpp#235) »).
- Lignée eval : #233 (case A, létalité, mergé fef8b93) ; plan `2026-09-29-005-fix-2573-eval-command-position-plan.md`.
- Doctrine : cpp#205 (default-survivable) ; escalade édition guardrail : cpp#223/#231. Tickets : cpp#234, cpp#235, mika#2573.
