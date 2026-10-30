---
title: "fix(permissions): l'ancre eval couvre {, if/while/until, builtin, \\eval et un préfixe d'affectation (cpp#240)"
type: fix
status: active
date: 2026-10-30
issue: senara-solutions/claude-pilot#240
---

# L'ancre « eval en position commande » couvre les sept formes manquantes

## Pourquoi

cpp#239 a remplacé `\beval\s` par `_EVAL_COMMAND_POSITION_RE`, et admission comme létalité
lisent cette définition unique. Sept formes que bash exécute bien comme un `eval` en position
commande y échappent pourtant. Elles passent de `is_tier3_dangerous=True` (sur main avant #239)
à `False` :

- `{ eval "$CMD"; }`
- `if eval "$CMD"; then :; fi`
- `while eval "$CMD"; do :; done`
- `until eval "$CMD"; do :; done`
- `builtin eval "$CMD"`
- `\eval "$CMD"`
- `x=1 eval "$CMD"`

Aujourd'hui, aucune décision ne change : `policy.evaluate` refuse ces formes, et
`_bash_allow_is_chain_safe` est faux. Ce qui manque, c'est le veto qui s'applique après une
règle `allow`. Une règle future qui accepterait l'un de ces préfixes ne serait plus rattrapée.

## Axe

Le diff durcit et n'admet rien de plus. Il rend tier3 et terminales des formes qui l'étaient
sur main avant #239. Il ne touche aucune forme `eval` en argument.

## Décisions

1. **Mots-clés ouvrants.** `if|while|until` rejoignent `then|do|else|elif`, et `{` rejoint `!`,
   toujours suivis d'un blanc. `{` n'entre pas dans la classe de séparateurs
   `[|&;\n(`]` : bash n'ouvre un groupe que sur `{` suivi d'un blanc, et le prendre seul
   ferait de `${eval}` ou `x{eval y` une position commande.
2. **Préfixes.** `builtin` rejoint la chaîne `sudo|env|exec|command|nohup|time|xargs`.
3. **Affectation.** Un préfixe `NAME=val`, répétable et mêlé aux autres préfixes.
   - `NAME` suit `[A-Za-z_][A-Za-z0-9_]*`.
   - `val` est une suite de caractères non blancs ou de segments entre guillemets simples
     ou doubles, pour que `x="a b" eval` soit reconnu.
   - Chaque alternative du `val` consomme un caractère distinct d'un guillemet, ou un
     segment entre guillemets : aucune ambiguïté, donc pas de retour arrière exponentiel.
4. **`\eval`.** Un `\` optionnel devant `eval`.

L'ancre reste un objet unique, partagé par `TIER3_PATTERNS` et
`_TIER3_VERB_PATTERNS_FOR_LETHALITY`. Le test qui le vérifie (`is`) ne change pas.

## Tests

- Les sept formes donnent `is_tier3_dangerous=True`, `is_tier3_dangerous_for_lethality=True`
  et `_denial_is_terminal=True`.
- **Non-régression des positifs de #235.** `cargo test --test eval x`, `node --eval "x"` et
  `cargo test --test=eval x` restent admis et survivables. S'y ajoutent les voisins que les
  nouvelles branches pourraient happer :
  - `cargo test --test eval x` derrière `if`, comme dans `if cargo test --test eval x; then :; fi` ;
  - `FOO=1 cargo test --test eval x` ;
  - `echo ${eval}`.
- **Temps borné.** Une affectation longue sans `eval`, ou avec un guillemet non fermé, est
  évaluée sous le budget de 50 ms de cpp#250.

## Preuve

`uv run pytest`, `uv run ruff check` et `uv run mypy src` sont verts. Les sept formes sont
rouges sur main avant le correctif : les tests nouveaux échouent sans lui.

## Hors périmètre

- `case … in`, `function`, `coproc`, `exec -a`.
- L'évaluateur bashlex `per_spawn` (mika#1708).
