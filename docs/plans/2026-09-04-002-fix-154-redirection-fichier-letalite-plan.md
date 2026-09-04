---
issue: claude-pilot#154
title: Un refus de forme sur une commande qui redirige vers un fichier contenu n'est plus létal - Plan
type: fix
scope_repo: claude-pilot
priority: p1-important
date: 2026-09-04
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# Un refus de forme sur une commande qui redirige vers un fichier contenu n'est plus létal - Plan

## Goal Capsule

**Objectif.** Aujourd'hui, **toute** commande contenant une redirection `>` vers
un fichier réel est classée tier3 pour la *létalité*. Conséquence mesurée : un
refus dont la cause est purement une question de **forme** (une chaîne que
`_bash_allow_is_chain_safe` ne sait pas honorer) **tue la session** dès lors que
la commande écrit un fichier de travail — même sous `/tmp`, même dans le
worktree. Trois pilotes de mika#2158 sont morts là aujourd'hui, le dernier avec
8 commits poussés et la PR à un appel de distance.

Ce plan retire la redirection-vers-un-fichier-**contenu** de la classe létale,
**sans toucher au refus** : la commande reste refusée exactement comme
aujourd'hui, elle revient au modèle en `tool_result` d'erreur, et la séance
continue. Une commande réellement dangereuse (`rm -rf`, `git push --force`,
`sed -i`, `bash -c`, écriture hors worktree, écriture sur le control plane)
reste terminale.

**Moyen.** Élargir le mécanisme que cpp#130 a déjà posé — et *seulement* celui-là.
cpp#130 retire, avant le test des motifs tier3 de létalité, une redirection vers
le puits inerte `/dev/null`. Ce plan retire, au même endroit et de la même façon
(lexicalement, sans toucher au disque), une redirection dont la cible est
**lexicalement contenue** : un chemin littéral sous `/tmp/`, ou un chemin
relatif au worktree. Une cible absolue hors `/tmp` (`/etc/passwd`), une cible
`~`, une cible contenant `..` ne sont pas retirées : le motif `>` continue de
matcher et le refus reste létal.

C'est le prolongement direct de la phrase que cpp#130 a laissée en dette dans
son propre docstring :

> « Widening the exemption to in-worktree targets is left to the destination
> veto (`permissions._destination_veto_reason`), which cpp#130 deliberately
> does not touch. »

Ce plan constate que le veto de destination **ne peut pas** porter cet
élargissement en l'état (§ Mesure, constat M3) et pose l'élargissement là où
cpp#130 l'avait laissé : dans le classificateur de létalité.

## Mesure — l'état de `main`, sondé

Sonde exécutée le 2026-09-04 dans le venv `claude-pilot` installé
(`~/.local/share/uv/tools/claude-pilot/bin/python`), contre le worktree de ce
ticket à `e893c6d` (HEAD de `main`). Décideurs réels, pas de stub :
`policy.evaluate`, `_bash_allow_is_chain_safe`, `_destination_veto_reason`,
`is_tier3_dangerous_for_lethality`, `_denial_is_terminal`.

