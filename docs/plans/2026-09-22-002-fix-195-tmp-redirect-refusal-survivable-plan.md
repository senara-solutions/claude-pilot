---
issue: claude-pilot#195
title: git show > /tmp/x refusé TERMINAL malgré cpp#154 — rendre le refus SURVIVABLE - Plan
type: fix
scope_repo: claude-pilot
priority: p1-important
date: 2026-09-22
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# git show > /tmp/x refusé TERMINAL malgré cpp#154 — rendre le refus SURVIVABLE - Plan

## Goal Capsule

**Objectif.** La commande de halt mika#2471 (rejeu MPC contre le classifier de
`main`, transcript `117d7a20`) est `git show origin/main:crates/mika-common/
src/home.rs > /tmp/ck_home_main.rs`. Elle est refusée (correct — écriture
hors worktree) mais TERMINALE — elle tue la session — alors que cpp#154 avait
déjà rendu ce type de redirect non létal pour la classe générique. Ce plan
établit la cause précise (elle diffère de l'hypothèse initiale du ticket sur
UN point de mécanisme, confirmé par sonde), pose la décision (a)/(b), et
répare — sans toucher au refus, seulement à sa létalité.

## Cause établie à la source (HEAD 4bc6105, sondée dans ce worktree)

`_denial_is_terminal` (`permissions.py:767`) est :

```python
if is_tier3_dangerous_for_lethality(command):
    return True
if _redirect_destination_veto_reason(command, cwd) is not None:
    return True
return _destination_veto_reason(command, cwd) is not None
```

Sonde (`_is_tier3_dangerous_for_lethality`, `_redirect_destination_veto_reason`,
`_destination_veto_reason`, `_denial_is_terminal`, `_segment_write_kind`) sur
la commande exacte de mika#2471 et ses contrôles, `cwd` = worktree temporaire :

