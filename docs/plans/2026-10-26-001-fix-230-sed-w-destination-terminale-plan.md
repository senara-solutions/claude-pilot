---
title: "cpp#230 : une écriture sed `w FILE` hors worktree est un refus terminal - Plan"
type: fix
date: 2026-10-26
class: létalité seulement (resserrement) — aucune décision d'admission ne change
artifact_contract: ce-unified-plan/v1
execution: code
product_contract_source: ce-plan-bootstrap
---

# cpp#230 : destination d'écriture sed terminale hors worktree - Plan

## Goal Capsule

- **Objectif :** qu'un pilote qui tente d'écrire hors de son worktree par sed perde sa session, comme il la perd déjà avec `sed -i` hors worktree.
- **Moyen :** extraire lexicalement les destinations d'écriture d'un script sed (commande `w FILE`, commande `W FILE`, drapeau `w FILE` d'un `s///`) et les faire passer par `_destination_veto_reason`, le veto de destination qui juge déjà `cp`, `mv`, `mkdir` et les redirections.
- **Arrêt :** si une décision d'admission bouge, ou si une forme aujourd'hui terminale devient survivable.

## Pourquoi

Rejeu sur `origin/main` @ ec63e2d (2026-10-26), cwd = le worktree :

| commande | `_denial_is_terminal` |
|---|---|
| `sed -n 'w /etc/evil' crates/guards.rs` | False |
| `sed 'w /tmp/x' f` | False |
| `sed -e 's/a/b/w /etc/x' f` | False |
| `sed --expression='s/a/b/w /etc/x' f` | False |
| `sed 'W /etc/x' f` | False |
| `sed -i 's/a/b/' /etc/x` | True |

La commande `w` et le drapeau `w` écrivent dans un fichier arbitraire. Le bac à sable monte `/etc` en lecture seule, mais la politique de permissions ne doit pas reposer sur cette seule couche (défense en profondeur, cpp#218/cpp#220). Trouvé en revue de cpp#229, pas une régression.

Mesuré sur GNU sed 4.10 : `;` fait partie du nom de fichier (`s/a/b/w out; p` crée `out; p`) ; l'espace avant le nom est facultatif (`s/a/b/w1` crée `1`) ; les labels de `:`, `b`, `t`, `T` s'arrêtent au `;` ; `--expr=` est accepté comme préfixe ; `--sandbox` refuse `w` ; sed ouvre (crée, tronque) les fichiers `w` à la compilation, avant toute erreur (`sed -n 'b end;w br;:end'` échoue sur le label et crée `br;:end`).

## Product Contract

- R1. Une destination d'écriture sed (`w FILE`, `W FILE`, `s/…/…/w FILE`, avec ou sans espace avant `FILE`) qui résout hors du worktree ou sur le plan de contrôle (cpp#42) rend `_denial_is_terminal` vrai. Formes de passage du script couvertes : positionnel, `-e S`, `-eS`, `--expression=S`, `--expression S`, préfixes longs non ambigus, options groupées (`-ne S`, `-nE`). Exception déclarée : `-f FILE` (script dans un fichier, contenu inconnaissable) reste survivable.
- R2. Une destination qui résout sous le worktree (`w .pilot-scratch/x`, `s/a/b/w out.txt`) ne rend pas la commande terminale. Contrôle négatif dans le même test.
- R3. Aucune admission ne change. sed est admis sous deux formes étroites (`_is_safe_sed_print_only`, et `_is_safe_sed_pure_substitution` en mode confiné), et `_destination_veto_reason` tourne aussi sur le chemin d'une admission par la politique, avec `interrupt=True` inconditionnel. Le nouveau contrôle ne s'exécute donc que pour la question de létalité (`for_lethality=True`) : l'admission reste identique octet pour octet, par construction.
- R4. Aucune forme terminale aujourd'hui ne devient survivable : la carve `sed_i_confined_to_worktree` reste inchangée.
- R5. Le hint modèle (`DENIED_BASH_PATTERNS_HINT`) dit que l'écriture sed `w` hors worktree termine la session, comme il le dit déjà pour `sed -i`.

## Planning Contract

### Décisions

- KTD1. **Le veto vit dans `_destination_veto_reason`, comme le demande le ticket, derrière `for_lethality`.** Un contrôle sur la commande entière, en tête de la fonction, segment par segment, et non un nouveau write-kind : un segment sed peut aussi porter une redirection (`sed 'w x' f > out`), qui doit continuer d'être classée `bash-redirect` avec ses carves `/tmp`/`/dev/null`. La porte `for_lethality` garantit R3.
- KTD2. **Extraction lexicale dans `tier1.py`** (`sed_write_destinations(segment) -> list[str]`), à côté des autres helpers sed. Deux temps.
  - Marche d'options GNU sed 4.x sur les jetons shlex. Courtes sans argument : `n r E s u z b`. `i` prend le reste du groupe comme suffixe facultatif (`-ie` = suffixe `e`, pas `-i -e` ; même règle que `_sed_inplace_suffix`, cpp#253). `e`, `f`, `l` exigent un argument (reste du groupe, sinon jeton suivant). Longues (préfixe non ambigu accepté) : `--expression`, `--file`, `--line-length` (argument requis) ; `--in-place[=S]` ; `--quiet`, `--silent`, `--regexp-extended`, `--separate`, `--unbuffered`, `--null-data`, `--zero-terminated`, `--binary`, `--posix`, `--debug`, `--sandbox`, `--follow-symlinks`, `--help`, `--version`. `--` termine les options. Un jeton de redirection (`2>/dev/null`, `>out`, `<in`, `&>x`, ou un opérateur seul suivi de sa cible) est sauté, pour que `sed 2>/dev/null 'w /etc/x' f` garde son script.
  - Analyse du script joint par des sauts de ligne (GNU joint plusieurs `-e` ainsi). Grammaire : adresses (nombre, `first~step`, `$`, `/re/`, `\cREc`, drapeaux `I`/`M`, `addr,+N`, `addr,~N`, `0,/re/`, espaces), `!`, `{`/`}`, `#` (commentaire), `:label` / `b` / `t` / `T` (label jusqu'au `;` ou au saut de ligne), `a`/`i`/`c` (texte jusqu'à la fin de ligne, `\` de continuation), `r`/`R`/`e` (argument jusqu'à la fin de ligne), `w`/`W` (blancs facultatifs puis nom jusqu'à la fin de ligne, `;` et `}` compris), `s` (délimiteur quelconque sauf `\` et saut de ligne, échappements, drapeaux `g p e i I m M` et chiffres, drapeau `w` avec nom jusqu'à la fin de ligne), `y`, `v`, `l`/`L`/`q`/`Q` avec entier facultatif, commandes simples `= d D g G h H n N p P x z F`. Linéaire, sans regex à retour arrière (leçon cpp#250).
- KTD3. **Pourquoi ne pas réutiliser la marche de `_sed_i_target_operands`.** Elle échoue fermé sur `-f` pour sa propre carve et ne lit pas les groupes `-ne`. La modifier toucherait la carve de cpp#229/#243/#253 (R4). La nouvelle marche est séparée ; seule la règle du suffixe de `-i` est partagée en réutilisant `_sed_inplace_suffix`.
- KTD4. **Sens d'échec : non prouvé, survivable (doctrine cpp#205, `_denial_is_terminal`).** Option inconnue : GNU refuse de démarrer, aucune destination. Script dans un fichier (`-f`) : inconnaissable, aucune destination, résidu déclaré. `--sandbox` : GNU rejette `w`, aucune destination. Script inanalysable : aucune destination, résidu déclaré ; la commande reste refusée dans tous les cas. Script dont le mot shell porte une citation ANSI-C (`$'\x77 /etc/x'`) ou une expansion (`"$C /etc/x"`) : shlex ne voit pas ce que bash passe à sed, résidu déclaré, épinglé par un test survivable.
- KTD5. **Destinations exemptées :** `/dev/stdout` et `/dev/stderr` (fichiers spéciaux de GNU sed, idiome courant), `/dev/null` (puits inerte, cpp#130), nom vide (GNU refuse de compiler). **Destinations vetoées avant résolution :** racine `$` ou `~` (anti-réécriture cpp#154 D3 / cpp#211, `is_within_project` ne développe rien). Ensuite `is_within_project`, puis `_is_control_plane_path`, dans l'ordre du veto existant. **`/tmp/…` est terminal, choix délibéré :** le ticket vise « hors worktree » et le constat liste `sed 'w /tmp/x'` ; c'est la règle de `sed -i`. La redirection `> /tmp/x` est survivable depuis cpp#154 parce que des pilotes mouraient sur cet idiome de brouillon ; rien de tel n'est mesuré pour `sed … w /tmp/…`. Le rejeu du corpus le vérifie : s'il trouve des écritures `w /tmp` réelles, l'asymétrie est signalée dans le rapport.
- KTD6. **Corps de heredoc.** `_split_compound_command` coupe sur les sauts de ligne, donc une ligne de corps de heredoc (`cat > doc.md <<'EOF'` puis `sed 'w /etc/x' f`) se lirait comme une commande sed. Bash ne l'exécute jamais (le corps est une donnée, cité ou non). Avant l'extraction, les lignes de corps de chaque heredoc sont blanchies (lecture des ouvreurs par `_scan_command_line_for_heredocs`, cpp#241) ; un heredoc non terminé est un corps jusqu'à la fin, comme pour bash. Le pilote qui livre ce ticket citera ces lignes dans son message de commit.
- KTD7. **Commande en tête :** le mot `sed`, ou un chemin dont le nom de base est `sed` (`/usr/bin/sed`). Un préfixe `env`/`VAR=`, `xargs sed`, `(sed …)` ou `{ sed …; }` n'est pas lu : résidu déclaré, comportement inchangé.

### Hors périmètre

- La commande `e` et le drapeau `e` de `s///` (exécution de commande) : autre classe, autre ticket.
- `awk` et ses écritures `print > "f"` : autre interpréteur.

### Considéré, non construit

- Un reconnaisseur plus grossier (chercher `w` après un délimiteur) : la revue adversariale a montré que les labels coupés au `;`, le nom de fichier jusqu'à la fin de ligne et les regex d'adresse sont chacun nécessaires pour éviter un faux négatif ou un faux positif. La grammaire complète est le minimum correct.

### Assumptions

- Le contrôle de plan de contrôle (cpp#42) s'applique aux destinations sed comme aux autres : `w .git/hooks/x` devient terminal.

## Implementation Units

### U1. Tests d'abord (commit seul, rouge sur main)

Fichier : `tests/test_permissions.py`, un test paramétré par forme.

- Terminal : `sed -n 'w /etc/evil' crates/guards.rs`, `sed 'w /tmp/x' f`, `sed -e 's/a/b/w /etc/x' f`, `sed --expression='s/a/b/w /etc/x' f`, `sed --expression 'w /etc/x' f`, `sed --expr='w /etc/x' f`, `sed -ne 'w /etc/x' f`, `sed -nE 'w /etc/x' f`, `sed -E 's/a/b/w /etc/x' f`, `sed -z 'w /etc/x' f`, `sed -l 5 'w /etc/x' f`, `sed 'W /etc/x' f`, `sed 's/a/b/w /etc/x' f`, `sed 'w/etc/x' f`, `sed 's/a/b/gw/etc/x' f`, `sed '1p;w /etc/x' f`, `sed ':a;w /etc/x' f`, `sed 'b e;w /etc/x' f`, `sed -e '/re/{w /etc/x' -e '}' f`, `sed 's|a|b|gw /etc/x' f`, `sed 'w ../../x' f`, `sed "w $HOME/x" f`, `sed 'w ~/x' f`, `/usr/bin/sed 'w /etc/x' f`, `echo a && sed 'w /etc/x' f`, `sed 2>/dev/null 'w /etc/x' f`, `sed -ie 's/a/b/w /etc/x' f`, `sed 'w .git/hooks/pre-commit' f`.
- Survivable dans le même test : `sed -n 'w .pilot-scratch/x' f`, `sed 's/a/b/w out.txt' f`, `sed -n 'w /dev/stdout' f`, `sed -n '/warning/p' f`, `sed -n '/\w/p' f`, `sed 's/w/W/g' f`, `sed -n '1,5p' f`, `sed 'a w /etc/x' f`, `sed 's/a/w \/etc\/x/' f`, `sed -f script.sed f`, `sed --sandbox 'w /etc/x' f`, `sed $'\x77 /etc/x' f` (résidu épinglé), `cat > docs/x.md <<'EOF'` + ligne `sed 'w /etc/x' f` + `EOF`, `git commit -F - <<'EOF'` + `fix` + ligne vide + `sed 'w /etc/x' f` + `EOF`.
- Témoins existants : `sed -i 's/a/b/' /etc/x` reste terminal, `sed -i 's/a/b/' src/x.rs` reste survivable.
- Témoin d'admission : `_destination_veto_reason(…, for_lethality=False)` rend `None` pour `sed -n '/warn/p' f`, `grep -n x f; sed -n '/warn/p' f` et `sed 'w /etc/x' f` (le contrôle ne touche pas la question du refus).

Fichier : `tests/test_tier1.py` — tests unitaires de `sed_write_destinations` (formes ci-dessus, groupage, `-l 5`, préfixes longs, `--sandbox`, `-f`, option inconnue, script inanalysable avec et sans `w` → `[]`), et un test de bornage temporel sur un script pathologique (2 000 `\` et `/`).

### U2. Extraction lexicale

Fichier : `src/claude_pilot/tier1.py` — `sed_write_destinations(segment) -> list[str]`, la marche d'options et l'analyseur de script (KTD2, KTD4), et le blanchiment des corps de heredoc (KTD6).

### U3. Veto

Fichier : `src/claude_pilot/permissions.py` — en tête de `_destination_veto_reason`, sous `for_lethality`, `_sed_write_veto_reason(command, cwd)` (KTD1, KTD5, KTD6). Mise à jour du bloc doctrinal au-dessus de `_denial_is_terminal` (point 2 de l'ensemble de danger prouvé).

### U4. Hint

Fichier : `src/claude_pilot/tier1.py` — `_SED_INPLACE_BULLET` nomme aussi l'écriture `w FILE` hors worktree. Test : la puce rendue la contient.

## Verification Contract

- Rouge-avant, une forme sed à la fois : le test U1 lancé sur `origin/main` échoue sur chaque forme terminale (paramétrisé, un échec par forme), passe sur la branche.
- Mutations : neutraliser l'appel du veto (toutes les formes terminales rougissent), retirer le drapeau `w` de `s` dans l'analyseur (seules les formes `s///w` rougissent), retirer la commande `W` (seule `W` rougit), retirer le blanchiment des heredocs (les deux témoins heredoc rougissent).
- Rejeu sur le corpus des transcripts pilotes (`~/.mika/data/pilot-transcripts/`) : toutes les commandes Bash contenant `sed`, verdict de létalité et décision `_destination_veto_reason(for_lethality=False)` avant/après. Seules des lignes à écriture `w` réelle hors worktree peuvent passer de False à True (chaque bascule vérifiée à la main comme un vrai segment sed, pas une ligne de corps), aucune de True à False, aucune décision de refus ne bouge.
- `uv run pytest`, `uv run ruff check`, `uv run mypy src`, `scripts/verify-pipeline.sh origin/main`.

## Definition of Done

Les cinq formes du constat rendent True, `w .pilot-scratch/x` rend False dans le même test, l'admission est inchangée, suite complète verte, lint et typage propres, commit local sans push.