| commande | policy | chain_safe | dest_veto | tier3_leth | **_denial_is_terminal** |
|---|---|---|---|---|---|
| `mkdir -p /tmp/2158bodies && for n in …; do gh issue view $n … > /tmp/2158bodies/$n.md 2>/tmp/2158bodies/$n.err && echo …; done` | allow `bash-mkdir` | **False** | None | True | **True** |
| `cat > /tmp/probe_test.rs <<'EOF'…EOF` + `python3 - <<'PY'…PY` | allow `bash-cat-heredoc-tmp` | **False** | None | True | **True** |
| `cat > probe.rs <<'EOF'…EOF` (worktree) | deny (défaut) | False | None | True | **True** |
| `echo hi > notes.txt` (worktree) | deny (défaut) | False | None | True | **True** |
| `echo hi > /etc/passwd` | deny (défaut) | False | **None** | True | True |
| `mkdir x && rm -rf /tmp/y` | allow `bash-mkdir` | False | None | True | True |
| `grep foo bar >/dev/null` | allow `bash-grep` | False | None | **False** | **False** (cpp#130) |
| `cat > /tmp/probe.rs <<'EOF'…EOF` **seul** | allow `bash-cat-heredoc-tmp` | **True** | None | (True) | (jamais consulté) |

Trois constats portent le plan :

**M1 — la létalité vient bien du motif `>` générique.** `TIER3_PATTERNS`
(`tier1.py:203`) contient `(?<!<)>{1,2}(?!\(|&[\d-])`. `is_tier3_dangerous_for_lethality`
ne retire que `\d*>{1,2}\s*/dev/null` (`_STDOUT_DEVNULL_RE`, cpp#130). Toute
autre redirection — `/tmp`, worktree, `/etc` — reste létale. Les lignes 1 à 4 du
tableau sont la même cause.

**M2 — le refus, lui, est correct et n'est pas en cause.** Lignes 1 et 2 :
`chain_safe = False`. La boucle `for` avec redirections après le `&&` (ligne 1)
et le second heredoc `python3 - <<'PY'` (ligne 2) cassent réellement le contrat
« sole-command + no-trailing ». Le ticket le dit et la sonde le confirme : *« le
refus est correct, c'est sa létalité qui ne l'est pas. »* Ce plan ne touche
à aucune règle d'autorisation.

**M3 — `_destination_veto_reason` est AVEUGLE aux redirections, donc il ne peut
pas servir de garde à lui seul.** Ligne 5 : `echo hi > /etc/passwd` rend
`dest_veto = None`. La raison est structurelle : `_segment_write_kind`
(`permissions.py:812`) ne classe que `cp`/`mv`, `mkdir`, et `git show >`. Un
`echo`/`cat` avec redirection n'est **aucun** de ces trois, donc la cible n'est
jamais extraite. **Conséquence directe pour l'AC1 :** une implémentation qui se
contenterait de sa condition finale — « et `_destination_veto_reason` rend
`None` » — rendrait `> /etc/passwd` NON létal. La condition opérante de l'AC1
est la première (« chaque destination d'écriture est dans le worktree ou sous
`/tmp` ») et elle exige une extraction de cible **nouvelle**, qui n'existe pas
dans le code. C'est le cœur du travail.

**M4 (pour l'AC4) — `bash-cat-heredoc-tmp` EST atteignable de bout en bout.**
Ligne 8 : un heredoc `/tmp` seul rend `policy = allow`, `chain_safe = True`
(`_is_sanctioned_pure_heredoc` est honoré explicitement dans
`_bash_allow_is_chain_safe`, `permissions.py:512-516`), `dest_veto = None` — le
handler **autorise**. `_denial_is_terminal` n'est jamais consulté sur ce chemin :
sa valeur `True` dans la sonde est vacante. La règle n'est donc pas une promesse
fausse ; elle est cassée **par la chaîne** quand une seconde commande la suit,
ce que le corps du ticket décrit exactement. L'AC4 se résout donc par sa
**première branche** (rendre l'atteignabilité testée), pas par le retrait.

## Décisions de conception

### D1 — Le mécanisme est lexical, jamais résolu sur le disque

`_is_sanctioned_tmp_scratch` (cpp#143) porte une leçon chèrement acquise,
documentée dans son bloc d'en-tête (`permissions.py:922-968`) : une version
antérieure résolvait la destination via `Path.resolve()` avant de tester
l'appartenance à `/tmp`, et **cassait les tests d'évasion par lien symbolique de
cpp#38** — un lien `esc -> ../../../tmp` dans le worktree résolvait vers `/tmp`
et se faisait exempter alors que le pilote n'avait jamais écrit `/tmp`.

Ce plan applique la même règle : le test porte sur le **texte littéral** de la
cible, tel qu'il apparaît dans la commande. Pas de `Path.resolve()`, pas de
`stat`, pas de `cwd`. Une cible relative qui traverse un lien symbolique ne
« passe » donc pas par ruse : elle passe parce qu'elle est *écrite* comme
relative — et la commande est refusée de toute façon, rien n'est écrit (voir R1).

**Corollaire d'API :** `is_tier3_dangerous_for_lethality` **garde sa signature
actuelle** `(command: str) -> bool`. Aucun `cwd` n'est ajouté. Les trois sites
d'appel et les tests existants ne bougent pas.

### D2 — La narrowing vit dans `is_tier3_dangerous_for_lethality`, donc couvre les DEUX routes de refus

L'AC1 énonce la classe par sa cause : « un refus dont la cause est la forme
(chain-unsafe) ». Deux placements sont possibles :

- **(a)** au seul site du veto de chaîne (`permissions.py:1231`) — colle à la
  lettre de l'AC1 ;
- **(b)** dans `is_tier3_dangerous_for_lethality`, consulté par
  `_denial_is_terminal`, donc par les deux sites (veto de chaîne `:1231` **et**
  refus ordinaire `:1316`).

**Ce plan retient (b).** Trois raisons :

1. *Deux notions de létalité dériveraient.* Le code porte déjà, en toutes
   lettres, la garde contre exactement cette dérive : cpp#151 volet B0 a
   supprimé trois calculs séparés de létalité au profit d'un seul, parce que
   « calling `_denial_is_terminal` separately per consumer would let them drift
   on a future edit » (`permissions.py:1226-1229`). Introduire une seconde
   notion de létalité — « létale ici, pas là, pour la même commande » — rouvre
   ce que cpp#151 vient de fermer.
2. *La propriété est celle de la commande, pas de la route.* « Écrire un fichier
   de travail contenu n'est pas, à soi seul, un motif de tuer la séance » est
   vrai indépendamment de la règle YAML qui a ou n'a pas matché en amont.
3. *Les lignes 3 et 4 de la table de mesure sont la même mort.* `cat > probe.rs`
   et `echo hi > notes.txt` dans le worktree passent par le refus par défaut
   (`rule_id = None`), pas par le veto de chaîne. Sous (a) elles resteraient
   létales. Ce sont des écritures de fichier de travail **dans le worktree**,
   la forme la plus banale qui soit.

(b) est un **sur-ensemble** de l'AC1, pas une contradiction : l'AC1 énonce une
condition suffisante (« reste non terminal **quand** … »), pas une condition
exclusive.

**Décision VERROUILLÉE : (b).** Arbitrée en première passe par mika-arch
(session `b16f49b4`, F2) : « la cohérence avec cpp#151 B0 (une seule computation
de létalité) et la couverture des écritures de travail dans le worktree
justifient le sur-ensemble ». L'implémenteur ne rouvre pas ce choix.

*Note historique (ne pas implémenter).* L'option (a) a été écartée, pas mise en
réserve. Elle consistait à ne toucher que le site `:1231` ; elle laissait létales
les lignes 3 et 4 de la mesure — `cat > probe.rs` et `echo hi > notes.txt` dans
le worktree, les deux écritures de fichier de travail les plus banales — et
introduisait une seconde notion de létalité à côté de la première. Elle est
consignée ici pour que le prochain lecteur sache qu'elle a été pesée, non pour
lui offrir un choix.

### D3 — Une cible portant une expansion (`$n`) est admise, avec son résidu nommé

L'AC3 exige que les lignes 1 et 2 du tableau du ticket passent au vert. La
ligne 1 redirige vers `/tmp/2158bodies/$n.md` : la cible **contient un `$`**.
Le charset de cpp#143 (`[\w./-]+`) l'exclurait, et l'AC3 échouerait.

Ce plan admet `$` dans la cible **pour la létalité seulement**, sous trois
conditions cumulatives : le texte littéral commence par `/tmp/` ou est relatif
(ni `/` ni `~` en tête), et ne contient `..` **nulle part**.

**Résidu accepté, nommé :** `$n` pourrait s'étendre à l'exécution en `../../x`,
et le test lexical ne le verrait pas. Deux raisons pour lesquelles ce résidu ne
porte pas de risque d'écriture :

1. **La commande n'est jamais exécutée.** On statue ici sur la létalité d'un
   refus déjà prononcé. Aucun octet n'est écrit, quelle que soit l'expansion.
2. **Aucun oracle de sondage ne s'ouvre.** L'argument de l'oracle est écrit dans
   le bloc de doctrine de `_denial_is_terminal` (`permissions.py:657-663`) : un
   pilote sous injection pourrait essayer une orthographe par tour jusqu'à en
   trouver une **autorisée**. Or la classe traitée ici est refusée par sa
   *forme* — chain-unsafe, ou refus par défaut. Aucune orthographe de
   destination ne fait basculer un refus de forme en autorisation : le refus est
   indépendant de la destination. Il n'y a donc rien à apprendre par sondage.

**Contre-exemple à cette dernière phrase, nommé pour l'architecte.** Il existe
une règle dont la chain-safety *dépend* de la cible : `bash-git-show-redirect`
(`permissions.py:536-556`) est honorée par rule_id, et son regex YAML contraint
la cible. Une variation de cible y bascule donc bien refus↔autorisation. Mais ce
chemin est intact : quand la règle matche, la commande est **autorisée**, puis le
veto de destination s'exécute à son propre site (`:1263`) où `interrupt=True`
est un littéral inconditionnel (cpp#128). Le fil de détente y reste armé, ce
plan ne le touche pas. La condition « aucun oracle » vaut donc pour la classe
traitée, et le seul contre-chemin conserve son garde-fou.

### D4 — Ce que ce plan NE fait PAS, et pourquoi

**On n'étend PAS `_segment_write_kind` aux redirections.** Ce serait la
fermeture « propre » du trou M3 (le veto de destination verrait enfin
`> /etc/passwd`), et le bloc de doctrine de `_denial_is_terminal` la nomme déjà
comme dette : « A write verb it does not classify (`touch`, `tee`, ...) is still
REFUSED […] Closing that gap means teaching `_segment_write_kind` more verbs,
which is its own change. »

Elle est écartée ici pour une raison de **régression mesurable**, pas de
paresse : `_destination_veto_reason` est aussi consulté sur le chemin
**autorisé** (`:1263`), où son verdict est terminal et inconditionnel. Classer
la redirection comme write-kind y ferait remonter `cat > /tmp/x <<'EOF'` — la
ligne 8 de la table, aujourd'hui **autorisée** — avec une cible hors worktree,
donc **VETO terminal**. Cela casserait l'AC4 et régresserait un chemin qui
fonctionne, sauf à étendre en même temps l'exception `/tmp` aux redirections.
C'est un second changement, avec sa propre surface de test.

**Livrable de suivi (à ficher par l'implémenteur, pas par ce plan) :**
« `_segment_write_kind` ne classe pas les redirections : `> /etc/passwd` rend
`dest_veto = None` », avec la mesure M3 comme preuve et la contrainte
« l'exception `/tmp` doit être étendue au write-kind redirection dans le même
changement, sinon `bash-cat-heredoc-tmp` régresse ». Condition de réveil :
quand cpp#154 est mergé.

## Livrables

### L1 — `_redirect_targets(command) -> list[str] | None` (`tier1.py`)

Extraction lexicale de **toutes** les cibles de redirection d'une commande.

**Formes EXTRAITES** (elles désignent un fichier, leur cible doit être validée) :

- `>`, `>>`, `N>`, `N>>` — avec ou sans espace avant la cible.
- `&>`, `&>>` — redirection combinée stdout+stderr. **Elles écrivent bien un
  fichier**, donc leur cible est extraite et validée comme les autres.

**Formes IGNORÉES** (elles ne désignent aucun fichier ; les laisser en place ne
retire rien, donc le motif `>` continue de matcher et la létalité tient) :

- `>&M`, `N>&M` — duplication de descripteur de fichier (`2>&1`). L'opérande
  est un numéro de descripteur, pas un chemin.
- `>(`, `<(` — substitution de processus, déjà couverte par ses propres motifs
  `>\(` / `<\(` dans `TIER3_PATTERNS`.

**Fail-closed** : rend `None` si une cible n'est pas extractible (guillemets
déséquilibrés, redirection en fin de chaîne sans opérande). L'appelant traite
alors comme non contenu, donc létal — c'est-à-dire le comportement de `main`.

Aucun accès disque.

> **Correction assumée d'un point de F3 (première passe, session `b16f49b4`).**
> F3 demandait d'ajouter `N>&M` **et `&>`** à la liste des formes *ignorées*.
> `N>&M` : oui, appliqué ci-dessus. **`&>` : non — et c'est délibéré.** `&> f`
> écrit réellement dans `f` (bash : stdout **et** stderr vers `f`). L'ignorer
> serait sûr au sens fail-closed — le `>` non retiré laisse le motif matcher,
> donc la commande reste létale — mais laisserait mourir un pilote sur
> `cmd &> /tmp/log`, exactement la classe que ce ticket ferme. L'**intention**
> de F3 (« ne pas fabriquer un faux confinement en mal-analysant une forme qui
> n'est pas une cible fichier ») est portée intégralement ; elle s'applique à
> `N>&M`, pas à `&>`.
>
> **TRANCHÉ en seconde passe (session `b16f49b4`, `Verdict: GROOMED`) :** la
> correction est retenue. Verbatim de l'architecte — « la divergence sur `&>`
> (conservé comme cible fichier) étant justifiée par l'objectif du ticket
> (éviter les morts sur redirections vers /tmp) et documentée dans L1 avec tests
> correspondants ajoutés en L5 ». **`&>` et `&>>` sont des cibles fichier
> extraites et validées.** L'implémenteur ne rouvre pas ce point.

### L2 — `_is_contained_redirect_target(dest) -> bool` (`tier1.py`)

Vrai si la cible littérale est contenue :

- `..` absent de la chaîne entière → sinon Faux ;
- ne commence pas par `~` → sinon Faux ;
- si absolue : doit commencer par `/tmp/` → sinon Faux ;
- si relative : Vrai.
- Charset : `[\w./$@{}-]+` — identique à cpp#143 **plus** `$`/`{`/`}` pour
  l'expansion de paramètre (D3). Tout autre métacaractère shell → Faux
  (fail-closed).
- `/dev/null` reste couvert en amont par `_STDOUT_DEVNULL_RE` ; ce prédicat ne
  le duplique pas.

### L3 — `_CONTAINED_REDIRECT_RE` : le retrait, dans `is_tier3_dangerous_for_lethality`

`is_tier3_dangerous_for_lethality` applique, **après** le retrait `/dev/null` de
cpp#130 et avant l'appel à `is_tier3_dangerous`, un second retrait : chaque
redirection dont la cible passe L2 est remplacée par un espace. Les autres
restent, donc le motif `>` continue de matcher et la létalité tient.

L'ordre est chargé : cpp#130 d'abord (il a ses propres tests d'arête sur
`/dev/null.txt`, `/dev/nullified`), ce retrait ensuite. Le docstring de la
fonction est mis à jour ; la phrase de dette citée en § Goal Capsule est
remplacée par le renvoi à cpp#154.

**`is_tier3_dangerous` — la fonction du REFUS — n'est pas touchée.** C'est la
garantie de l'AC1 seconde phrase et de M2.

### L4 — Test anti-vacuité rejoué (AC3)

`tests/test_policy_devpilot.py` : un test paramétré sur les **trois** commandes
du tableau du ticket (les deux `mkdir`+boucle des callbacks `193e368c` /
`ce63ad41`, et le double-heredoc de `0c3ba346`), avec `cwd` = un worktree
temporaire, asseyant `_denial_is_terminal(...) is False`.

**Preuve de non-vacuité (exigée par l'AC3, « sortie rouge dans la PR ») :**
l'implémenteur exécute le test **avant** L3 (le rouge : les trois rendent
`True`), colle la sortie d'échec dans le corps de la PR, puis applique L3 et
montre le vert. Un test qui n'a jamais été rouge ne prouve rien
(`feedback_verify_pipeline_passes_without_the_fix`).

### L5 — Tests de non-régression (AC2)

Nommer et **conserver sans réécriture** les tests existants de cpp#128/cpp#130
qui asseyent la létalité des commandes réellement dangereuses. L'implémenteur
les localise par `grep -n "_denial_is_terminal" tests/` et les cite par
`fichier:ligne` dans la PR. Ajouts (ne remplacent rien) :

- `rm -rf` / `git push --force` / `sed -i` / `bash -c` accompagnés d'une
  redirection contenue → **restent terminaux** (le verbe matche indépendamment
  du retrait) ;
- `echo hi > /etc/passwd` → **reste terminal** (cible absolue hors `/tmp`) ;
- `echo hi > ../x` et `echo hi > /tmp/../etc/x` → **restent terminaux** (`..`) ;
- `echo hi > ~/x` → **reste terminal** (`~`) ;
- évasion par lien symbolique de cpp#38 (`cp s esc/x`, `mkdir esc/x`) →
  inchangée : ces write-kinds passent par `_destination_veto_reason`, que ce
  plan ne touche pas.

Et, pour les formes de redirection énumérées en L1 :

- `cmd 2>&1 | tail` → **reste non létal** (aucune cible fichier ; `2>&1` est
  ignoré par L1 et déjà exempté en amont par `_FD_DEVNULL_RE`/le motif
  `(?!\(|&[\d-])`). Test de non-régression : c'est l'idiome que cpp#130 cite
  comme le survivant de la « two-character life-or-death gap ».
- `cmd &> /tmp/log` → **devient non létal** (cible extraite, contenue) ;
  `cmd &> /etc/log` → **reste létal**. C'est le couple qui prouve que `&>` est
  traité comme une cible fichier et non ignoré (voir la correction assumée
  en L1).
- `cmd > >(tee f)` → **reste létal** (substitution de processus, motif `>\(`
  indépendant du retrait).

### L6 — Test d'atteignabilité de `bash-cat-heredoc-tmp` (AC4, branche 1)

Test asseyant, sur `cat > /tmp/<token> <<'EOF'…EOF` **seul**, la chaîne complète
de bout en bout : `evaluate(...) == allow` avec `rule_id == "bash-cat-heredoc-tmp"`,
`_bash_allow_is_chain_safe(...) is True`, `_destination_veto_reason(...) is None`.
Les trois dans le **même** test, avec un commentaire nommant M4 : la valeur de
`_denial_is_terminal` sur cette commande est vacante (jamais consultée sur un
chemin autorisé), et un futur lecteur ne doit pas la prendre pour une
contradiction.

La règle n'est **pas** retirée : la mesure M4 établit qu'elle est honorée.

## Séquence

1. L1 + L2 (extraction + prédicat, purement lexicaux, testables seuls).
2. L4 en **rouge** — capturer la sortie.
3. L3 (le retrait) → L4 au vert.
4. L5, L6.
5. `uv run pytest` complet ; toute rupture dans les suites cpp#38/#42/#128/#130
   est un **arrêt**, pas un test à ajuster.

## Acceptance Criteria

- **AC1** — Un refus dont la cause est la forme (chain-unsafe) reste **non
  terminal** quand chaque destination d'écriture de la commande est dans le
  worktree ou sous `/tmp` et que `_destination_veto_reason` rend `None`. Le
  refus est surfacé au modèle comme `tool_result` d'erreur, la séance continue.
  La redirection vers un fichier ne suffit plus, à elle seule, à rendre un refus
  létal ; elle reste refusée sur le chemin tier1 si elle l'est aujourd'hui.
  → **Vérification :** `is_tier3_dangerous` (refus) inchangée, prouvée par
  l'absence de diff sur la fonction ; L3 + L4 pour la létalité.
- **AC2** — Une commande réellement dangereuse (tier3 par son verbe : `rm -rf`,
  `git push --force`, écriture sur le control plane, évasion de `cwd`) reste
  terminale — test existant de cpp#128/#130 **nommé, pas réécrit**.
  → **Vérification :** L5, avec citation `fichier:ligne` des tests conservés.
- **AC3** — Rejeu anti-vacuité : les trois commandes du tableau, avec `cwd` =
  worktree ; sur `main` `_denial_is_terminal` rend `True` (rouge), avec le
  correctif `False` (vert) ; sortie rouge dans la PR.
  → **Vérification :** L4, sortie d'échec collée dans le corps de la PR.
- **AC4** — La règle `bash-cat-heredoc-tmp` est soit atteignable (un heredoc
  seul vers `/tmp` est autorisé de bout en bout, handler compris — test), soit
  retirée avec son motif.
  → **Vérification :** L6. Branche retenue : **atteignable**, sur la mesure M4.

## Hors portée (repris du ticket, inchangé)

- Réécrire les dispositions de dispatch pour que les pilotes n'utilisent pas de
  redirections — demi-vie d'un remède manuel, mesurée trois fois le 2026-09-04.
- La cause de la chaîne elle-même (le modèle enchaîne deux heredocs) : le refus
  est correct, c'est sa létalité qui ne l'est pas.

**Ajouté par ce plan (D4) :** l'extension de `_segment_write_kind` aux
redirections — fichée en suivi, pas faite ici.

## Fire-Disposition

Trois des six livrables sont des **détecteurs** (L4, L5, L6). Leur disposition
au tir est fixée ici, avant écriture, conformément à la gate mika#1574 — et non
laissée à l'appréciation de l'implémenteur au moment où le rouge apparaît.

| Livrable | Nature | Tir sur état PRÉEXISTANT | Tir à l'EXÉCUTION |
|---|---|---|---|
| **L4** — anti-vacuité (3 commandes mortes) | détecteur | **Attendu rouge**, et ce rouge est le livrable : il est capturé et collé dans le corps de la PR (AC3). Un L4 qui ne serait PAS rouge avant L3 invalide le test, pas le code. | **(c) halte-et-remontée.** Un échec après L3 signifie que le retrait ne couvre pas une des trois formes mortes : arrêt, pas d'ajustement du test. |
| **L5** — non-régression (cpp#38/#42/#128/#130) | détecteur | **Attendu vert.** Un rouge préexistant sur `main` est une découverte, pas un test à corriger : halte et remontée à l'opérateur avant toute modification. | **(c) halte-et-remontée.** Un rouge après L3 signifie que le retrait a mordu sur une classe létale légitime. Arrêt. **Interdit explicitement : ajuster, marquer `xfail`, ou réécrire un test cpp#128/#130** — l'AC2 exige qu'ils soient *nommés*, pas *réécrits*. |
| **L6** — atteignabilité `bash-cat-heredoc-tmp` | détecteur | **Attendu vert** (mesure M4 sur `e893c6d`). Un rouge préexistant contredirait M4 : halte et remontée — la branche « atteignable » de l'AC4 tomberait, et le choix AC4 devrait être rouvert avec l'architecte. | **(c) halte-et-remontée.** L6 est aussi le canari de D4 : s'il rougit après L3, c'est qu'un changement a atteint le chemin autorisé, ce que ce plan interdit. |
| **L1/L2/L3** — extraction, prédicat, retrait | **pas des détecteurs** | Sans objet. | Sans objet : purement lexicaux, aucun état persistant n'est lu ni écrit, aucune base, aucun fichier. Le seul effet observable est la valeur de retour de `_denial_is_terminal`. |

**Aucun détecteur de ce plan n'écrit d'état, n'ouvre de ticket, ni ne notifie.**
Ce sont des tests `pytest`. La disposition « observations futures uniquement »
ne s'applique donc à aucun d'eux ; la disposition unique est **(c)
halte-et-remontée**, avec la seule exception nommée du rouge *attendu et
capturé* de L4 avant L3.

## Risques

- **R1 — « on rend non létale une commande qui écrit hors du worktree ».** Non :
  la cible doit être *lexicalement* contenue, et une cible non contenue n'est
  pas retirée, donc reste létale (L5). Et dans tous les cas la commande est
  **refusée** — aucun octet n'est écrit. Ce plan ne transforme aucun refus en
  autorisation.
- **R2 — extraction de redirection incomplète (une forme non reconnue).** Le
  fail-closed de L1 traite l'inconnu comme non contenu → létal, c'est-à-dire le
  comportement de `main`. Une lacune d'extraction ne peut donc pas *dégrader* la
  sécurité, seulement laisser subsister une mort.
- **R3 — dérive entre les deux retraits (`/dev/null` et « contenu »).** Ils
  vivent dans la même fonction, dans cet ordre, avec un commentaire unique. Les
  tests d'arête de cpp#130 (`/dev/null.txt`, `/dev/nullified`,
  `/dev/null/../etc/passwd`) sont conservés tels quels et doivent rester verts.
- **R4 — le sur-ensemble de D2 rend survivable un refus PAR DÉFAUT
  (`rule_id = None`), c'est-à-dire la posture « unknown, ask a human » du tier 2.**
  C'est le risque réel de D2, et il est assumé : le refus reste un refus, la
  commande n'est jamais exécutée, et la boucle de refus est bornée par
  `maxTurns=200` — le seul des quatre garde-fous de séance qui borne réellement
  une séance occupée-mais-stérile, comme l'établit le bloc de doctrine de
  `_denial_is_terminal` (`permissions.py:706-718`). Ce que D2 rend survivable,
  c'est un refus, pas une écriture.

## Références

- `src/claude_pilot/tier1.py:203` — le motif `>` générique de `TIER3_PATTERNS`.
- `src/claude_pilot/tier1.py:243` — `is_tier3_dangerous_for_lethality`, et son
  docstring qui laisse explicitement l'élargissement worktree en dette.
- `src/claude_pilot/permissions.py:640-720` — bloc de doctrine de
  `_denial_is_terminal` (les deux classes létales, l'argument de l'oracle).
- `src/claude_pilot/permissions.py:922-982` — `_is_sanctioned_tmp_scratch` et la
  leçon « lexical, pas résolu » (cpp#143).
- `src/claude_pilot/permissions.py:1216-1250` — site du veto de chaîne, et
  cpp#151 B0 (« une seule computation de létalité »).
- `src/claude_pilot/policies/permissions.yaml:215` — `bash-cat-heredoc-tmp`.
- Preuves du ticket : `/var/log/claude-pilot/{193e368c,ce63ad41,0c3ba346}*.stderr`.