| commande | t3_lethality | redirect_veto (whole-cmd) | dest_veto (per-write-kind) | **TERMINAL** | write-kind |
|---|---|---|---|---|---|
| `git show origin/main:home.rs > /tmp/ck_home_main.rs` (2471) | False (cpp#154) | **None** | **non-None** ← le bug | **True** | `bash-git-show-redirect` |
| `git show origin/main:x > /etc/ck.rs` | True | None | non-None | True (correct) | `bash-git-show-redirect` |
| `git show origin/main:x > /var/outside/ck.rs` | True | None | non-None | True (correct) | `bash-git-show-redirect` |
| `git show origin/main:x > ../escape.rs` | True | None | non-None | True (correct) | `bash-git-show-redirect` |
| `sed -i 's/a/b/' f` | True | None | None (kind=None) | True (correct, via tier3) | — |
| `rm -rf x` | True | None | None (kind=None) | True (correct, via tier3) | — |
| `echo hi > /tmp/scratch.txt` (control, non git-show) | False | None | **None** (already fixed) | **False** | `bash-redirect` |

**Correction assumée par rapport à l'hypothèse du ticket/dispatch.** Le
ticket cpp#195 et le message de dispatch supposaient que le veto TERMINAL
venait de `_redirect_destination_veto_reason` (le fallback whole-command que
cpp#154/#155/#176 documentent). La sonde le contredit : cette fonction rend
déjà `None` pour la commande #2471 — elle a bien le carve-out `/tmp/`. Le
vrai coupable est **`_destination_veto_reason`** (la fonction PER-WRITE-KIND,
`permissions.py:1088`), qui a deux branches spécifiques — `bash-mkdir`
(cpp#143) et `bash-redirect` (cpp#154/#155, avec le carve-out `/tmp/`) — mais
**aucune** pour `bash-git-show-redirect` (cpp#35/#128, antérieure à la
généralisation cpp#155). Un dest `bash-git-show-redirect` tombe directement
sur le check générique `is_within_project(dest, cwd)` quelques lignes plus
bas, qui rend `False` pour tout chemin hors worktree — y compris `/tmp/…` —
et vétote : `"destination '/tmp/ck_home_main.rs' resolves outside the
worktree (cpp#38 symlink-traversal containment)"`.

Le chemin d'exécution réel emprunté par la commande #2471 a aussi été vérifié
(pas supposé) : `policy.evaluate` la classe `allow, rule_id=bash-git-readonly`
(le pattern `bash-git-show-redirect`, plus strict, exige une cible RELATIVE —
`(?!/)` — et ne matche donc jamais une cible absolue `/tmp/…`; `evaluate` est
first-match-wins et retombe sur `bash-git-readonly`, un pattern non ancré qui
matche n'importe quel `git show …` quelle que soit la suite). `_bash_allow_is_
chain_safe` rend `False` pour cette commande (le motif `>` générique de
`is_tier3_dangerous` matche toujours sur ce chemin — cpp#154 ne touche QUE le
classificateur de létalité, jamais le classificateur de refus). La commande
passe donc par le site du **veto de chaîne** (`create_permission_handler`,
`permissions.py:1493-1520`), qui appelle `_denial_is_terminal` — PAS par le
site du veto de destination à `interrupt=True` littéral et inconditionnel
(`:1534-1560`, l'exception cpp#128 qui ne consulte jamais `_denial_is_
terminal`). Ce dernier site n'est atteignable pour `bash-git-show-redirect`
que via le rule_id `bash-git-show-redirect` lui-même, dont le pattern exige
une cible relative — une cible `/tmp/…` absolue n'y arrive donc jamais. C'est
la garantie qui rend le fix ci-dessous sûr (§ Décisions).

## Décision (a) vs (b)

**(a) — inconsistance, pas invariant ratifié.** Confirmé.

- cpp#154 a explicitement blanchi `is_tier3_dangerous_for_lethality` pour un
  redirect lexicalement contenu (`/tmp/…` ou relatif) — preuve directe
  d'intention : un research-write vers `/tmp` n'est pas létal.
- Le docstring de `_redirect_destination_veto_reason` (cpp#155) affirme que
  `_destination_veto_reason` est censée être "a STRICT SUPERSET of what this
  function proves for redirects" — c'était FAUX pour `bash-git-show-redirect`
  jusqu'à ce fix : c'est le bug, nommé dans le code lui-même sans avoir été
  fermé.
- Aucun document sous `docs/solutions/` (recherché : rien daté du 18/09 ni
  ailleurs sur "le classifier ne décide jamais sur le contenu" appliqué à ce
  cas précis) ne ratifie la létalité de l'échappement de confinement comme
  invariant indépendant du carve-out `/tmp` de cpp#154. Toute la trace écrite
  va dans l'autre sens.

Remède = le classifier (`_destination_veto_reason`), pas le contexte de
dispatch.

## Le fix

`src/claude_pilot/permissions.py`, dans `_destination_veto_reason`, la boucle
per-destination (`:1132` et suivantes). Avant :

```python
        for dest in dests:
            if kind == "bash-mkdir" and _is_sanctioned_tmp_scratch(dest):
                continue
            if kind == "bash-redirect":
                ...
```

Après (diff net, un seul branchement ajouté) :

```python
        for dest in dests:
            if kind == "bash-mkdir" and _is_sanctioned_tmp_scratch(dest):
                continue
            if (
                kind == "bash-git-show-redirect"
                and _is_contained_redirect_target(dest)
                and dest.startswith("/tmp/")
            ):
                continue
            if kind == "bash-redirect":
                ...
```

Le prédicat réutilisé, verbatim, est celui que `bash-redirect` (cpp#154/#155)
et `_redirect_destination_veto_reason` (cpp#154/#155/#176) utilisent déjà :
`_is_contained_redirect_target(dest) and dest.startswith("/tmp/")`. Rien n'est
réinventé. `is_tier3_dangerous` (le classificateur du REFUS) n'est pas
touché ; `_redirect_destination_veto_reason` n'est pas touché ; aucune règle
YAML n'est touchée.

**Pourquoi ce n'est PAS un élargissement de ce qui est admis.** La branche
n'est atteignable, en pratique, que depuis le site DENY (`_denial_is_
terminal`, via le veto de chaîne ou le refus par défaut) — jamais depuis le
site ALLOW à `interrupt=True` inconditionnel (`:1534`), qui n'atteint le
write-kind `bash-git-show-redirect` que via le rule_id du même nom, dont le
pattern YAML exige une cible relative. Une cible `/tmp/…` (absolue) ne peut
donc jamais porter ce rule_id et n'atteint ce site qu'en étant déjà refusée
par la policy. La commande reste refusée dans tous les cas ; seule sa
létalité change.

**Écart assumé par rapport au libellé initial du dispatch.** Le dispatch
demandait de ne toucher QUE `_denial_is_terminal`. La cause vérifiée montre
que le bug vit dans `_destination_veto_reason`, pas dans `_denial_is_
terminal` (qui se contente d'agréger trois verdicts déjà corrects sauf un).
Dupliquer le carve-out /tmp dans `_denial_is_terminal` sans toucher `_destination_
veto_reason` aurait recréé exactement la dérive que cpp#151 B0 et le docstring
de `_redirect_destination_veto_reason` mettent en garde ("two components
answering the same question and drifting apart is the exact failure mode
cpp#151 B0 closed") — et cela aurait aussi laissé `_destination_veto_reason`
elle-même incohérente avec sa propre documentation ("STRICT SUPERSET").
Corriger à la source évite la duplication et n'élargit rien (voir preuve du
paragraphe précédent). Jugement assumé, signalé ici pour revue.

## Preuve deux sens (both-directions)

**Positif (doit devenir NON-terminal) — replay exact #2471 :**
`_denial_is_terminal("Bash", {"command": "git show origin/main:crates/
mika-common/src/home.rs > /tmp/ck_home_main.rs"}, wt)` passe de `True`
(rouge, pré-fix) à `False` (vert, post-fix). Confirmé aussi au niveau handler
(`create_permission_handler`) : `PermissionResultDeny(interrupt=False)` — la
commande reste `PermissionResultDeny` (refusée), seul `interrupt` change.

**Négatif (doit rester terminal, les deux mondes) :**
`> /etc/ck.rs`, `> /var/outside/ck.rs` (hors worktree, non-/tmp), `>
../escape.rs` (traversal), `sed -i`, `rm -rf`, `git push --force` — tous
`_denial_is_terminal(...) is True` avant et après.

Tests : `tests/test_permissions.py`, section `cpp#195`
(`test_cpp195_destination_veto_git_show_tmp_carve_out`,
`test_cpp195_destination_veto_git_show_non_tmp_still_vetoed`,
`test_cpp195_denial_is_terminal_mika_2471_replay_survivable`,
`test_cpp195_negative_stays_terminal` (paramétré),
`test_cpp195_handler_end_to_end_still_denied_but_survivable`). Rouge-avant
capturé en stashant uniquement `permissions.py` et en rejouant `-k cpp195` :
3 échecs (`destination_veto_git_show_tmp_carve_out`,
`denial_is_terminal_mika_2471_replay_survivable`,
`handler_end_to_end_still_denied_but_survivable`), collés dans le corps de la
PR.

## Non-régression

Suite complète : `uv run pytest` — 1162 passed (aucune suite cpp#128/#151/
#154/#155/#166/#176 modifiée ni cassée). `uv run ruff check .` — clean.
`uv run mypy src` — clean.

## Hors portée

- Le charset de l'extraction (`[^\s;&|<>()]*`, hérité de `_redirect_targets`)
  ne supporte toujours pas les cibles à expansion de paramètre (`$n`) pour le
  DEV-NULL check spécifiquement — non pertinent ici (`/dev/null` est un
  littéral, pas un motif) ; voir aussi le round 2 ci-dessous qui a généralisé
  l'extraction.
- ~~`git show ...:x > /dev/null` reste vétoté~~ — **CORRIGÉ au round 2, voir
  § Suite (round 2)** : c'était précisément le trou qui empêchait le fix
  original de couvrir la commande réelle #2471.

## Suite (round 2 — MPC review, même ticket)

**Constat de la revue.** Le fix ci-dessus (round 1) répare la forme à UNE
seule cible (`git show …:x > /tmp/x`). La commande RÉELLE de mika#2471, telle
que rejouée par MPC, est composée : `git show origin/main:crates/mika-common/
src/home.rs > /tmp/ck_home_main.rs 2>/dev/null || true`. Elle restait
TERMINALE après le round 1 — un second bug, distinct et compoundant.

**Cause du round 2, établie à la source (sonde) :**
`_extract_write_destinations`'s branche `bash-git-show-redirect` utilisait
`_GIT_SHOW_REDIRECT_DEST_RE = re.compile(r">\s*([\w./-]+)\s*$")` — ancrée en
FIN de chaîne (`$`), donc ne capturant QUE la DERNIÈRE redirection de la
ligne. Pour `… > /tmp/ck_home_main.rs 2>/dev/null`, seule la cible
`/dev/null` (la dernière) était extraite — `/tmp/ck_home_main.rs`, déjà
carve-out par le round 1, n'était **jamais même examinée**. Et `/dev/null`,
pour le write-kind `bash-git-show-redirect`, n'avait lui-même AUCUN carve-out
(le skip `/dev/null` n'existait que dans la branche `bash-redirect`) — donc
`/dev/null` tombait sur le check générique de confinement et vétotait :
`"destination '/dev/null' resolves outside the worktree"`. Sonde (probe
against HEAD `3b8ddfd`, avant ce commit) :

| commande | dests extraits (avant) | dest_veto (avant) | terminal (avant) |
|---|---|---|---|
| `… > /tmp/x 2>/dev/null` | `['/dev/null']` (seul !) | non-None (`/dev/null` hors worktree) | True |

**Fix round 2 — deux points, factorisés pour ne pas re-dériver (cpp#151/#155) :**

1. **Extraction complète.** `_extract_write_destinations`'s branche
   `bash-git-show-redirect` réutilise désormais `_segment_redirect_targets`
   (LA MÊME extraction que `bash-redirect`, `tier1._redirect_targets` sous le
   capot) au lieu de l'ancien regex ancré fin-de-chaîne. Résultat : TOUTES
   les cibles de redirection de la ligne sont extraites, pas seulement la
   dernière. `_GIT_SHOW_REDIRECT_DEST_RE` est retiré (mort, plus référencé
   nulle part).
2. **Carve-out par-cible partagé.** Dans `_destination_veto_reason`, la
   branche per-target (`/dev/null` skip, disqualification lexicale
   fail-closed, carve `/tmp/`, sinon containment générique) est désormais
   UNE seule branche `if kind in ("bash-redirect", "bash-git-show-redirect")`
   au lieu de deux branches séparées et asymétriques — `bash-git-show-
   redirect` a maintenant EXACTEMENT le même jeu de carve-outs que
   `bash-redirect`, cible par cible.

Composabilité : la létalité est non-terminale **SSI CHAQUE cible** de la
ligne est individuellement non-létale (contenue sous `/tmp/`, ou
`/dev/null`) ; UNE seule cible réellement hors-worktree (non-/tmp,
non-/dev/null) sur la ligne reste TERMINALE — prouvé par les tests négatifs
composés (§ Acceptance criteria, AC2).

**Effet de bord corrigé, nommé.** Un test préexistant appelait directement
`_extract_write_destinations("bash-git-show-redirect", "git show")` (aucune
redirection du tout) et attendait `None`. La nouvelle extraction générique
rend `[]` dans ce cas précis (contrat différent de l'ancien regex, qui
rendait `None` pour "aucun match"). `_extract_write_destinations` normalise
maintenant `[] -> None` pour préserver EXACTEMENT le contrat de retour
préexistant ("`None` = destination indéterminable") pour tout appelant, pas
seulement `_destination_veto_reason` (dont le `if not dests` traitait déjà
les deux cas pareil). Aucune assertion de test existante modifiée.

## Acceptance criteria

- **AC1 — replay exact #2471, non-terminal ET refusé.** La commande RÉELLE
  `git show origin/main:crates/mika-common/src/home.rs > /tmp/ck_home_main.rs
  2>/dev/null || true` rend `_denial_is_terminal(...) is False`, ET reste
  `PermissionResultDeny` (jamais `PermissionResultAllow`) via
  `create_permission_handler` — vérifié end-to-end, pas seulement au niveau
  unitaire. → `test_cpp195_denial_is_terminal_mika_2471_replay_survivable`,
  `test_cpp195_handler_end_to_end_still_denied_but_survivable`.
- **AC2 — négatifs composés restent terminaux.** `> /tmp/x 2>/etc/y` (une
  cible carve-out + une hors-worktree), `> /etc/x 2>/dev/null` (une
  hors-worktree + une carve-out), `> ../escape 2>/dev/null` (traversal +
  carve-out), plus les négatifs simples du round 1 (`/etc`, hors-worktree
  non-/tmp, `..`, `sed -i`, `rm -rf`, `git push --force`) — tous
  `_denial_is_terminal(...) is True`, sur les deux têtes (avant/après ce
  commit). → `test_cpp195_negative_stays_terminal` (paramétré, 9 cas).
- **AC3 — composition /dev/null couverte.** `git show …:x > /dev/null` seul,
  et `/dev/null` comme UNE cible parmi plusieurs, sont carve-out pour
  `bash-git-show-redirect` exactement comme pour `bash-redirect` (cpp#130).
  → `test_cpp195_destination_veto_git_show_devnull_carve_out`,
  `test_cpp195_destination_veto_git_show_composition_every_target_extracted`.
- **AC4 — aucune autorisation nouvelle.** `is_tier3_dangerous` (le
  classificateur du REFUS), toute règle YAML, et
  `_redirect_destination_veto_reason` restent inchangés ; le refus (`policy
  allow ... vetoed` / `PermissionResultDeny`) est identique avant/après, seul
  `interrupt` (la létalité) change. Le site ALLOW à `interrupt=True`
  inconditionnel (`:1534`, l'exception cpp#128) reste hors d'atteinte pour
  une cible `/tmp/…` sur ce write-kind (le rule_id `bash-git-show-redirect`
  exige une cible RELATIVE). → preuve dans le corps du plan (§ round 1, "Pourquoi
  ce n'est PAS un élargissement"), et `test_cpp195_handler_end_to_end_still_
  denied_but_survivable` qui assert `isinstance(result, PermissionResultDeny)`
  explicitement.

## Preuve deux sens — round 2 (rouge-avant/vert-après)

Rouge capturé en stashant uniquement `permissions.py` (retour à `3b8ddfd`,
le head de PR#196 avant ce commit), tests gardés :

```
FAILED test_cpp195_destination_veto_git_show_tmp_carve_out
FAILED test_cpp195_destination_veto_git_show_devnull_carve_out
FAILED test_cpp195_destination_veto_git_show_composition_every_target_extracted
FAILED test_cpp195_denial_is_terminal_mika_2471_replay_survivable
FAILED test_cpp195_handler_end_to_end_still_denied_but_survivable
================= 5 failed, 10 passed, 43 deselected in 0.52s ==================
```

Les 10 passés incluent déjà les 3 négatifs composés (AC2) — ils étaient
CORRECTS avant même ce round (la composition n'existait pas encore pour les
rendre faussement survivables, donc rien à régresser là) ; c'est le signal
qu'AC2 teste bien une propriété qui doit rester vraie, pas une propriété que
ce commit invente.

Vert après restauration du fix : 15/15 (`-k cpp195`). Suite complète : 1167
passed (1162 + 5 nouveaux tests round 2). `ruff check .` / `mypy src` /
`verify-pipeline.sh` : clean.

## Références

- `src/claude_pilot/permissions.py:767` — `_denial_is_terminal`.
- `src/claude_pilot/permissions.py:1088` — `_destination_veto_reason`, la
  fonction corrigée.
- `src/claude_pilot/permissions.py:1204` — `_redirect_destination_veto_reason`
  (déjà correcte, non touchée).
- `src/claude_pilot/tier1.py:355` — `_is_contained_redirect_target`, le
  prédicat réutilisé verbatim.
- `docs/plans/2026-09-04-002-fix-154-redirection-fichier-letalite-plan.md` —
  cpp#154, le carve-out /tmp d'origine.
- `docs/plans/2026-08-30-002-fix-128-nonlethal-policy-denial-plan.md` —
  cpp#128, la distinction refus/létalité.
- `docs/plans/2026-09-04-001-fix-151-refus-non-letal-residuel-plan.md` —
  cpp#151 B0, "one lethality computation".
- claude-pilot#195, mika#2471, transcript `117d7a20`.
