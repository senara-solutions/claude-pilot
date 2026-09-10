---
issue: claude-pilot#166
title: bash-git-readonly refuse `git show <rev>:<path> > fichier` pour un ref non-hex — les grooms cross-branche échouent en déterministe - Plan
type: fix
scope_repo: claude-pilot
priority: p1-important
date: 2026-09-10
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# bash-git-readonly refuse `git show <rev>:<path> > fichier` pour un ref non-hex — les grooms cross-branche échouent en déterministe - Plan

## Goal Capsule

**Objectif.** La règle YAML sanctionnée `bash-git-show-redirect` (cpp#35)
autorise `git show <rev>:<path> > <cible>` mais restreint le REF source au
charset `[a-f0-9]+` — un SHA, une SHA abrégée, ou une branche/tag nommée en
hex. Le flux groom-plan-only qui réutilise un plan d'une AUTRE branche (ticket
dé-groomé, plan sur une branche umbrella) exécute exactement :

```
git show FETCH_HEAD:docs/plans/<plan>.md > <worktree>/docs/plans/<plan>.md
```

`FETCH_HEAD` n'est pas `[a-f0-9]+` (le `T`, le `C`, le `_` n'y sont pas). La
commande retombe donc sur `bash-git-readonly` (`^git\s+(status|log|diff|show|
…)`), qui matche le PRÉFIXE `git show` mais n'accorde aucune exception à la
redirection — `_bash_allow_is_chain_safe`'s wholesale `>` veto refuse alors la
commande entière. Aucun octet n'est écrit, aucun marqueur GROOMED n'est posé,
et le ticket re-groome sans fin (mika#2131 : sessions 763d48a1, bc35d0d9,
fcb82364 — la 3e re-dispatchée avec une contrainte de prompt nommant la cause,
sans effet, parce que la cause est dans la policy, pas dans le prompt).

**Correctif.** Élargir le charset du REF dans le pattern YAML de
`[a-f0-9]+` à `[\w./-]+` — le même charset déjà utilisé pour le chemin source
et pour la cible de redirection dans la même règle. Le charset ainsi élargi
admet `FETCH_HEAD`, `HEAD`, `ORIG_HEAD`, `MERGE_HEAD`, un nom de branche
(`feature/166-fix`), un ref remote-tracking (`origin/main`), un tag
(`v1.2.3`), ou toujours un SHA. La cible (le côté écriture) n'est **pas**
touchée : les lookaheads `(?!/)(?!~)(?!.*\.\.)` qui confinent la redirection
au worktree restent EXACTEMENT ceux de cpp#35, inchangés caractère pour
caractère.

