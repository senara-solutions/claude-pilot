---
ticket: cpp#278 / mika#2634
kind: feat
class: prompt-layer (le hint système nomme les formes TERMINALES + le substitut « écris un test, pas une sonde shell ») — AUCUN changement d'admission ni de létalité
status: landed (registre dérivé + builder du bloc + composition du hint + test garde-dérive ; admission + létalité byte-identiques sur échantillon large)
---

# Le hint des patterns refusés nomme les formes TERMINALES et le substitut « écris un test, pas une sonde shell » — Plan (cpp#278)

## Goal Capsule

Trois pilotes tués en 24 h (`93bac846`, `c722b251`, `debf318f`) par une **sonde
shell** ad hoc : chacun voulait **observer un comportement shell/git** (une capture
de stderr, un état git, une redirection) et l'a fait par un script jetable au lieu
d'un test. Le hint système (`DENIED_BASH_PATTERNS_HINT`, `tier1.py`, ajouté au prompt
par `agent.py:1315`) nommait déjà les patterns refusés, mais ne disait PAS lesquels
**tuent la session** (refusé ET fin de run, sans retry ni recovery), ni le substitut.
Les consignes posées dans chaque ticket après chaque mort ne passent pas à l'échelle.

But : **mesure de prompt pure — admission ET létalité byte-identiques.** On ne touche
NI `is_tier1_auto_approve`, NI `is_tier3_dangerous`, NI `TIER3_PATTERNS`, NI
`is_tier3_dangerous_for_lethality`, NI `_denial_is_terminal`, NI le YAML, NI l'egress.
Seuls changent : la chaîne du hint, sa dérivation/son test, et un petit registre
constant co-localisé avec les listes.

## Cause racine (vérifiée à la source, servi `1e8e28b`)

Table de rejeu du ticket (vrai worktree) : `bash -c 'exit 1'` et `sh -c 'exit 1'`
sont **terminaux** (tier3 par conception) ; `mkdir -p .pilot-scratch/x && cd …` est
survivable. Le refus et la létalité de `bash -c` sont **voulus**. Le trou n'est pas
dans le classifieur — il est dans ce que le modèle **sait** avant d'agir : le hint ne
distinguait pas « refusé mais récupérable » de « refusé ET mort ».

## Le fix

### AC1 — le hint nomme les formes TERMINALES, une ligne « refusé ET fin de session »

Un nouveau bloc EN TÊTE du hint (les formes terminales sont la panne la plus coûteuse,
donc en premier), rendu depuis `_TERMINAL_FORM_REGISTRY` :

```
- `bash -c` / `sh -c` (running a script through a shell) — refused AND the session ENDS.
  `sh -c` shares this fate; `xargs sh -c …` / `find … -exec sh -c …` do too.
- `eval` (evaluating a constructed string at command position) — refused AND the session ENDS.
- `sed -i` whose in-place target is outside this worktree — refused AND the session ENDS.
- a shell redirect (`>`, `>>`) whose target is outside this worktree — refused AND the session ENDS.
- `rm -rf` on an unresolved or uncontained target — refused AND the session ENDS.
```

### AC2 — le hint donne le substitut

Paragraphe unique après le bloc : pour OBSERVER un comportement shell/git, **écrire un
test** dans le harnais du dépôt (durable, re-exécutable), OU lancer **une commande simple
par appel** avec des chemins relatifs LITTÉRAUX sous `.pilot-scratch/` — **sans variable,
sans `;`/`&&`, sans sous-shell**. (Les carves cpp#272/#279 admettent désormais les
littéraux `.pilot-scratch/<subpath>`, donc le substitut est réellement disponible.)

Ligne de garde ajoutée (condition du gate MPC sur cpp#278, dernière ligne du bloc
substitut) : « Never `pip install` on the host (it clobbers the shared launcher); to
test, use `uv run` in a clone or a throwaway venv. » — un `pip install` hôte réécrit le
lanceur `[console_scripts]` partagé (`~/.local/bin/claude-pilot`) et casse le launcher de
prod. Texte de hint pur : advisory, PAS une entrée de `_TERMINAL_FORM_REGISTRY` (ce n'est
pas un classifieur de forme terminale), donc sans effet sur admission/létalité ni sur le
test garde-dérive centré-registre.

### AC3 — dérivé de `tier1.py`, garde-dérive

`_TERMINAL_FORM_REGISTRY` (co-localisé avec les tuples de létalité, `tier1.py`) :
un tuple de `(bullet, pattern, is_verb_lethal)` où `pattern` est l'OBJET regex EXACT
que le classifieur matche (les entrées `bash -c`/`sh -c`/`sed -i`/`rm -rf` de
`TIER3_PATTERNS` hoistées en constantes nommées — `_BASH_C_PATTERN`, etc. — plus
`_EVAL_COMMAND_POSITION_RE` déjà nommée, plus `_GENERIC_REDIRECT_PATTERN` pour le
redirect). `_render_terminal_forms_block()` CONSTRUIT le bloc depuis le registre ;
`DENIED_BASH_PATTERNS_HINT` = ce bloc + le corps existant. Le hint n'est donc pas un
miroir copié-main : il est dérivé des mêmes objets regex que l'enforcement.

