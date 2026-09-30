---
ticket: cpp#252 / mika#2252
kind: fix
class: lethality-only (syntactic over-refusal — une substitution de processus lecture-seule `<(git show …)` lue comme danger prouvé)
status: blocked-on-guardrail-edit (probe + recognizer + tests + docs landed/staged ; le câblage `permissions._denial_is_terminal` bloqué par le classifieur [Security Weaken], gaté à Vincent, voie cpp#223/#231)
---

# Une substitution de processus lecture-seule (`diff <(git show …) file >/dev/null`) n'est pas un refus TERMINAL — Plan (cpp#252)

## Goal Capsule

Pilote #2252 (5cfd4bc8) mort TERMINAL à 2026-09-30T13:52:26.824Z sur une
comparaison PURE LECTURE :

```
diff <(git show HEAD:crates/mika-agent/src/tools/pr_merge_with_gate.rs) .pilot-scratch/pr_merge_with_gate.rs.orig >/dev/null 2>&1; echo "--- vérification que la restauration est complète ---"; cargo t
```

`diff` entre une substitution de processus `<(git show HEAD:<chemin>)` (lecture
pure d'un blob git) et un fichier du worktree, sortie jetée (`>/dev/null 2>&1`),
suivie d'un `echo` et d'un `cargo t`. Aucun verbe destructeur, aucune écriture
hors-worktree (`/dev/null` n'est pas une exfiltration), aucun réseau. Sous cpp#205
(terminal réservé au danger PROUVÉ) ce refus ne doit pas être létal. But : létalité
seule, admission byte-identique — la commande reste DENY, seul `_denial_is_terminal`
bascule True→False.

## Cause racine (prédicat vérifié à la source, HEAD 216033d, worktree git temp)

Le prédicat qui tire est l'entrée `<\(` de `TIER3_PATTERNS` (`tier1.py:229`) — une
des entrées de `_TIER3_VERB_PATTERNS_FOR_LETHALITY` (`= TIER3_PATTERNS[:-1]`,
cpp#205), donc `is_tier3_dangerous_for_lethality(cmd)` renvoie True et
`_denial_is_terminal` renvoie True à sa PREMIÈRE porte (`permissions.py:894-900`).
Probe :

- `_denial_is_terminal(VERBATIM)` = **True** (AVANT).
- `is_tier3_dangerous_for_lethality(VERBATIM)` = **True** ; seul motif qui matche
  le texte strippé = `<\(`. `rm_confined`=False, `sed_confined`=False,
  `is_readonly_waitloop_script`=False.
- Le `>/dev/null` est STRIPPÉ par `_STDOUT_DEVNULL_RE` avant le contrôle de motif
  (cpp#130), donc ce n'est PAS la redirection qui tue.
- `_redirect_destination_veto_reason(VERBATIM)` = None ; `_destination_veto_reason(…,
  for_lethality=True)` = None. Donc la SEULE cause de létalité = la substitution de
  processus `<(`.

Les hypothèses candidates du ticket, RÉFUTÉES à la source : (a) le `>` de
`>/dev/null` lu comme redirection hors-worktree — NON (strippé par cpp#130, et les
deux vetos renvoient None) ; (c) l'interaction `bash-grep`/`diff` — NON (le motif
verbe tire AVANT tout veto). Seule (b) — la substitution de processus `<( … )` lue
comme substitution dangereuse — est le tueur. C'est la famille cpp#205 : `<(` est
en `TIER3_PATTERNS` parce qu'il peut SMUGGLER n'importe quoi dans son intérieur ; sa
létalité EST la létalité de la commande intérieure. Quand chaque `<( CMD … )` a un
CMD lecture-seule d'une liste FERMÉE, il n'y a pas de danger prouvé.

## Gate MPC-ratifié (implémenté exactement, PAS le brouillon du ticket)

- Intérieurs admis dans `<( )` = liste FERMÉE de commandes LECTURE : **git show,
  git diff, cat, printf**. NOTE : `git diff` est DEDANS ; `echo` est DEHORS (le
  ticket listait git-show/cat/printf/echo — MPC remplace `echo` par `git diff`).
- SURVIVABLE : le verbatim 5cfd4bc8 ET `diff <(cat a) <(cat b)`.
- TERMINAL (reste) : `<(curl …)`, `<(bash …)`, `<(sh -c …)`, `<(eval …)`,
  `<(rm …)`, et toute substitution de SORTIE `>(…)`. Admission identique.
- Test de temps borné (< 50 ms) sur entrée pathologique (leçon ReDoS cpp#250 — pas
  de quantificateur imbriqué ; reconnaissance linéaire/lexicale).

## Le fix (létalité seule, admission byte-identique)

Reconnaisseur NOUVEAU, purement lexical, létalité seule, sibling exact des carves
cpp#213 (`rm_confined_to_pilot_scratch`), mika#2565 (`sed_i_confined_to_worktree`)
et cpp#237 (`is_readonly_waitloop_script`).

`tier1.py` (helpers NOUVEAUX, jamais appelés par `is_tier3_dangerous` /
`TIER3_PATTERNS` / tier1 / `is_tier3_dangerous_for_lethality`) :

- `readonly_procsub_survivable(command) -> bool` : True SSI la létalité tier3 de la
  commande est due UNIQUEMENT à des substitutions de processus LECTURE
  `<( CMD … )`. Mécanisme = sibling cpp#213/#2565 : chaque `<( … )` ADMIS est
  blanchi et le reste est re-vérifié avec `is_tier3_dangerous_for_lethality`
  INCHANGÉ, donc toute AUTRE cause de danger prouvé (verbe destructeur chaîné,
  seconde substitution non admise) renvoie encore True là et reste terminale.
- `_readonly_procsub_masked(command) -> str | None` : scan lexical UNIQUE
  gauche→droite, conscient des guillemets. Renvoie la commande avec chaque
  `<( CMD … )` admis blanchi en espaces, ou `None` (fail-closed) si : une
  substitution de SORTIE `>( … )`, un intérieur `<( … )` hors liste fermée, une
  substitution de commande (`` ` ``/`$(`), ou une substitution/guillemet
  imbriqué/non équilibré/non terminé. `None` aussi si aucun `<( … )` admis trouvé.
- `_procsub_close_index(command, start) -> int | None` : index du `)` fermant,
  suivi de la profondeur de parenthèses, saute les régions guillemetées, linéaire.
- Constante `_PROCSUB_READ_INTERIOR_RE` : mot de tête dans la liste fermée suivi
  seulement de caractères d'argument qui ne chaînent/redirigent/substituent pas
  (`;`, `|`, `&`, `<`, `>`, `(`, `)`, `$`, backtick, newline exclus). Classe de
  caractères négative à étoile UNIQUE — linéaire, pas de backtracking catastrophique
  (leçon ReDoS cpp#250). `fullmatch` sur l'intérieur strippé.

`permissions.py` `_denial_is_terminal` (LE CÂBLAGE GUARDRAIL — BLOQUÉ par le
classifieur [Security Weaken], gaté à Vincent) : ajouter `readonly_procsub_survivable`
à l'import `from .tier1 import` et l'étendre au garde tier3 :

```python
if (
    is_tier3_dangerous_for_lethality(command)
    and not rm_confined_to_pilot_scratch(command, cwd)
    and not sed_i_confined_to_worktree(command, cwd)
    and not is_readonly_waitloop_script(command)
    and not readonly_procsub_survivable(command)   # cpp#252
):
    return True
```

Quand le carve s'applique, on tombe À TRAVERS vers les vetos redirection/
destination existants (masque cpp#236, `_redirect_destination_veto_reason`,
`_destination_veto_reason(for_lethality=True)`) — inchangés — qui renvoient None
pour le verbatim (le `>/dev/null` est reconnu comme non-écriture, cpp#130). Une
VRAIE redirection hors-worktree (`> /etc/passwd`, `> "$HOME/y"`) survit au
reconnaisseur (cpp#205 a retiré le catch-all `>` du set de létalité) mais est
RÉ-ARMÉE par ces vetos sur la commande COMPLÈTE, donc reste terminale. Le carve est
un NO-OP sur tout appel `for_lethality=False` : admission byte-identique.

Fail-closed (aucune vraie danger jamais exempté) : `>( … )`, intérieur hors liste
(`curl`/`wget`/`bash`/`sh -c`/`eval`/`rm`/`python3`), substitution de commande dans
l'intérieur, verbe destructeur chaîné, `<( … )` non équilibré → le reconnaisseur
renvoie False → le verbe `<(` garde le refus TERMINAL.

## Acceptance criteria

- [x] **AC1 — survivable.** Le VERBATIM #2252 → `_denial_is_terminal` False APRÈS
  (True AVANT, prouvé à la source). Plus `diff <(cat a) <(cat b)`,
  `diff <(git show HEAD:x) y >/dev/null`, `cat <(printf '%s' x)`,
  `diff <(git diff HEAD a) b`. *(AVANT vérifié machine à la source ; APRÈS par
  simulation à la source du garde câblé (probe_sim) — validation machine du câblage
  bloquée par le classifieur [Security Weaken], voir Fire-Disposition.)*
- [x] **AC2 — restent TERMINAUX (chacun testé).** `<(curl …)`/`<(wget …)` (réseau),
  `<(bash …)`/`<(sh -c …)`/`<(eval …)`/`<(python3 …)` (exécution arbitraire),
  `<(rm -rf …)` (destructif), une substitution de SORTIE `>(tee out)`, un verbe
  destructeur chaîné (`… && rm -rf /`), et une VRAIE redirection hors-worktree
  (`> /etc/passwd`, `> "$HOME/y"`) → `_denial_is_terminal` reste True (prouvé par
  simulation ; le reconnaisseur renvoie False pour les intérieurs/`>(…)`, et les
  redirections hors-worktree sont ré-armées par les vetos de destination). Axe
  egress inchangé.
- [x] **AC3 — admission byte-identique.** `is_tier3_dangerous(VERBATIM)`=True,
  `is_tier1_auto_approve`=False, `policy.decision`=deny — INCHANGÉS (prouvé à la
  source). Le deny RESTE ; le carve est un no-op sur `for_lethality=False`. Tier1
  gate `is_tier1_auto_approve` JAMAIS touché. `>/dev/null` reconnu comme
  non-écriture sans ouvrir de porte d'écriture.
- [x] **AC4 — non-réouverture.** Nouveau helper appelé seulement par
  `_denial_is_terminal` ; `is_tier3_dangerous_for_lethality` et `TIER3_PATTERNS`
  inchangés. Suite complète verte (1566 passed, 2 skipped — les deux tests e2e
  cpp#252 SKIPPÉS tant que le câblage n'est pas appliqué).
  cpp#154/#203/#205/#213/#229/#236/#237/#241/#245/#250 inchangés (#176/#178 revus
  dans les deux sens). Plus le test de temps borné < 50 ms.

## Fire-Disposition

- **Feu** : deny TERMINAL de toute comparaison lecture-seule via substitution de
  processus `<(git show …)`/`<(cat …)` (mika#2252 tueur 5cfd4bc8) — classe
  fréquente : comparer un blob git à un fichier restauré, `diff <(…) <(…)` de deux
  vues. **Éteint** : une substitution de processus lecture-seule d'intérieur dans la
  liste fermée n'est plus un danger prouvé sur le chemin létalité ; le refus reste,
  seule la mort disparaît.
- **Vérif de sortie** : reconnaisseur `tier1.py` + tests unitaires GREEN
  (`TestReadonlyProcsubSurvivable`, 7 passed dont temps borné < 50 ms) ; ruff/mypy
  clean ; suite complète verte (1566 passed, 2 skipped) ; probe simulé du garde
  câblé (AC1 False / AC2 True, tous prouvés). Application du hunk `permissions.py`
  sous fenêtre manuelle Vincent (import + ligne de garde — bloqué par [Security
  Weaken]), puis les 2 tests e2e cpp#252 passent (skip levé) et re-probe
  `_denial_is_terminal(VERBATIM)` False.
- **Résidu (nommé)** : (1) l'intérieur est volontairement STRICT — aucun `;`, `|`,
  `&`, `$`, `<`, `>` dedans : un `<(git show HEAD:x | head)` légitime reste TERMINAL
  (fail-closed, hors scope). (2) `echo` retiré de la liste (décision MPC) : un
  `<(echo …)` reste TERMINAL. (3) La substitution de commande `$(…)`/backtick
  n'importe où → fail-closed (le reconnaisseur décline), même si elle serait inerte.
  (4) Corriger AUSSI l'admission (admettre `<(git show …)` en allow) est hors scope
  — décision Vincent.

## Références

- Solution : `docs/solutions/security-issues/a-read-only-process-substitution-is-not-proven-danger.md`.
- Doctrine : cpp#205 (défaut survivable, terminal réservé au danger prouvé ;
  `_TIER3_VERB_PATTERNS_FOR_LETHALITY = TIER3_PATTERNS[:-1]`). Siblings de carve :
  cpp#213 (`rm_confined_to_pilot_scratch`), mika#2565 (`sed_i_confined_to_worktree`),
  cpp#237 (`is_readonly_waitloop_script`), cpp#209 (forme du carve gaté
  `for_lethality`). Masque redirection substitution-aware : cpp#236. `/dev/null`
  non-écriture : cpp#130. Leçon ReDoS (pas de quantificateur imbriqué) : cpp#250.
- Escalade édition guardrail : cpp#223/#231. PR : cpp#252.
