---
ticket: cpp#283
kind: fix
class: hardening (ReDoS — parité de langage reconnu, admission + létalité inchangées ; aucun changement de décision)
status: landed (fix une constante + test de parité old/new + test de temps borné en sous-processus + balayage élargi)
---

# Une boucle `for` aérée fige le classifieur — le séparateur de corps est ambigu — Plan (cpp#283)

## Goal Capsule

`_SANCTIONED_READONLY_FOR_LOOP_RE` (cpp#151 volet A) est consulté par
`_bash_allow_is_chain_safe` dès que la policy rend `allow` (par exemple
`bash-grep`, qui capte le ` grep ` du corps en first-match). Une boucle dont le
corps contient des lignes vides indentées et qui ne correspond PAS à la forme
sanctionnée (ici à cause d'une redirection `> out.txt`) déclenche un
backtracking exponentiel. But : supprimer le blow-up ; **aucune décision ne
change**.

## Cause racine (reproduite à la source, HEAD 1581d74)

```python
_FOR_STMT_SEP = r"(?:[ \t]*(?:;|\r?\n)[ \t]*)+"
_FOR_CMD = rf"{_FOR_CMD_WORD}(?:{_FOR_BLANK}+{_FOR_ARG})*{_FOR_BLANK}*"
```

Deux ambiguïtés empilées :

1. Le groupe répété du séparateur commence ET finit par `[ \t]*`. Un blanc entre
   deux `;`/newlines appartient soit à la queue d'une itération, soit à la tête
   de la suivante : 2^n découpages à essayer sur un échec.
2. `_FOR_CMD` finit par `{_FOR_BLANK}*` juste avant le `[ \t]*` de tête du
   séparateur : les blancs avant le premier `;` ont deux propriétaires.

Mesures à la source (sous-processus, timeout 10 s) :

| entrée | n | HEAD 1581d74 | fix |
|---|---|---|---|
| `_bash_allow_is_chain_safe`, n lignes `"  "` | 8 | 11,3 ms | 0,11 ms |
| | 12 | 921 ms | 0,09 ms |
| | 14 | 7 913 ms | 0,10 ms |
| | 16 | > 10 s | 0,10 ms |
| | 200 | > 10 s | 0,26 ms |
| regex seule, `" ;"` × n + `" ! done"` | 10 | 0,47 ms | 0,01 ms |
| | 20 | 477 ms | 0,01 ms |
| | 24 | 6 897 ms | 0,01 ms |
| | 40 | > 10 s | 0,01 ms |
| | 200 | > 10 s | 0,04 ms |

## Le fix (une constante)

```python
_FOR_STMT_SEP = r"(?:(?:;|\r?\n)[ \t]*)+"
```

Le séparateur perd ses blancs de tête. Chaque blanc a désormais UN seul
propriétaire : avant le premier `;`/newline il appartient au `_FOR_CMD`
précédent ; après un `;`/newline il appartient à la queue de cette itération,
et l'itération suivante commence obligatoirement sur un `;`/newline qu'aucun
blanc ne peut matcher.

**Langage reconnu inchangé.** `_FOR_STMT_SEP` n'est utilisé qu'à deux endroits
(`_FOR_BODY` et la fin `…{_FOR_STMT_SEP}done`), chaque fois immédiatement après
un `_FOR_PIPELINE`, qui finit par `_FOR_CMD`, qui finit par `{_FOR_BLANK}*`.
Comme `[ \t]` ⊂ `_FOR_BLANK`, `{_FOR_BLANK}*[ \t]*` = `{_FOR_BLANK}*`. La
continuation `\`+newline reste un blanc de `_FOR_CMD` (le token nu exclut `\`,
donc un `\` ne peut jamais terminer un argument). Vérifié mécaniquement : le
motif compilé après fix est exactement le motif d'avant où la seule sous-chaîne
`(?:[ \t]*(?:;|\r?\n)[ \t]*)+` est remplacée par `(?:(?:;|\r?\n)[ \t]*)+` (deux
occurrences, 1944 → 1932 caractères).

L'option « garder `[ \t]*` en tête et retirer les blancs de fin de `_FOR_CMD` »
a été écartée : `_FOR_CMD` a besoin de ses blancs de fin pour ` | ` et pour une
continuation `\`+newline avant `;` ; la déplacer touchait trois sites au lieu
d'un.

## Acceptance criteria

- [x] **AC1 — repro à HEAD.** Tableau ci-dessus ; les deux formes de l'issue
  dépassent 10 s.
- [x] **AC2 — temps borné.** `tests/test_for_loop_redos.py` : les deux formes à
  n=200 en < 50 ms. Chaque sonde tourne dans un **sous-processus avec timeout
  dur de 20 s** : une régression rend le test rouge au lieu de figer la suite.
  La sonde vérifie aussi que la policy rend bien `allow` sur la commande, sinon
  une policy vide (fail-closed) rendrait le test vacuement vert. Rouge démontré
  sur l'ancien séparateur (2 failed, « no answer within 20 s (ReDoS) »), vert
  après.
- [x] **AC3 — parité de langage.** Le motif d'avant est figé verbatim dans le
  test ; corpus généré de 32 280 entrées (31 573 distinctes ; 5 139 positifs,
  27 141 négatifs) : lignes vides, `;;`, `; \n` mélangés, blancs avant `done`,
  CRLF, continuations `\`, pipes, arguments quotés, writer/redirect dans le
  corps, `rm`, `$(…)`, backtick, chaînes en queue, préfixe `cd` dans/hors
  worktree. 0 divergence.
- [x] **AC4 — admission + létalité inchangées.** Seule ligne source = la
  constante ; aucun appelant modifié. Tests for-loop cpp#92/cpp#151 inchangés et
  verts. Suite complète verte (1876 passed), ruff/mypy clean, verify-pipeline OK.
- [x] **AC5 — balayage élargi** (voir ci-dessous).

## Balayage (tier1.py + permissions.py, + policy YAML en bonus)

141 motifs collectés par introspection (95 tier1, 18 permissions, 28 règles de
la policy embarquée) + 5 motifs inline. Deux passes :

1. **Fuzz générique** des 141 motifs : 30 préfixes × 46 unités de pompe × 12
   suffixes d'échec à n=24, `search`. Pire cas 0,11 ms. Le même harnais sur
   l'ANCIEN motif for-loop dépasse le timeout (contrôle positif).
2. **Audit structurel** via `re._parser` : (i) quantificateurs imbriqués,
   (ii) groupe répété dont les ensembles de tête et de queue se recoupent,
   (iii) répétitions non bornées adjacentes à ensembles recoupés. 113 candidats
   sur 25 motifs ; pour chacun un préfixe-témoin qui atteint le nœud, la pire
   (pompe, suffixe) à n=12, puis série n=12/16/20/24 (détection exponentielle)
   et n=200/1000/3000 (polynomiale), chaque mesure en sous-processus avec
   timeout. Contrôle positif : l'ancien motif for-loop sort EXP sur 21 de ses 58
   candidats (n=24 ≈ 7 s).

**Résultat : aucun autre motif exponentiel.** Résidus POLYNOMIAUX (quadratiques)
nommés, non corrigés ici (hors périmètre, entrées de plusieurs milliers de
blancs) :

| motif | entrée | n=1000 | n=3000 | n=10000 |
|---|---|---|---|---|
| `policy.bash-mkdir` | `mkdir` + `" -a\t"`×n + `$(` | 97 ms | 853 ms | 9,2 s |
| `tier1._SAFE_SED_PRINT_RE` | `sed -n '1p'` + `"\n "`×n + ` >` | 46 ms | 393 ms | 4,2 s |
| `tier1._EVAL_COMMAND_POSITION_RE` | `sudo` + `"\t\n"`×n + backtick | 25 ms | 207 ms | 2,3 s |
| `policy.bash-for-loop-safe-body` | | 9 ms | 90 ms | |
| `_SANCTIONED_READONLY_FOR_LOOP_RE` (fixé), `find a; `×n | | 4,7 ms | 28 ms | |

Tous les autres candidats < 10 ms à n=3000.

## Fire-Disposition

- **Feu** : gel du classifieur sur toute boucle `for` aérée (lignes vides
  indentées, `; ;`) qui atteint la forme sanctionnée et la manque. **Éteint** :
  0,26 ms à n=200.
- **Résidu (nommé)** : les trois quadratiques du tableau (secondes à 10 k
  blancs) — candidats à un ticket de suivi si l'on veut une borne stricte,
  pas d'exponentielle.
