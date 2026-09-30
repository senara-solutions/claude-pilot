---
ticket: cpp#250 / mika#2601
kind: fix
class: hardening (ReDoS — parité de décision, admission + létalité byte-identiques ; aucun changement de décision)
status: landed (fix + test de temps borné + tests de parité + doc + audit du balayage ; édition regex NON bloquée)
---

# Un quantificateur imbriqué dans la whitelist sed-print est un ReDoS — Plan (cpp#250)

## Goal Capsule

Pilote 01c5c3ef (mika#2601), 2026-09-30 : bloqué en état R depuis 11:21:06Z,
**41 min de CPU** sur le thread principal, ne rendra jamais la main. Dernière
ligne de log :

```
[tool:request] Bash: sed -n '1139,1216p' crates/mika-agent/src/task_engine/mod.rs > .pilot-scratch/probe-block.txt; wc -l .pilot-scratch/probe-block.txt
```

`faulthandler` localise la boucle :

```
tier1.py:2469 _is_safe_sed_print_only
tier1.py:1342 _blank_print_only_sed_segments
tier1.py:1440 is_tier3_dangerous_for_lethality
permissions.py:895 _denial_is_terminal
```

**ReDoS** dans `_SAFE_SED_PRINT_RE` (`tier1.py:2457`, introduit par cpp#190).
But : supprimer le blow-up ; **aucune décision ne change** (admission ET
létalité byte-identiques) — c'est un durcissement de performance pur.

## Cause racine (reproduite à la source, HEAD bd7bf6a)

```python
rf"^\s*sed\s+-n\s+'{_SED_ADDR}(?:,{_SED_ADDR})?p'\s*(?:[A-Za-z0-9_./-]+\s*)*$"
#                                                    ^^^^^^^^^^^^^^^^^^^^^^^^^
```

Le groupe d'opérandes `(?:[A-Za-z0-9_./-]+\s*)*` est un quantificateur imbriqué
(`X+` dans `(...)*`) où `\s*` peut matcher le vide. Quand un chemin est suivi
d'un caractère HORS de la classe `[A-Za-z0-9_./-]` (le `>` d'une redirection,
un `|`, un `$`), la queue ne peut plus matcher, et le moteur re-essaie CHAQUE
découpe du chemin en sous-jetons `[A-Za-z0-9_./-]+`, chacun avec un `\s*` vide
intercalé. Le temps est exponentiel en la longueur du chemin.

Mesure à la source (`_is_safe_sed_print_only`, chemin de N chars + ` > g`) :

| N (chars du chemin) | temps HEAD |
|---|---|
| 16 | 3,2 ms |
| 20 | 53,8 ms |
| 22 | 199,0 ms |
| 24 | 797,6 ms |
| 26 | 2 970 ms |
| 28 | 11 676 ms |
| ~40 (verbatim pilote) | > 20 s (hang) |

Croissance ~×4 tous les +2 chars. Le chemin du pilote fait ~40 chars → gel.

## Le fix (une ligne, ratifié MPC, parité vérifiée sur 11 cas)

Ancrer CHAQUE répétition d'opérande sur un `\s+` de tête :

```python
rf"^\s*sed\s+-n\s+'{_SED_ADDR}(?:,{_SED_ADDR})?p'\s*(?:\s+[A-Za-z0-9_./-]+)*\s*$"
#                                                    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
```

Chaque opérande doit maintenant commencer par `\s+` : l'ambiguïté (où finit un
opérande, où commence le suivant) disparaît, un run non-blanc ne peut plus se
découper de plusieurs façons. Le matching devient linéaire. Après fix, le
verbatim pilote rend en ~2,4 µs, un chemin de 200 chars + `>` en ~7,7 µs.

C'est le SEUL changement de source. Aucune fonction en aval n'est modifiée :
la parité de décision est structurelle.

## Balayage (Attendu #3 de l'issue — audité, une seule vulnérabilité)

Grep de tout motif `tier1.py` / `permissions.py` de forme `(?:X+\s*)*` /
`(X*)*` :

- `_SAFE_SED_PRINT_RE` (tier1.py:2457) — **VULNÉRABLE, corrigé ici.** Classe
  RESTREINTE `[A-Za-z0-9_./-]` → un `>` hors-classe crée une frontière d'échec
  qui déclenche le backtracking catastrophique.
