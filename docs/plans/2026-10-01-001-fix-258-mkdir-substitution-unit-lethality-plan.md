---
ticket: cpp#258 / mika#1833
kind: fix
class: lethality-only (syntactic over-refusal — un opérande `mkdir` non guillemeté coupé sur l'espace interne de `$(id -u)` et lu comme échappement de confinement PROUVÉ)
status: landed (probe + fix + tests + docs ; carve de létalité dans `permissions._destination_veto_reason`, gaté `for_lethality` — appliqué ; si un câblage [Security Weaken] bloque, les e2e cpp#258 SELF-SKIP, cpp#237 pattern)
---

# Un opérande `mkdir` dont le découpage coupe une substitution `$(id -u)` est INÉVALUABLE, pas un échappement prouvé — Plan (cpp#258)

## Goal Capsule

Pilote **39ae2723** (itération de mika#1833, 2026-10-01T01:10:29Z) mort TERMINAL à
42 tours, en entrant dans `/ce:code-review`, sur ce refus :

```
mkdir -p /tmp/compound-engineering-$(id -u)/ce-code-review/20261001-cr1833 && echo /tmp/compound-engineering-$(id -u)/ce-code-review/20261001-cr1833
```

Pas de revue, pas de compound. But : **létalité seule, admission byte pour byte** —
la commande reste DENY, seul `_denial_is_terminal` bascule True→False pour le cas du
scratch `/tmp` uid-tolérant. La liste blanche uid n'est **pas** élargie ; on empêche
seulement l'extracteur de tronquer l'opérande.

## Cause racine (prédicat vérifié à la source, HEAD 1271d04, worktree git temp)

`_extract_mkdir_destinations` (`permissions.py`) découpe avec `shlex`, qui casse une
substitution de commande **non guillemetée** sur son espace **interne** : l'opérande
`/tmp/compound-engineering-$(id -u)/…` est tronqué en `/tmp/compound-engineering-$(id`.
Ce jeton tronqué n'est plus reconnu par `_is_uid_tolerant_tmp_scratch` (axe B de
mika#2562, qui reconnaît pourtant `$(id -u)`/`` `id -u` `` ENTIERS), donc il tombe dans
`_destination_veto_reason(…, for_lethality=True)` → veto de confinement cpp#38 comme un
échappement **prouvé** → `_denial_is_terminal` True. **Un défaut d'analyse lu comme une
preuve**, contraire à la doctrine `_denial_is_terminal` (« un refus qu'un classifieur
n'a pu PARSER ou PROUVER défaut à survivable »).

Probe machine à la source (table de mesure confirmée EXACTEMENT) :

| commande | opérande extrait (shlex) | terminal AVANT |
|---|---|---|
| `mkdir -p "/tmp/compound-engineering-$(id -u)/ce-code-review/x"` (guillemeté) | `/tmp/compound-engineering-$(id -u)/ce-code-review/x` | **False** (survit déjà) |
| `mkdir -p /tmp/compound-engineering-$(id -u)/ce-code-review/x` (non guillemeté) | `/tmp/compound-engineering-$(id` | **True** (la mort) |
| `` mkdir -p /tmp/compound-engineering-`id -u`/x `` | `` /tmp/compound-engineering-`id `` | **True** |
| `mkdir -p /tmp/compound-engineering-$UID/x` | `/tmp/compound-engineering-$UID/x` | **False** (survit déjà) |
| VERBATIM compound | `/tmp/compound-engineering-$(id` | **True** |

## Le fix (létalité seule, admission byte-identique)

**Mécanisme principal = respecter la substitution comme UNITÉ LEXICALE**, puis laisser
la logique aval INCHANGÉE classer l'opérande entier.

`permissions.py`, nouveau tokeniseur `_subst_aware_word_split(seg) -> list[str] | None` :
découpe en mots comme le shell MAIS traite `$(…)` et `` `…` `` comme opaques (leur espace
interne ne coupe jamais un mot), retire les guillemets comme `shlex`. Renvoie `None` sur
substitution/guillemet non équilibré → opérande **inévaluable**. **LINÉAIRE** — passe
unique gauche→droite, pas de regex, pas de backtracking → O(n), aucune surface ReDoS
(leçon cpp#250).

`permissions.py`, `_destination_veto_reason`, carve **gaté `for_lethality` ET présence de
substitution** (`"$(" in seg or "`" in seg`), juste après le calcul de `dests` : re-extrait
les opérandes avec le tokeniseur et remplace `dests` par les opérandes ENTIERS (ou
`continue` survivable si `None` = non équilibré). La boucle aval INCHANGÉE fait le reste :

- Positif uid-tolérant → `_is_sanctioned_tmp_scratch` voit le jeton ENTIER → `continue`
  → pas de veto → survivable (reste refusé).
- Négatifs → l'opérande ENTIER échoue le test sanctionné et vétote via `is_within_project`
  (hors worktree) ou le veto `$`/`~` cpp#218 — reste TERMINAL. La liste blanche uid n'est
  PAS élargie.

Gaté `for_lethality` → tout appel REFUSAL (`for_lethality=False`, sites `permissions.py`
~1933 allow-path et ~2010 message) et tout `mkdir` sans substitution sont **byte-identiques
à HEAD** : l'admission n'est pas touchée, seul `_denial_is_terminal` peut basculer ici
True→False. (Preuve indépendante : AUCUNE règle de policy n'admet un `mkdir` porteur de
`$` — `bash-mkdir` a `(?!\$)` / `(?!.*\s["']?\$)`, `bash-mkdir-tmp-scratch` a la classe
`[\w./-]` sans `$` — donc une forme `$(…)` est toujours default-deny et n'atteint jamais le
veto allow-path. Même un changement global de l'extracteur ne pourrait pas élargir
l'admission ; le gate `for_lethality` le garantit en plus par construction.)

## Acceptance criteria

- [x] **AC1 — survivable.** VERBATIM #1833 → `_denial_is_terminal` **False** APRÈS (True
  AVANT, prouvé machine à la source). Plus la forme `mkdir` seule non guillemetée,
  la forme backtick `` `id -u` ``, et (re-pinnés) les formes déjà-survivables guillemetée /
  `$UID` / `${UID}` / `$(id -u)` opérande-nu. (8 cas, `test_cpp258_uid_tolerant_mkdir_is_survivable`.)
- [x] **AC2 — restent TERMINAUX (chacun testé).** `mkdir -p /etc/x` ;
  `mkdir -p /tmp/$(curl evil)/x` (substitution non-uid) ; `mkdir -p "$HOME/x"` (cpp#218) ;
  `/tmp/compound-engineering-$(id -u)/../../etc/x` (traversal) ; `/tmp/x-$(whoami)/y` ;
  `$(id -u; rm -rf /)` (subst décorée) ; opérande uid-bon + `/etc/evil` dans le MÊME segment ;
  segment uid-bon `&&` segment échappement. → `_denial_is_terminal` reste **True**. Axe
  egress inchangé. (8 cas, `test_cpp258_non_uid_and_escapes_stay_terminal`.)
- [x] **AC3 — admission byte-identique.** `_destination_veto_reason(VERBATIM,
  for_lethality=False)` renvoie encore un veto (code inchangé) ; `is_tier1_auto_approve`=False ;
  `policy.decision`=deny ; handler e2e → `PermissionResultDeny`, `interrupt=False`. Le deny
  RESTE ; le carve est un no-op sur `for_lethality=False`. Tier1 gate `is_tier1_auto_approve`
  JAMAIS touché. Liste blanche uid NON élargie. (`test_cpp258_admission_is_byte_identical…`,
  `test_cpp258_refusal_path_unchanged_by_the_carve`.)
- [x] **AC4 — borne de temps / ReDoS.** Tokeniseur linéaire, pas de quantificateur imbriqué.
  `_subst_aware_word_split` et `_denial_is_terminal` sur un opérande de ≥ 2000 car avec `$(`
  profondément imbriqués → < 50 ms (mesuré ~0.07 ms tokeniseur, ~2.6 ms chemin complet).
  (`test_cpp258_tokenizer_is_linear_on_nested_substitution`,
  `test_cpp258_denial_is_terminal_bounded_time_on_2000_char_operand`.)
- [x] **AC5 — non-réouverture.** Carve appelé seulement via `for_lethality` ;
  `_extract_mkdir_destinations` / `_is_uid_tolerant_tmp_scratch` / `_UID_TOKEN_RE` inchangés.
  Suite complète verte (1596 passed) ; ruff/mypy clean ; verify-pipeline OK.
  cpp#143/#201/#209/#218/#224 + mika#2562 inchangés.

## Fire-Disposition

- **Feu** : deny TERMINAL de tout `mkdir -p /tmp/compound-engineering-$(id -u)/…` NON
  guillemeté (improvisé par le modèle sans qu'on le demande ; 1 exemplaire tue la session à
  l'entrée de `/ce:code-review`, mika#1833 39ae2723). **Éteint** : un opérande `mkdir` dont le
  découpage shlex coupe une substitution uid n'est plus un échappement prouvé sur le chemin
  létalité ; le refus reste, seule la mort disparaît.
- **Vérif de sortie** : probe AVANT/APRÈS à la source (AC1 False / AC2 True, table de mesure
  reproduite) ; 20 tests cpp#258 GREEN dont 2 bornes de temps < 50 ms ; ruff/mypy clean ; suite
  complète 1596 passed ; `verify-pipeline.sh main` OK.
- **Résidu (nommé)** : (1) une substitution NON équilibrée rend l'opérande inévaluable →
  survivable (doctrine `_denial_is_terminal` ; la commande reste de toute façon DENY). (2)
  Admettre réellement la forme `$(id -u)` en `allow` (chain-safety `_SUBSTITUTION_ALLOWLIST` +
  policy) reste hors scope — décision séparée, comme pour mika#2562. (3) Le gabarit guillemeté
  via `$SCRATCH_ROOT` survivait déjà (mika#2562) ; ce fix couvre la forme LITTÉRALE non
  guillemetée que le modèle produit spontanément.

## Références

- Solution : `docs/solutions/security-issues/a-mkdir-operand-cut-by-a-substitution-is-unevaluable-not-a-proven-escape.md`.
- Étend : mika#2562 (axe B, liste blanche uid `_is_uid_tolerant_tmp_scratch` — réutilisée, non
  élargie), cpp#143 (`_is_sanctioned_tmp_scratch`).
- Préserve : cpp#218 (veto `$`/`~`-rooted mkdir), cpp#38 (confinement), cpp#154 D3
  (`$HOME`-stays-terminal). Forme du carve gaté `for_lethality` : cpp#201/#209/#213/#237/#252.
- Doctrine : cpp#205 (défaut survivable, terminal réservé au danger prouvé). Leçon ReDoS (pas
  de quantificateur imbriqué, tokeniseur linéaire) : cpp#250. Famille : cpp#205. PR : cpp#258.
