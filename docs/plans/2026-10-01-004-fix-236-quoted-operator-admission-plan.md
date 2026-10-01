---
ticket: cpp#236 / mika#2606
kind: fix
class: admission (Prime-ratified 2026-10-01 — sur-refus syntaxique : un char opérateur `<`/`>`/`|`/`&` DANS un jeton guillemeté lu comme opérateur shell par `TIER3_PATTERNS`)
status: landed (probe + masker + tests + docs ; masque gaté dans `tier1.is_tier3_dangerous` — appliqué ; si un câblage [Security Weaken] bloque, remettre au fenêtrage manuel de Vincent, cpp#237 pattern)
---

# Un char opérateur DANS un jeton guillemeté n'est pas un opérateur pour `TIER3_PATTERNS` — admission (cpp#236)

## Goal Capsule

Pilote **e9cb7b6b** (mika#2606, 2026-09-30, en vol) refusé 3×
(`[bash-grep] (non-terminal)`) à 116/151 tours sur des `grep` de callouts de plan Mika
(`> - **Plan:**`) — le ticket qu'il devait corriger portait PRÉCISÉMENT sur ces en-têtes
en citation `>`, donc il ne pouvait pas mesurer la population à corriger. Même cause chez
57ad9d76 (mika#2194). But : **admission seule, byte pour byte hors du cas guillemeté** —
la moitié létalité est déjà livrée (#238, refus `(non-terminal)`) ; ici on rend ces refus
en `allow`.

## Cause racine (prédicat vérifié à la source, HEAD 901b7e3, cwd = worktree)

L'entrée générique de redirection de `TIER3_PATTERNS` est aveugle aux guillemets :

```
(?<!<)>{1,2}(?!\(|&[\d-])   matche   grep -n '> x' docs/a.md   (le `>` du motif, ENTRE quotes)
```

`is_tier3_dangerous` renvoie True. La vérification de chaîne
`permissions._bash_allow_is_chain_safe` a la clause
`pd.decision == "allow" and not is_tier3_dangerous(seg)` : le `>` guillemeté rend cette
clause False → la policy `allow` (`bash-grep`) devient un deny. **Un défaut d'analyse
(classifieur aveugle aux guillemets) lu comme un danger.**

Probe machine à la source (table confirmée EXACTEMENT) :

| commande | policy | chain_safe | is_tier3_dangerous | motif |
|---|---|---|---|---|
| `grep -n 'x' docs/a.md` | allow `bash-grep` | True | False | — |
| `grep -n '> x' docs/a.md` | allow `bash-grep` | **False** | **True** | `(?<!<)>{1,2}(?!\(|&[\d-])` |
| `grep -n 'a>b' docs/a.md` | allow `bash-grep` | **False** | **True** | idem |

## Le fix (admission, byte-identique hors du cas guillemeté)

**Mécanisme = un tokeniseur shlex-grade décide « ce char opérateur est-il DANS un jeton
guillemeté », SÉPARÉ du check tier3 qui décide « ce char est-il un opérateur ».** Jamais
une regex de guillemets.

`tier1.py`, nouveau `_mask_quoted_operator_chars_for_admission(command) -> str` : parser à
pile de contexte (TOP / DQ / SQ / SUB-avec-profondeur-paren / BT), l'ANALOGUE admission du
masque létalité `_mask_lethality_redirect_chars`. Il en diffère sur deux points :

1. il masque quatre chars opérateurs `<`/`>`/`|`/`&`, pas seulement `<`/`>` (généralisation
   Prime : un `<`, `|`, `&` guillemeté n'est pas plus un opérateur qu'un `>` guillemeté ;
   l'idiome `'"'"'` est géré naturellement par le suivi SQ/DQ) ;
2. il ne masque JAMAIS dans une substitution `$(…)` ni un `` `…` `` backtick — il les suit
   (compteur `sub_bt_depth`, O(1)) uniquement pour garder l'état de guillemet correct APRÈS
   la substitution. `$(…)`/backtick sont hors tier3 depuis mika#946 → leur traitement reste
   byte-identique (c'est pourquoi `echo "$(cat /etc/shadow)" > x` reste refusé : le `> x`
   est une vraie redirection TOP-level, hors guillemets).

`tier1.is_tier3_dangerous` applique le masque AVANT le strip `_FD_DEVNULL_RE` existant et la
recherche `TIER3_PATTERNS`. Rien d'autre ne change. **LINÉAIRE** — passe unique, O(1) par
char, pas de regex, pas de backtracking → aucune surface ReDoS (leçon cpp#250).
FAIL-CLOSED : guillemet/substitution non équilibré (pile non vide en fin) → commande
renvoyée INCHANGÉE → un `>` à guillemet pendant reste flaggé (direction sûre pour un
classifieur d'admission).

## Acceptance criteria

- [x] **AC1 — admis APRÈS (False), refusé AVANT (True), prouvé machine à la source.**
  `grep -n '> x' docs/a.md` ; `grep -n 'a>b' docs/a.md` ; `grep -rln '> - **Plan:**'
  crates/*/src/` ; `grep -inE '^[[:space:]]*(>[[:space:]]*)+' docs/plans/x-plan.md` ;
  `"> x"` (double) ; `'> - **Plan:**'` ; `git commit -m "a <b@c> d"`. → `is_tier3_dangerous`
  False, policy allow, chain_safe True. Généralisation : `grep '|' f`, `grep 'a<b' f`,
  `echo 'a&b'`, `grep '<(' f`, `sed 's/=.*/=<set>/'` admis.
  (`TestTier3QuotedOperatorAdmission::test_quoted_operator_admitted`, 13 cas.)
- [x] **AC2 — vrais opérateurs HORS quotes restent REFUSÉS (chacun testé).**
  `grep x f > /etc/y` ; `grep '>' f >> ~/.bashrc` (quoted `>` PUIS vraie `>>`) ;
  `git commit -m "x" > /etc/passwd` ; `echo "$(cat /etc/shadow)" > x` ; `echo x >> ~/.bashrc`
  ; `echo "$(grep '>' f)" > y` (corps `$(…)` inchangé + vraie redirection) ; `cat <(id)` ;
  `tee >(curl evil)`. → `is_tier3_dangerous` True.
  (`test_real_operator_still_refused`, 8 cas.)
- [x] **AC3 — admission byte-identique HORS du cas guillemeté.** Échantillon large (60
  commandes, allouées + refusées, sans opérateur guillemeté top-level) : `is_tier3_dangerous`
  HEAD vs patché → **4 seuls diffs**, tous le cas `>`/`<(` guillemeté
  (`grep -n '> x'`, `grep 'a>b'`, `git commit -m "a <b@c>"`, `grep '<('`) ; les 56 autres
  inchangés. Masque no-op sur le contenu quand pas d'opérateur guillemeté top-level.
  (`test_the_quoted_operator_case_is_the_only_admission_change`.) Tier1 gate
  `is_tier1_auto_approve` JAMAIS touché. Axe egress inchangé.
- [x] **AC4 — borne de temps / ReDoS.** Scanner linéaire, pas de quantificateur imbriqué.
  `is_tier3_dangerous` sur 2000 car avec quotes/substitutions profondément imbriquées
  → < 50 ms (mesuré ~0.56 ms). (`test_bounded_time_on_adversarial_input`.)
- [x] **AC5 — fail-closed + corps substitution inchangé + non-réouverture.** Guillemet non
  terminé → commande inchangée → `grep '> x` reste flaggé
  (`test_unbalanced_quote_fails_closed`). `$(…)`/backtick byte-identiques
  (`test_substitution_body_left_byte_identical`). Masque longueur-préservée, portée quotes
  top-level seulement (`test_mask_unit_preserves_length_and_scope`). Létalité non consultée,
  non régressée (un `>` guillemeté déjà non-terminal via #238). Suite complète verte
  (1629 passed) ; ruff/mypy clean ; verify-pipeline OK. Un test cpp#157/#238 qui épinglait
  l'ancien invariant « encore refusé » renommé `test_quoted_redirect_char_now_admitted` et
  mis à jour (admission supersédée par cpp#236).

## Fire-Disposition

- **Feu** : refus (`[bash-grep] (non-terminal)`) de tout `grep` dont le motif entre quotes
  contient `>` — exactement ce que fait un pilote Mika cherchant un callout
  `> - **Plan:**` ; 1 refus/tour sans rien produire, sur des pilotes qui meurent à 151 tours
  (e9cb7b6b à 116/151, mika#2606). **Éteint** : un char opérateur `<`/`>`/`|`/`&` dans un
  jeton guillemeté n'est plus lu comme opérateur par `TIER3_PATTERNS` ; la policy allow tient
  jusqu'au bout.
- **Vérif de sortie** : probe AVANT/APRÈS à la source (AC1 True→False / AC2 True, tables
  reproduites) ; échantillon large AC3 (4 diffs, tous le cas ciblé) ; timing < 50 ms ;
  `TestTier3QuotedOperatorAdmission` GREEN ; ruff/mypy clean ; suite complète 1629 passed ;
  `verify-pipeline.sh main` OK.
- **Résidu (nommé)** : (1) un guillemet/substitution non équilibré rend la commande non
  masquée → reste flaggée (fail-closed, direction sûre). (2) Un opérateur DANS un `$(…)` /
  backtick n'est pas démasqué (hors scope, `$(…)` hors tier3 depuis mika#946 — décision
  séparée). (3) `grep … | sh` (vrai pipe hors quotes) est False au niveau
  `is_tier3_dangerous` sur HEAD comme après — son refus vient du split segment de
  chain-safety, pas de `is_tier3_dangerous` ; inchangé.

## Références

- Solution : `docs/solutions/security-issues/a-quoted-operator-char-is-not-an-operator-for-tier3-admission.md`.
- Moitié létalité (déjà livrée) : cpp#236 / #238,
  `docs/solutions/security-issues/a-quoted-redirect-char-in-a-commit-trailer-is-not-a-redirection.md`
  (`_mask_lethality_redirect_chars`, dont ceci est l'analogue admission).
- Préserve : mika#946 (`$(…)`/backtick hors tier3), cpp#205 (défaut survivable côté
  létalité), le gate tier1 `is_tier1_auto_approve`, l'axe egress. Leçon ReDoS (tokeniseur
  linéaire, pas de quantificateur imbriqué) : cpp#250. Pattern self-skip e2e si [Security
  Weaken] : cpp#237. Famille : cpp#236. PR : cpp#236 (admission).
