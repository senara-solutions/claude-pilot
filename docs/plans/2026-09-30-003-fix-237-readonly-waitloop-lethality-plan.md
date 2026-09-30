---
ticket: cpp#237 / mika#2105
kind: fix
class: lethality-only (syntactic over-refusal — un `sh -c` enveloppant une boucle d'attente lecture-seule lu comme danger prouvé)
status: blocked-on-guardrail-edit (probe + recognizer + tests + docs landed/staged ; le câblage `permissions._denial_is_terminal` gaté à Vincent, voie cpp#223/#231)
---

# Une boucle d'attente lecture-seule (`sh -c 'while … sleep … cat/du/tail'`) n'est pas un refus TERMINAL — Plan (cpp#237)

## Goal Capsule

Pilote #2105 (e1a6c78b) mort TERMINAL à 2026-09-29T15:13:20.327Z sur une boucle
d'attente PURE LECTURE :

```
sh -c 'n=0; while [ $n -lt 55 ]; do if [ -f .pilot-scratch/measures.txt ]; then cat .pilot-scratch/measures.txt; exit 0; fi; sleep 10; n=$((n+1)); done; du -sm target; tail -1 .pilot-scratch/cold0.log'
```

Tests `[ -f … ]`, `cat`, `du -sm target`, `tail -1`, `sleep`, arithmétique
`n=$((n+1))`, tout sous `.pilot-scratch/` et le worktree. Aucun verbe destructeur,
aucune écriture hors-worktree, aucun réseau, aucune substitution/eval dangereuse.
Sous cpp#205 (terminal réservé au danger PROUVÉ) ce refus ne doit pas être létal.
But : létalité seule, admission byte-identique — la commande reste DENY, seul
`_denial_is_terminal` bascule True→False.

## Cause racine (prédicat vérifié à la source, HEAD c136814, worktree git temp)

Le prédicat qui tire est l'entrée `\bsh\s+-c\b` de `TIER3_PATTERNS`
(`tier1.py:201`) — une des entrées de `_TIER3_VERB_PATTERNS_FOR_LETHALITY`
(cpp#205), donc `is_tier3_dangerous_for_lethality(cmd)` renvoie True et
`_denial_is_terminal` renvoie True à sa PREMIÈRE porte (`permissions.py:893-898`).
Probe :

- `_denial_is_terminal(VERBATIM)` = **True** (AVANT).
- `is_tier3_dangerous_for_lethality(VERBATIM)` = **True** ; seul motif qui matche
  le texte strippé = `\bsh\s+-c\b` (span 0..5). `rm_confined`=False,
  `sed_confined`=False.
- `_redirect_destination_veto_reason` = None ; `_destination_veto_reason(…,
  for_lethality=True)` = None. Donc la SEULE cause de létalité = le verbe `sh -c`.

Les hypothèses candidates du ticket, RÉFUTÉES à la source : (a) l'arithmétique
`$((n+1))` lue comme substitution `$(` — NON (elle est mono-quotée, n'atteint
aucun scanner de redirection, et n'est pas une substitution de commande) ; (b)
`du`/`tail`/`cat` lus comme écriture — NON ; (d) un token `while`/`done`/`;` —
NON. Seule (c) — l'enveloppe `sh -c '<script inline>'` — est le tueur. C'est la
famille cpp#205 : `sh -c` est un WRAPPER, pas un verbe intrinsèquement destructeur
comme `rm -rf` ; sa létalité EST la létalité du script qu'il enveloppe. Quand ce
script est une boucle d'attente lecture-seule, il n'y a pas de danger prouvé.

## Le fix (létalité seule, admission byte-identique)

Reconnaisseur NOUVEAU, purement lexical, létalité seule, sibling exact des carves
cpp#213 (`rm_confined_to_pilot_scratch`) et mika#2565 (`sed_i_confined_to_worktree`).

`tier1.py` (helpers NOUVEAUX, jamais appelés par `is_tier3_dangerous` /
`TIER3_PATTERNS` / tier1 / `is_tier3_dangerous_for_lethality`) :

- `is_readonly_waitloop_script(command) -> bool` : True SSI la commande est une
  boucle d'attente lecture-seule auto-contenue. Déballe une enveloppe UNIQUE
  `sh -c '<script>'` / `bash -c "<script>"` (rien après le guillemet fermant,
  sinon fail-closed), puis exige : (1) aucune substitution de commande
  (` ``, `$(cmd)`, funsub `${ …}`, `$'…'`) — l'arithmétique `$((…))` SANS `$`
  imbriqué est blanchie AVANT le contrôle `$(` (donc `n=$((n+1))` est admis mais
  `$(( $(cmd) ))` reste rejeté) ; (2) aucun métacaractère pipe/background/
  subshell/redirection (`| & < > ( )`) ; (3) présence d'un mot-clé de boucle
  (`while`/`until`/`for`) + `do` + `done` ; (4) chaque statement (split
  quote-aware sur `;`/newline) est une commande simple lecture-seule — mot de
  commande dans l'allowlist close (`[`/`test`, `cat`, `head`, `tail`, `du`, `ls`,
  `wc`, `sleep`, `exit`, `:`, `true`, `false`, `echo`, `printf`, `grep`, `stat`,
  `dirname`, `basename`), tête `for NAME in <liste>`, ou affectation `NAME=valeur`
  — dont chaque opérande fichier est WORKTREE-RELATIF (rejette absolu, `..`, `~`,
  variables d'env dangereuses `$HOME`/`$OLDPWD`/`$PWD`/…).
- Helpers privés : `_waitloop_operand_is_safe`, `_waitloop_statement_is_readonly`,
  `_waitloop_split_statements`, et les constantes `_WAITLOOP_*`.

`permissions.py` `_denial_is_terminal` (LE CÂBLAGE GUARDRAIL — gaté à Vincent) :
ajouter `is_readonly_waitloop_script` à l'import `from .tier1 import` et l'étendre
au garde tier3 :

```python
if (
    is_tier3_dangerous_for_lethality(command)
    and not rm_confined_to_pilot_scratch(command, cwd)
    and not sed_i_confined_to_worktree(command, cwd)
    and not is_readonly_waitloop_script(command)   # cpp#237
):
    return True
```

Quand le carve s'applique, on tombe À TRAVERS vers les vetos redirection/
destination existants (masque cpp#236, `_redirect_destination_veto_reason`,
`_destination_veto_reason(for_lethality=True)`) — inchangés — qui renvoient None
pour une boucle reconnue (aucun `>`/cible hors-worktree par construction), donc la
commande devient survivable. Le carve est un NO-OP sur tout appel
`for_lethality=False` : admission byte-identique.

Fail-closed (aucune vraie danger jamais exempté) : réseau (`curl`/`wget`), verbe
destructeur (`rm`), écriture hors-worktree (`> /etc/x`, `> "$HOME/y"`), `eval`,
substitution `$(cmd)`, pipe vers `sh`, ou un `sh -c` non déballable proprement →
le reconnaisseur renvoie False → le verbe `sh -c` garde le refus TERMINAL.

## Acceptance criteria

- [x] **AC1 — survivable.** Le VERBATIM #2105 → `_denial_is_terminal` False APRÈS
  (True AVANT, prouvé à la source). Plus une boucle isolée
  `while … sleep … cat .pilot-scratch/x`, une enveloppe `bash -c` double-quote avec
  compteur `$((…))`, une boucle `for … in`. *(AVANT vérifié machine à la source ;
  APRÈS par simulation à la source du garde câblé (probe237c) — validation machine
  du câblage bloquée par le classifieur, voir Fire-Disposition.)*
- [x] **AC2 — restent TERMINAUX (chacun testé).** La MÊME boucle avec `curl`/`wget`
  (réseau), `rm`/`rm -rf` (destructif), redirection hors-worktree
  (`> /etc/passwd`, `> "$HOME/y"`), `eval`, substitution `$(rm -rf /)`, pipe vers
  `sh`, `$(( $(cmd) ))` (subst dans l'arithmétique) → `_denial_is_terminal` reste
  True (reconnaisseur False, prouvé). Axe egress inchangé (`HTTPS_PROXY= curl`
  reste terminal).
- [x] **AC3 — admission byte-identique.** `is_tier3_dangerous(VERBATIM)`=True,
  `is_tier1_auto_approve`=False, `policy.decision`=deny — INCHANGÉS (prouvé à la
  source). Le deny RESTE ; le carve est un no-op sur `for_lethality=False`. Tier1
  gate `is_tier1_auto_approve` JAMAIS touché.
- [x] **AC4 — non-réouverture.** Nouveau helper appelé seulement par
  `_denial_is_terminal` ; `is_tier3_dangerous_for_lethality` et
  `TIER3_PATTERNS` inchangés. Suite complète verte (1554 passed, 2 skipped — les
  deux tests e2e cpp#237 SKIPPÉS tant que le câblage n'est pas appliqué).
  cpp#154/#157/#201/#203/#205/#209/#213/#234/#235/#236/#241/#2565/#2573 inchangés
  (#176/#178 revus dans les deux sens).

## Fire-Disposition

- **Feu** : deny TERMINAL de toute boucle d'attente lecture-seule enveloppée dans
  `sh -c`/`bash -c` (mika#2105 tueur e1a6c78b) — classe fréquente : sonder un
  fichier de mesures, attendre un artefact de build, poller `.pilot-scratch/`.
  **Éteint** : un `sh -c` enveloppant une boucle lecture-seule à cibles
  worktree-relatives n'est plus un danger prouvé sur le chemin létalité ; le refus
  reste, seule la mort disparaît.
- **Vérif de sortie** : reconnaisseur `tier1.py` + tests unitaires GREEN
  (`TestReadonlyWaitloopScript`, 5 passed) ; ruff/mypy clean ; suite complète
  verte ; probe simulé du garde câblé (AC1 False / AC2 True). Application du hunk
  `permissions.py` sous fenêtre manuelle Vincent, puis les 2 tests e2e cpp#237
  passent (skip levé) et re-probe `_denial_is_terminal(VERBATIM)` False.
- **Résidu (nommé)** : (1) le reconnaisseur est volontairement STRICT — pas de
  pipe, pas de subshell, pas de `&&`/`||` : une boucle lecture-seule utilisant ces
  formes reste TERMINALE (fail-closed, hors scope). (2) Une boucle lecture-seule
  lisant un fichier via une variable `$n` non prouvée numérique est admise si `$n`
  n'est pas une variable d'env dangereuse ; le refus persistant (jamais exécuté)
  fait que ce n'est pas une brèche. (3) Corriger AUSSI l'admission (déballer
  `sh -c` de boucles sûres) est hors scope — décision Vincent.

## Références

- Solution : `docs/solutions/security-issues/a-sh-c-wrapped-read-only-wait-loop-is-not-proven-danger.md`.
- Doctrine : cpp#205 (défaut survivable, terminal réservé au danger prouvé ;
  `_TIER3_VERB_PATTERNS_FOR_LETHALITY`). Siblings de carve : cpp#213
  (`rm_confined_to_pilot_scratch`), mika#2565 (`sed_i_confined_to_worktree`),
  cpp#209 (forme du carve gaté `for_lethality`). Reconnaissance boucle lecture-seule
  d'admission : cpp#92/#151 (`_is_sanctioned_readonly_for_loop`).
- Escalade édition guardrail : cpp#223/#231. PR : cpp#237.