Test `test_terminal_forms_hint_derived_from_tier1` (`tests/test_tier1.py`) : pour chaque
entrée du registre, (a) son bullet est présent dans le hint rendu ; (b) si
`is_verb_lethal`, son pattern est dans l'ensemble de létalité
(`_TIER3_VERB_PATTERNS_FOR_LETHALITY` ∪ `_PROVEN_DANGEROUS_VERB_PATTERNS_CPP205` ∪
`_SED_INPLACE_LONG_FORM_FOR_LETHALITY`) ; sinon c'est la SEULE entrée exclue du verb-set
(le redirect générique, `TIER3_PATTERNS[-1]`, létal via le véto de destination cwd-aware).
Deux tests compagnons : le registre nomme bien les 5 formes AC1 ; chaque pattern
verb-létal, nourri d'une commande représentative, est prouvé terminal par le vrai
prédicat `_matches_proven_dangerous_lethality_verb`.

**Rouge vérifié par toggle.** (1) une entrée ajoutée au registre dont le bullet n'est pas
rendu dans le hint → `assert bullet in hint` échoue ; (2) un pattern d'une entrée
quittant l'ensemble de létalité → `assert pattern in verb_set` échoue. Les deux
reproduits en vert/rouge avant commit.

### AC4 — aucun changement d'admission ni de létalité

Hoister les 5 patterns en constantes nommées laisse `TIER3_PATTERNS` avec les MÊMES
objets compilés dans le MÊME ordre → `is_tier3_dangerous` byte-identique ;
`_TIER3_VERB_PATTERNS_FOR_LETHALITY = TIER3_PATTERNS[:-1]` inchangé → létalité identique.
Preuve : digest sha256 sur 107 lignes (`is_tier1_auto_approve` / `is_tier3_dangerous` /
`is_tier3_dangerous_for_lethality` / `_denial_is_terminal` sur un corpus large : tous les
verbes tier3, carves pilot-scratch/mktemp, redirects in/out-worktree, cpp205, find/xargs,
read-only, compounds, outils non-Bash) — **identique avant et après** (`c86045d8…`).

## Invariants

PROMPT-ONLY. tier1 gate + classifieur tier3 + chemin létalité + YAML + egress INTOUCHÉS.
Le registre et le builder ne sont consultés QUE pour composer une chaîne de prompt ;
aucun chemin de décision de permission ne les atteint. Le câblage `agent.py` est inchangé
(`"append": DENIED_BASH_PATTERNS_HINT`, même constante). Limite assumée (du ticket) :
c'est une mesure de prompt, fragile par nature ; elle COMPLÈTE les carves structurelles
cpp#268/#272/#279, elle ne les remplace pas.

## Fire-Disposition

- **Feu** : trois pilotes morts TERMINAL en 24 h par une sonde shell ad hoc
  (`93bac846` mika#2626 groom, `c722b251` mika#2623 implement, `debf318f` mika#2634 groom) —
  chacun voulait observer un comportement et a scripté au lieu de tester ; le hint ne
  nommait ni les formes qui tuent ni le substitut.
- **Éteint** : le hint, en tête, nomme les 5 formes terminales (une ligne « refusé ET fin
  de session » chacune) et donne le substitut (« écris un test ; ou une commande simple
  par appel, chemins littéraux sous `.pilot-scratch/` »). Dit une fois, au seul endroit
  que tous les pilotes lisent. Une ligne de garde finale ajoute : jamais de `pip install`
  hôte (il clobbe le lanceur partagé) — tester via `uv run` dans un clone ou un venv jetable
  (condition du gate MPC ; texte advisory, admission/létalité inchangées).
- **Vérif de sortie** : registre + builder + composition (`tier1.py`), test garde-dérive
  + 3 compagnons (`tests/test_tier1.py`) ; pytest 1730 vert (0 skip) ; ruff/mypy clean ;
  verify-pipeline GREEN ; digest admission+létalité IDENTIQUE avant/après (107 lignes,
  `c86045d8…`) ; rouge de la garde-dérive démontré par deux toggles. Édition appliquée
  directement — aucun blocage.
- **Résidu (nommé)** : (1) mesure de prompt — stochastique par construction ; la défense
  structurelle reste les carves de létalité (cpp#268/#272/#279). (2) La garde-dérive est
  centrée-registre : elle force la mise à jour du hint quand une forme terminale enregistrée
  dérive ou disparaît du hint ; un NOUVEAU verbe létal ajouté à `tier1.py` SANS entrée de
  registre n'est pas capté (le registre est le sous-ensemble curé que le hint expose — par
  conception, le hint reste concis). (3) Le hint nomme un sous-ensemble curé des ~20 verbes
  létaux (les formes « sonde », pas `git push --force`/`DROP TABLE`/`dd`/…) : délibéré, pour
  ne pas gonfler un bloc prépendu à chaque prompt.

## Références

- Solution : `docs/solutions/tooling-decisions/the-denied-patterns-hint-names-terminal-forms-and-the-write-a-test-substitute.md`.
- Siblings : cpp#59/mika#1409 (hint fondateur + section no-op headless), cpp#128 (défaut
  adaptable), cpp#205 (défaut survivable + verb-set létal), cpp#268/#272/#279 (carves
  structurelles de létalité — défense primaire), cpp#274 (sed long-form létalité).
- Code : `src/claude_pilot/tier1.py`. Câblage inchangé : `src/claude_pilot/agent.py:1315`.
  PR : cpp#278.