**Pourquoi c'est sûr.** cpp#43 a déjà établi, à l'écriture même de cpp#35, que
la mutabilité de la source n'est PAS la frontière de sécurité de cette règle :
même le charset hex-only admettait déjà une branche mutable, force-pushable
(`git show deadbeef:f` résout `deadbeef` comme un ref quand il en existe un,
git préfère le ref à l'objet). La sûreté de la règle a **toujours** reposé
uniquement sur la CIBLE littérale de la redirection — jamais sur ce que
nomme, ou peut faire bouger, le ref source. Élargir le charset du ref ne
change donc rien au modèle de menace ; ce plan ne touche à AUCUN caractère du
groupe cible.

## La crux de sécurité — auto-confinement, pas délégation au veto cpp#155

Le corps du ticket met en garde explicitement : ne pas s'appuyer sur le veto
runtime `_destination_veto_reason` pour confiner la cible, parce que ce veto
est **aveugle aux redirections shell** — c'est le bug encore ouvert cpp#155
(`_destination_veto_reason("echo hi > /etc/passwd", cwd)` rend `None` sur
`main`, mesuré 2026-09-04, parce que `_segment_write_kind` ne classe QUE
`cp`/`mv`, `mkdir`, et `git show >` comme verbes d'écriture — un `echo`/`cat`/
`tee` avec redirection n'est classé nulle part, donc sa cible n'est jamais
extraite).

**Mesure, avant de conclure quoi que ce soit.** `_segment_write_kind`
(`permissions.py:832`) classe DÉJÀ `git show ... >` PAR NOM :

```python
if cmd == "git" and _GIT_SHOW_RE.match(seg) and ">" in seg:
    return "bash-git-show-redirect"
```

Cette classification est indépendante du ref — elle matche sur le verbe et la
présence d'un `>`, jamais sur ce qu'il y a entre `show` et le `>`. Donc, pour
la forme précise que ce ticket corrige, `_destination_veto_reason` n'est PAS
aveugle : elle voit et valide la cible, exactement comme elle le fait déjà
pour la source hex depuis cpp#35/cpp#38/cpp#42 (testé en suite). Le scope de
cpp#155 est spécifiquement les verbes que `_segment_write_kind` NE classe PAS
(`echo`, `cat`, `tee`, …) — `git show >` n'en fait pas partie, et cpp#155 ne
change rien à cette règle-ci.

**Ce que ce plan fait quand même, et pourquoi.** Le correctif n'élargit QUE le
charset du ref dans le pattern YAML — il ne touche ni au groupe cible de ce
pattern, ni au code Python de `_destination_veto_reason`/`_segment_write_kind`.
La confinement de la cible reste donc, comme avant cpp#166, à DEUX niveaux
indépendants, ni l'un ni l'autre nouveau :

1. **Niveau string, PRIMAIRE et auto-suffisant.** Le pattern YAML lui-même
   refuse — par un match POSITIF, entièrement ancré (`^...$`) — toute cible
   absolue, `~`, ou contenant `..`, AVANT que la commande ne soit jamais
   autorisée à s'exécuter. C'est un filtre pré-exécution qui se suffit à
   lui-même : une commande dont la cible n'a pas cette forme ne matche
   simplement PAS `bash-git-show-redirect`, retombe sur `bash-git-readonly`,
   et se fait refuser par le veto `>` généraliste de la chain-safety. Ce
   niveau ne dépend d'AUCUN veto runtime.
2. **Niveau runtime, défense en profondeur, PAS le mécanisme principal.**
   `_destination_veto_reason`, consulté au point d'octroi (`create_
   permission_handler`, APRÈS chain-safety), re-vérifie le confinement en
   résolvant le chemin — ce qui attrape ce que le niveau string ne peut pas
   voir : un symlink commité qui échappe au worktree (`> esc/passwd` où
   `esc -> ../OUTSIDE`, résidu documenté cpp#38 depuis cpp#35). Ce niveau
   EXISTE pour ce verbe précis et N'EST PAS affecté par le trou cpp#155,
   pour la raison mesurée ci-dessus.

Résultat : le correctif est auto-confinant au sens demandé par le ticket — il
n'élargit pas la cible, ne repose sur aucune réparation de cpp#155, et le
niveau string suffirait à lui seul à refuser toute évasion (`/etc/passwd`,
`../escape`, tout chemin hors worktree y compris sous `/tmp`, qui n'a AUCUNE
exemption dans cette règle — contrairement à `bash-mkdir`, cpp#143).

## Mesure — reproduction sur `main` avant correctif

Sondé dans le venv du worktree de ce ticket, `PYTHONPATH=src` :

```python
from claude_pilot.policy import evaluate, load_policy
from claude_pilot.permissions import _bash_allow_is_chain_safe
policy = load_policy(None)
cmd = "git show FETCH_HEAD:docs/plans/x.md > docs/plans/x.md"
evaluate(policy, "Bash", {"command": cmd})
# -> decision=allow, rule_id=bash-git-readonly
_bash_allow_is_chain_safe(policy, "Bash", {"command": cmd})
# -> False
```

`policy.evaluate` retourne `allow` sous `bash-git-readonly` (le préfixe `git
show` matche), mais `_bash_allow_is_chain_safe` refuse — parce que
`pd.rule_id != "bash-git-show-redirect"`, la sanction de redirection ne
s'applique pas, et le segment unique contenant un `>` échoue le test
tier1-safe / tier3-dangerous de la boucle générale. C'est exactement le
`policy:deny [bash-git-readonly]` mesuré en session par le corps du ticket.

## Le correctif

Un seul groupe de caractères change, dans `policies/permissions.yaml`, règle
`bash-git-show-redirect` :

```diff
- pattern: '^git\s+show\s+[a-f0-9]+:[\w./-]+\s*>\s*(?!/)(?!~)(?!.*\.\.)[\w./-]+\s*$'
+ pattern: '^git\s+show\s+[\w./-]+:[\w./-]+\s*>\s*(?!/)(?!~)(?!.*\.\.)[\w./-]+\s*$'
```

Le groupe cible (`(?!/)(?!~)(?!.*\.\.)[\w./-]+`) est identique caractère pour
caractère à avant. Le charset `[\w./-]+` exclut toujours `$`, backtick,
espace, `;`, `|`, `&`, guillemets — aucune expansion shell, substitution ou
chaînage ne peut se glisser dans le ref élargi, exactement comme pour le
chemin source (déjà `[\w./-]+`) et la cible.

`permissions.py` (`_bash_allow_is_chain_safe`) n'a besoin d'AUCUN changement :
l'honorage du rule_id `bash-git-show-redirect` est déjà indépendant de la
forme du ref — il teste `pd.rule_id == "bash-git-show-redirect"`, pas le
contenu de la commande. Seuls les commentaires qui documentaient l'ancienne
contrainte hex-only sont mis à jour (dans le YAML et dans le bloc de
commentaire de `_bash_allow_is_chain_safe`), pour ne pas laisser une fausse
doctrine « source = hex uniquement » à côté d'un code qui ne l'impose plus, et
pour nommer explicitement l'interaction cpp#155 (voir section précédente) —
faute de quoi le prochain lecteur retombe dans la même question.

## Livrables

- **L1** — `policies/permissions.yaml`, règle `bash-git-show-redirect` : ref
  charset `[a-f0-9]+` → `[\w./-]+`. Commentaire réécrit pour (a) documenter
  cpp#166, (b) répéter — pas seulement référencer — que la mutabilité n'a
  jamais été la frontière de sécurité (cpp#43), (c) nommer l'interaction
  cpp#155 et pourquoi elle ne s'applique pas à cette règle.
- **L2** — `permissions.py`, bloc de commentaire au-dessus de la ligne
  `if pd.decision == "allow" and pd.rule_id == "bash-git-show-redirect":`
  dans `_bash_allow_is_chain_safe` : même mise à jour de doctrine, plus le
  paragraphe cpp#166/cpp#155 détaillé (quel verbe cpp#155 classe, lequel il ne
  classe pas, pourquoi `git show >` n'est pas dans le trou).
- **L3** — `tests/test_policy_devpilot.py` :
  - Positifs : la forme exacte du ticket (`FETCH_HEAD` + cible worktree-
    relative) au niveau `evaluate`/`_effective`, au niveau
    `_bash_allow_is_chain_safe`, et au niveau `_dest_effective` (le chemin
    complet policy + chain-safe + destination validator, avec un vrai
    worktree sur disque). Plus une matrice d'autres formes de ref (`HEAD`,
    `ORIG_HEAD`, `MERGE_HEAD`, branche, branche-avec-slash, remote-tracking,
    tag, SHA majuscule).
  - Négatifs (déplacés depuis l'ancienne matrice « non-SHA » où ils étaient
    DENY par accident de la contrainte hex, alors que ce n'était pas leur
    intention documentée) → suppression des entrées `HEAD`/`main` de la
    matrice « unsafe » puisqu'elles sont maintenant des positifs légitimes.
  - Négatifs cpp#166 : `FETCH_HEAD:...` vers `/etc/passwd`, `../escape`,
    `/tmp/x` (hors worktree — PAS d'exemption `/tmp` dans cette règle,
    contrairement à `bash-mkdir`/cpp#143), `~/escape`, `$(evil)` — tous
    DENY, prouvant que le seul axe élargi est le ref, jamais la cible.
  - Anti-vacuité (`test_guard_denies_pre_166_fetch_head_shape`) : rejoue le
    pattern PRÉ-cpp#166 (hex-only, recopié littéralement) contre
    `FETCH_HEAD:docs/plans/x.md > docs/plans/x.md` et affirme qu'il NE
    matche PAS — donc qu'un revert du L1 ferait retomber le test positif
    correspondant en échec.
  - Destination validator (cpp#38 parity) : le même worktree avec symlink
    d'évasion (`esc -> ../OUTSIDE`) déjà utilisé par les tests hex, rejoué
    avec `FETCH_HEAD` — DENY — et avec une cible légitime — ALLOW.

## Séquence

1. Mesurer l'état `main` (fait ci-dessus, M1).
2. L1 (le seul changement fonctionnel).
3. L3 négatifs d'abord (dont l'anti-vacuité) — vérifier qu'ils échoueraient
   sans L1 (le test anti-vacuité teste littéralement le pattern pré-fix, donc
   pas besoin de stash/revert pour le prouver).
4. L3 positifs.
5. L2 (documentation).
6. `uv run pytest` complet, `uv run ruff check`, `uv run mypy src`.

## Acceptance Criteria

- **AC1** — `git show FETCH_HEAD:docs/plans/x.md > docs/plans/x.md` (cible
  worktree-relative) devient ALLOW de bout en bout (policy + chain-safe +
  destination validator).
  → Vérification : `test_bundled_allows_git_show_redirect_fetch_head_cross_branch_groom`,
  `test_guard_honors_git_show_redirect_fetch_head_shape`,
  `test_dest_validator_cpp166_fetch_head_legit_plan_allowed`.
- **AC2** — `git show FETCH_HEAD:... > /etc/passwd`, `> ../escape`, et
  `> /tmp/x` (hors worktree) restent DENY.
  → Vérification : entrées correspondantes dans
  `test_bundled_denies_git_show_redirect_unsafe`.
- **AC3** — Toute autre forme de `git show` en lecture seule (sans `>`) est
  inchangée — la règle `bash-git-readonly` continue de s'appliquer
  normalement.
  → Vérification : suite `test_policy_devpilot.py` complète, verte sans
  modification des tests de lecture seule préexistants.
- **AC4 (anti-vacuité)** — Un revert de L1 (retour à `[a-f0-9]+`) fait échouer
  le test positif FETCH_HEAD.
  → Vérification : `test_guard_denies_pre_166_fetch_head_shape` prouve la
  négative sur le pattern pré-fix directement (pas besoin d'un revert réel du
  fichier pour l'exécuter en CI) ; validé manuellement ci-dessous.

### Validation manuelle du revert (AC4)

Exécuté réellement (pas seulement décrit) sur ce worktree, en stashant
UNIQUEMENT `policies/permissions.yaml` (L1) pour isoler son effet des tests
déjà écrits (L3) :

```
$ git stash push -- src/claude_pilot/policies/permissions.yaml
$ uv run pytest tests/test_policy_devpilot.py -k fetch_head -q
4 failed, 7 passed, 236 deselected
FAILED test_bundled_allows_git_show_redirect_fetch_head_cross_branch_groom
FAILED test_bundled_allows_git_show_redirect_any_ref_shape[git show FETCH_HEAD:file > foo]
FAILED test_guard_honors_git_show_redirect_fetch_head_shape
FAILED test_dest_validator_cpp166_fetch_head_legit_plan_allowed

$ git stash pop
$ uv run pytest tests/test_policy_devpilot.py -k fetch_head -q
11 passed, 236 deselected
```

Exactement les 4 tests positifs qui exercent le ref `FETCH_HEAD` échouent sans
L1, et eux seuls (les 7 négatifs/parité restent verts sans L1 — ce sont les
tests qui prouvent que la cible reste confinée quel que soit l'état du ref,
donc ils ne dépendent pas de L1). C'est la preuve directe que L1 est le seul
changement porteur de l'AC1.

## Hors portée

- cpp#155 lui-même (`_segment_write_kind` n'apprenant pas les redirections
  `echo`/`cat`/`tee`) — nommé et expliqué ici parce que le ticket #166 le
  cite explicitement, mais ni corrigé ni rouvert par ce plan.
- Tout élargissement du groupe CIBLE de `bash-git-show-redirect` — hors
  scope, et le ticket l'exclut explicitement (« self-confining »).
- Le flux dev-pilot / prompt-side (option 1 du ticket : lire via `git show`
  sans `>` puis écrire via l'outil Write) — le ticket retient l'option 2
  (allow-list), ce plan l'implémente seule.

## Références

- `src/claude_pilot/policies/permissions.yaml:226-264` — la règle
  `bash-git-show-redirect`, son commentaire cpp#35/cpp#43/cpp#166.
- `src/claude_pilot/permissions.py:538-600` (approx.) — le bloc `_bash_allow_
  is_chain_safe` qui honore le rule_id, mis à jour L2.
- `src/claude_pilot/permissions.py:832-846` — `_segment_write_kind`, la
  classification structurelle qui rend `git show >` non-aveugle
  indépendamment de cpp#155.
- `src/claude_pilot/permissions.py:1003-1042` — `_destination_veto_reason`.
- claude-pilot#155 — le trou encore ouvert, verbes non classés
  (`echo`/`cat`/`tee`), scope distinct de ce ticket.
- claude-pilot#43 — la décision d'architecture originelle : la mutabilité de
  la source n'est pas la frontière de sécurité.
- claude-pilot#38 / #42 — containment worktree + denylist control-plane, le
  niveau runtime réutilisé sans modification ici.
- mika#2131 — l'incident de production (sessions 763d48a1, bc35d0d9,
  fcb82364) qui a fondé ce ticket.
