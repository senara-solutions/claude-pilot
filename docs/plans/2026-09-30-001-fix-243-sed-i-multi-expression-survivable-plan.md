---
ticket: cpp#243 (mika#2482 ; dup cpp#244)
kind: fix
class: default-survivable lethality (admission inchangée)
status: ready-for-qa
---

# `sed -i` multi-expression dans le worktree devient survivable — Plan

Reprend `docs/solutions/security-issues/a-single-substitution-carve-is-defeated-by-a-multi-expression-sed-script.md`. Famille cpp#229 / cpp#236 / cpp#241. GO gate MPC reçu.

## Goal Capsule

cpp#229 (mika#2565) a rendu SURVIVABLE un `sed -i` refusé quand l'édition en place vise un fichier relatif confiné au worktree — mais son extracteur ne reconnaissait qu'UNE substitution et abandonnait (fail-closed, terminal) dès un `-e`. Un script multi-substitution (`s///g` séparés par `;`, ou plusieurs `-e`) retombait sur le motif létal. **7e mort** : pilote `23311c35` (mika#2482), 21:24:39Z, sur `sed -i 's/…/…/g; s/…"feat\/1888\/research"…/…/g; s/…' <fichier worktree>`. **But** : rendre `sed_i_confined_to_worktree` robuste aux scripts multi-expression, **létalité seule**, admission octet-identique, le gate tier1 jamais touché.

## Le fix (LÉTALITÉ SEULE — `tier1.py`)

- **`_sed_i_target_operands`** : la séparation script / fichiers appartient à shlex, pas à un parse du script. Collecte explicite des scripts `-e`/`-eSCRIPT`/`--expression[=]` ; forme nue → premier positionnel = script, le reste = fichiers. `-f`/`--file` (script externe non inspectable) → `None` (fail-closed). Ne parse plus le corps du script pour trouver la fin du script (c'est ce que `\/` et `"…"` défont).
- **`_sed_i_script_all_safe_subs`** (nouveau) : scanner avant. Consomme des unités `_SED_I_SUBST_UNIT_RE` depuis le début ; après chaque unité, le reste doit être vide ou `;` + unité suivante. Le `;` DANS un motif/remplacement n'est jamais atteint (l'unité a déjà consommé jusqu'au séparateur fermant + drapeaux). Chaque script collecté (positionnel ET chaque `-e`) doit passer.
- **`_SED_I_SUBST_UNIT_RE`** : ex-`_SED_I_SAFE_SUBST_SCRIPT_RE` sans l'ancre de fin `\s*$`. Charset de drapeaux inchangé `[gpiImM0-9]` — **pas** de `w`/`W` (write), **pas** de `e` (exec). Un `w FILE` en fin de substitution termine l'unité, le résidu `w …` ne matche plus rien → terminal.
- Confinement par fichier (`_sed_i_target_confined` → `is_within_project`, cwd/fs-aware, symlink-résolvant ; rejette absolu, `..`, `$`, `~`) INCHANGÉ. Re-check du reste façon `rm_confined_to_pilot_scratch` INCHANGÉ.

## Fail-safe

Fail-closed dans toutes les directions : script vide, toute commande non-`s` (`w`/`W`/`r`/`R`/`e`/`d`/`y`/`a`/`i`/`c`), `w FILE` traînant, séparateur inconnu, `;` traînant sans commande, `-f`/`--file`, un seul fichier hors worktree, un verbe dangereux hors quotes → le segment reste TERMINAL. Le carve ne bascule QUE terminal→survivable.

## Hors portée

- Admission : intouchée (`is_tier3_dangerous`, `is_tier1_auto_approve`, YAML) — le `sed -i` reste DENY, le pilote bascule sur l'outil Edit.
- Le gate tier1 : jamais touché.
- `--in-place` long et suffixe de sauvegarde (`-i.bak`) : hors carve, restent terminaux (comportement cpp#229 inchangé).

## Acceptance criteria

- [x] **AC1 — forme verbatim #2482 survivable.** Multi-`;` `s///g` + `\/` + `"…"` sur fichier relatif du worktree → deny NON-terminal.
- [x] **AC2 — variantes multi-expression survivables.** `s/a\/b/c/g; s/d/e/g`, `s/a/b/g;s/c/d/g`, `-e … -e …`, `"…"` intégrés → survivables.
- [x] **AC3 — pas de régression cpp#229.** Substitution unique (avec/sans adresse-range, séparateurs alternatifs) reste survivable.
- [x] **AC4 — écritures `w` restent terminales.** `s/a/b/w /etc/evil`, `s/a/b/g; w /etc/x`, `-e 's/c/d/w /tmp/x'` → terminaux.
- [x] **AC5 — échappées de confinement restent terminales.** absolu, `$HOME`, `~`, `..`, symlink sortant, liste mixte, cwd irrésoluble → terminaux.
- [x] **AC6 — chaîne dangereuse reste terminale.** `;` hors quotes + verbe (`… ; rm -rf x`), `… && git reset --hard`, `-f script.sed` → terminaux.
- [x] **AC7 — admission octet-identique.** `is_tier3_dangerous`/`is_tier1_auto_approve`/YAML inchangés ; end-to-end = `PermissionResultDeny` non-terminal, jamais allow.

## Fire-Disposition

- **Feu** : classe de morts létales sur `sed -i` multi-substitution confiné au worktree (7e mort, mika#2482, mesure du 2026-09-29). **Traité** : `sed_i_confined_to_worktree` robuste aux scripts multi-`;` et multi-`-e` ; extraction des fichiers via shlex, validation intégrale du script via scanner ; écritures/execs et échappées restent terminales.
- **Vérif de sortie** : probe avant/après (verbatim + tous négatifs) ; `pytest`/`ruff`/`mypy`/`verify-pipeline` verts. QA MPC sur le code (GO gate reçu). Redeploy + restart moteur ensuite (hors ce repo).
- **Résidu** : aucun. Formes hors carve (`--in-place` long, `-i.bak`) restaient et restent terminales, fail-closed intentionnel.

## Preuve / vérif (verbatim)

Probe (source `sed_i_confined_to_worktree` + `_denial_is_terminal`) : les 5 positifs multi-expression passent de terminal→survivable ; les 12 négatifs restent terminaux ; cpp#229 mono-substitution sans régression. `uv run pytest` → **1532 passed** ; `uv run ruff check .` → clean ; `uv run mypy src` → clean ; `./scripts/verify-pipeline.sh main-updated` → passed (docs+source).

## Prédicat de déclenchement

Survivable ssi, pour CHAQUE segment `_split_compound_command`, `_sed_i_target_operands` renvoie une liste de fichiers non vide (leading word = `sed` exact, drapeau `-i` court présent, pas de `-f`/`--file`, chaque script `-e`/positionnel = substitutions pures séparées par `;` via `_sed_i_script_all_safe_subs`) ET chaque fichier passe `_sed_i_target_confined` (relatif, non `$`/`~`, `is_within_project`), au moins un segment est ainsi carvé, ET le reste (segments non carvés) n'est plus prouvé-dangereux.

## Références

- Solution : `docs/solutions/security-issues/a-single-substitution-carve-is-defeated-by-a-multi-expression-sed-script.md`.
- Preuve : pilote 23311c35 (mika#2482), 21:24:39Z. Famille : cpp#229 (#229), cpp#236 (#238), cpp#241. Dup : cpp#244.