- `_SAFE_SED_SUB_RES` (tier1.py:2383-2387), `\s*(?:\S+\s*)*$` — **PAS
  vulnérable.** La classe `\S` est NON restreinte : tout caractère de queue
  (`>`, `|`, `$`) est absorbé par `\S+`, il n'y a jamais de frontière d'échec de
  classe à backtracker. Stress vérifié à la source : opérande de 100 000 chars
  avec queue `>`/`|` → < 0,5 ms, linéaire (cas « quote non fermée » = 7 ms à
  100k, linéaire, pas exponentiel).

Conclusion du balayage : `_SAFE_SED_PRINT_RE` était le seul motif vulnérable de
cette classe. La classe de caractères RESTREINTE est ce qui distingue le motif
dangereux du motif sûr.

## Acceptance criteria

- [x] **AC1 — ReDoS reproduit à la source (HEAD bd7bf6a).** Le verbatim pilote
  fige `_is_safe_sed_print_only` > 20 s ; croissance ×4/+2chars mesurée
  (tableau ci-dessus). Prouvé machine.
- [x] **AC2 — linéarité après fix.** Le MÊME verbatim + un chemin de 200 chars
  suivi de `>` rendent en < 50 ms (mesuré : µs) via `_is_safe_sed_print_only`,
  `is_tier3_dangerous`, `is_tier3_dangerous_for_lethality`, et
  `permissions._denial_is_terminal`. Test de non-régression de temps borné
  ajouté (`tests/test_tier1.py`, budget 50 ms, `time.perf_counter`).
- [x] **AC3 — parité de décision (byte-identique).** Sur les 11 cas MPC
  (positifs : 1 ou plusieurs fichiers, `$p`, `/re/,/re/p`, sans fichier,
  espaces de queue ; négatifs : redirection `>`, second sed, métacaractère
  shell, mauvais flag `-i`) HEAD-regex et fix-regex renvoient le MÊME bool.
  Fuzz de 21 263 entrées générées : 0 divergence. Parité pinnée en test.
- [x] **AC4 — admission + létalité inchangées.** Seule ligne source touchée =
  la regex ; aucun appelant modifié → `is_tier3_dangerous`,
  `is_tier3_dangerous_for_lethality`, `_denial_is_terminal`,
  `is_tier1_auto_approve` byte-identiques par construction. Tier1 gate JAMAIS
  touché, axe egress JAMAIS touché. Suite complète verte (1559 passed), les
  tests sed-print cpp#189/#190 inchangés. ruff/mypy clean. verify-pipeline OK.
- [x] **AC5 — balayage.** Toute autre regex `(?:X+\s*)*` / `(X*)*` auditée ;
  seul `_SAFE_SED_PRINT_RE` vulnérable (voir section Balayage).

## Fire-Disposition

- **Feu** : gel à vie du classifieur de permissions (41 min CPU, thread
  principal, jamais rendu) sur toute commande atteignant `_SAFE_SED_PRINT_RE`
  avec un `sed -n '<range>p'` + chemin long + caractère hors-classe (une
  redirection `>`, un pipe `|`). Classe fréquente : un pilote lit un extrait de
  fichier et redirige la sortie. mika#2601 (01c5c3ef) est le tueur observé.
  **Éteint** : le matching devient linéaire ; l'input du pilote rend en µs.
- **Vérif de sortie** : repro machine du hang à HEAD (tableau exponentiel +
  hang > 20 s) ; après fix < 50 ms sur les 4 axes ; parité 11 cas + fuzz 21 263
  entrées à 0 divergence ; suite complète verte ; ruff/mypy clean ;
  verify-pipeline OK.
- **Résidu (nommé)** : (1) c'est un durcissement de perf, PAS un changement
  d'admission ni de létalité — aucune décision ne bouge, donc pas de Vincent
  requis (l'édition regex n'a pas été bloquée). (2) `_SAFE_SED_SUB_RES` partage
  la forme `(?:\S+\s*)*` mais est prouvé sûr (classe non restreinte) ; laissé
  tel quel — le réécrire serait un changement sans gain, hors scope. (3) Rien
  au-delà de cpp#250.

## Références

- Solution : `docs/solutions/security-issues/a-nested-quantifier-in-the-sed-print-whitelist-is-a-redos.md`.
- Origine du motif : cpp#189/#190 (`_is_safe_sed_print_only`, admission
  read-only sed-print). Chaîne d'appel du gel : cpp#205
  (`is_tier3_dangerous_for_lethality`), cpp#128/#205 (`_denial_is_terminal`).
- Issue : cpp#250. Pilote : mika#2601 (01c5c3ef).
