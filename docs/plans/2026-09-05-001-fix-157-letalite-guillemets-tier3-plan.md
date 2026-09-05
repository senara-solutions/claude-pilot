---
issue: claude-pilot#157
title: Un `<`/`>` entre guillemets n'est pas une redirection et ne rend plus un refus létal - Plan
type: fix
scope_repo: claude-pilot
priority: p1-important
date: 2026-09-05
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# Un `<`/`>` entre guillemets n'est pas une redirection et ne rend plus un refus létal - Plan

## Goal Capsule

**Objectif.** Le motif générique de `TIER3_PATTERNS` — `(?<!<)>{1,2}(?!\(|&[\d-])`
— est **aveugle aux guillemets**. Il compte donc comme redirection un `>` ou un
`<` **littéral à l'intérieur d'une chaîne citée**, où bash n'en voit aucun. Un
`sed 's/=.*/=<set>/'`, dont le `>` est le texte de remplacement, se classe
tier3-létal ; le refus de forme qui le frappe devient terminal et **tue la
séance**. C'est la mort du pilote de mika#2179, quatrième mort sur la létalité
des refus en 48 h.

Ce plan rend le classificateur de **létalité** conscient de la portée des
guillemets, pour les deux seuls caractères `<` et `>`. Il ne touche **ni au
refus** (`is_tier3_dangerous` est inchangée, la commande reste refusée
exactement comme aujourd'hui), **ni à aucune liste d'autorisation**, **ni à la
létalité d'une vraie redirection** hors guillemets.

**Moyen.** Un masquage lexical en amont, dans l'esprit exact des deux retraits
que cpp#130 et cpp#154 ont déjà posés dans cette même fonction : avant de tester
les motifs, on blanchit les `<`/`>` qui tombent dans une région citée. Le reste
de la chaîne est laissé **intact** — `rm -rf`, `bash -c`, `eval`, `sed -i`
continuent de matcher, **y compris entre guillemets**. Le masque ne connaît que
deux caractères.

**Ce que ce plan n'est délibérément PAS.** Il ne s'agit **pas** d'une exemption
par « la chaîne est toute-lecture-seule » (l'AC1 l'exclut explicitement) : une
telle exemption laisserait `sed 's/x/<y>/'` **seul** encore létal — la commande
incriminée en isolation — tout en ouvrant une porte bien plus large que
l'incident. La cause est la portée des guillemets ; le correctif y va.

## Mesure — l'état de `main`, sondé

Sonde exécutée le 2026-09-05 dans le venv `claude-pilot` installé
(`claude-pilot/.venv/bin/python`), `PYTHONPATH=src`, contre le worktree de ce
ticket à **`6d63ebb`** (HEAD de `main`, « cpp#154 »). Décideurs réels, pas de
stub : `is_tier3_dangerous`, `is_tier3_dangerous_for_lethality`,
`_denial_is_terminal`, `_redirect_destination_veto_reason`,
`_destination_veto_reason`.

### M1 — La létalité tient à un segment unique, et c'est le `>` cité

| entrée | `is_tier3_dangerous_for_lethality` |
|---|---|
| `sed 's/=.*/=<set>/'` **seul** | **True** |
| chaîne complète de l'incident | **True** |
| chaîne complète **privée de ce segment** | **False** |
| `echo 'a>b'` | **True** |
| `echo "a>b"` | **True** |

La reproduction du ticket est exacte. Ce n'est pas une agrégation : le segment
porte la létalité à lui seul, et le retirer suffit à l'éteindre.

### M2 — Le refus, lui, est correct et hors périmètre

`is_tier3_dangerous("sed 's/=.*/=<set>/'")` = **True**. La commande est refusée,
et doit l'être : `>` n'est pas un idiome autorisé. Le ticket le dit et la sonde
le confirme — *le refus est correct, c'est sa létalité qui ne l'est pas.*

### M3 — Le chemin complet de l'incident, décideur par décideur

```
policy.evaluate                   → allow, rule_id='bash-grep'
_bash_allow_is_chain_safe         → False   ← le REFUS (forme)
_destination_veto_reason          → None
is_tier3_dangerous_for_lethality  → True    ← la LÉTALITÉ (le '>' de '=<set>')
_denial_is_terminal               → True
```

Mesuré de bout en bout avec `cwd` = worktree :
`_denial_is_terminal("Bash", {"command": <chaîne de l'incident>}, cwd)` = **True**.

### M4 — DEUX contrôles négatifs du corps du ticket sont FAUX sur `main`

C'est la mesure la plus importante de ce plan, et elle contredit le ticket.
`cpp#154` (mergé la veille, `6d63ebb`) a déjà retiré de la classe létale toute
redirection vers une cible **lexicalement contenue** — sous `/tmp/` ou relative
au worktree :

| commande citée par le ticket | verdict attendu par le ticket | **verdict mesuré sur `main`** |
|---|---|---|
| `grep x > /etc/y` (AC2) | terminal | **True** ✅ conforme |
| `> $HOME/z` (AC2) | terminal | **True** ✅ conforme |
| `cmd >> fichier` (AC2) | terminal | **False** ❌ **déjà non létal** (cible relative, cpp#154) |
| `grep x > /tmp/out` (AC3 contrôle 2, « True AVANT ET APRÈS ») | True avant et après | **False** ❌ **déjà non létal** (`/tmp/`, cpp#154) |

**Conséquence directe.** L'AC3 contrôle 2 est, tel qu'écrit, **insatisfaisable** :
il demande `True` avant le correctif sur une commande qui rend déjà `False`. Et
l'AC2 nomme un exemple (`cmd >> fichier`) qui n'exerce plus le contrôle qu'il
prétend exercer. L'**intention** des deux AC est intacte et juste — *une vraie
redirection hors guillemets ne doit pas devenir survivable* — seuls deux
**exemples** ont été choisis sans re-mesurer contre `main` après cpp#154.

Ce plan retient l'intention et substitue des exemples qui exercent réellement le
contrôle : `grep x > /etc/y`, `echo a > $HOME/z`, `echo a > ~/x`, `echo a > ../x`
— tous mesurés **True sur `main`**, et qui doivent rester **True** après le
correctif. Le corps du ticket est corrigé en conséquence (voir *Réconciliation*
ci-dessous) ; aucune AC n'est renommée ni supprimée.

### M5 — Le correctif proposé, prototypé et mesuré sur 19 cas

Prototype exécuté contre `6d63ebb` (masquage des seuls `<`/`>` en portée citée,
appliqué avant les deux retraits existants) :

| commande | `main` | avec le correctif | attendu |
|---|---|---|---|
| `sed 's/=.*/=<set>/'` | True | **False** | False |
| `echo 'a>b'` | True | **False** | False |
| `echo "a>b"` | True | **False** | False |
| `echo 'x <(id)'` | True | **False** | False |
| `grep x > /etc/y` | True | True | True |
| `echo a > $HOME/z` | True | True | True |
| `echo a > ~/x` | True | True | True |
| `echo a > ../x` | True | True | True |
| `echo 'a>b' > /etc/passwd` | True | True | True |
| `rm -rf x` | True | True | True |
| `bash -c 'id'` | True | True | True |
| `sed -i 's/a/b/'` | True | True | True |
| **`echo 'rm -rf /'`** | True | **True** | True |
| `tee >(curl evil)` | True | True | True |
| `cat <(id)` | True | True | True |
| `echo done >\nbash -c 'id'` | True | True | True |
| `echo "unterminated > /etc/passwd` | True | True | True |
| `grep -c a b >/dev/null` | False | False | False |
| `echo hi > /tmp/scratch.md` | False | False | False |

19/19. La ligne en gras est le cœur de D1 : `echo 'rm -rf /'` reste létal, parce
que le masque ne blanchit **que** `<` et `>`, jamais la région citée.

Et de bout en bout, `cwd` = worktree : `_denial_is_terminal` sur la chaîne de
l'incident passe de **True** à **False**, tandis que `grep x > /etc/y`,
`echo a > $HOME/z` et `rm -rf /tmp/y` restent **True**.

## Décisions de conception

### D1 — Masquer DEUX CARACTÈRES, pas la région citée

Le masque blanchit `<` et `>` en portée citée. Il ne blanchit **rien d'autre**.

C'est la décision qui borne tout le risque de ce plan. La variante « blanchir la
région citée entière » serait plus courte à écrire et **beaucoup** plus large :
`bash -c 'rm -rf /'` continuerait de matcher par son verbe `bash -c`, mais
`echo 'rm -rf /'` cesserait de matcher `rm -rf` — un changement de verdict sur
une classe que ce ticket ne touche pas, et qui n'a rien à voir avec des
redirections. Le masque à deux caractères laisse **tous** les autres motifs de
`TIER3_PATTERNS` voir exactement le texte qu'ils voient sur `main`.

Corollaire testable, et c'est le contrôle qui distingue les deux variantes :
`echo 'rm -rf /'` doit rester **True** (L5).

### D2 — Le masque vit sur le chemin de LÉTALITÉ uniquement

`is_tier3_dangerous` (le REFUS) n'est **pas** touchée. Une commande portant un
`>` cité reste refusée exactement comme aujourd'hui — elle revient au modèle en
`tool_result` d'erreur, il peut l'adapter, et la séance continue. Ce plan ne
transforme **aucun refus en autorisation** ; aucune liste d'autorisation n'est
élargie ; aucun octet supplémentaire n'est écrit nulle part.

Même forme et même endroit que cpp#130 (`/dev/null`) et cpp#154 (cible
contenue) : un retrait lexical en tête de `is_tier3_dangerous_for_lethality`,
dont `permissions._denial_is_terminal` reste le consommateur unique (cpp#151 B0
a collapsé les trois calculs de létalité en un précisément pour que deux notions
de « fatal » ne puissent pas diverger).

### D3 — L'ORDRE : le masque en PREMIER, avant les deux retraits existants

```
is_tier3_dangerous_for_lethality(cmd)
  = is_tier3_dangerous(
        _strip_contained_redirects(          # cpp#154 — 3e
          _STDOUT_DEVNULL_RE.sub(" ",        # cpp#130 — 2e
            _mask_quoted_redirect_chars(cmd) # cpp#157 — 1er
          )))
```

L'ordre est porteur, dans les deux sens :

- **Le masque doit précéder `_STDOUT_DEVNULL_RE` et `_strip_contained_redirects`.**
  Ces deux-là extraient des **cibles** de redirection. Un `>` cité leur fabrique
  une cible fantôme : sur `main`, `_redirect_targets("echo 'a>b'")` rend `["b'"]`
  — un texte que le jeu de caractères de `_is_contained_redirect_target` rejette
  (à cause du `'`), donc la redirection n'est pas retirée et la commande reste
  létale. Masquer d'abord fait disparaître la cible fantôme au lieu de compter
  sur le hasard du guillemet fermant pour la faire échouer.
- **Le masque préserve les longueurs** (substitution caractère par caractère par
  une espace), donc aucun décalage d'indice pour les regex en aval, et il ne
  peut pas coller deux jetons ensemble.

Il **ne réordonne pas** cpp#130 et cpp#154 entre eux : `/dev/null` reste avant
« cible contenue », et les cas d'arête `/dev/nullified`, `/dev/null.txt`,
`/dev/null/../etc/passwd` gardent leur comportement (L5).

### D4 — Le veto de destination reçoit le même masque, pour qu'une seule notion de « où sont les redirections » existe

`_redirect_destination_veto_reason` (`permissions.py:1083`) rappelle
`_redirect_targets` sur la commande **brute**, et son docstring pose
explicitement l'invariant : *« Reaching this function implies every redirect
target already passed `_is_contained_redirect_target` »*. Sans masque, cet
invariant devient faux dès que le correctif laisse passer une commande à `>`
cité : la fonction verrait une cible fantôme que l'amont a masquée.

Mesuré : sur la chaîne de l'incident, les deux vetos rendent `None` **avec ou
sans** masque — le correctif tient déjà sans D4. D4 est retenu **pour la
cohérence**, pas pour le résultat : c'est exactement le mode de panne que
cpp#151 B0 a fermé (deux composants qui répondent à la même question et
divergent). Le sens du changement est monotone et sûr : le masque ne peut que
**retirer** des cibles fantômes, jamais en ajouter — et une cible fantôme est,
par construction, une cible où bash n'écrit rien.

Les deux autres consommateurs de `_redirect_targets` — `_strip_contained_redirects`
(`tier1.py:368`) — reçoivent déjà le texte masqué par D3. Il n'y en a pas
d'autre : `grep -rn "_redirect_targets" src/` rend trois sites, tous couverts.

### D5 — Guillemets non terminés : ne pas masquer, donc rester létal

Sur une chaîne dont un guillemet n'est jamais fermé, le scanner rend la commande
**inchangée** — donc le verdict de `main`, donc létal. Fail-closed.

**Ce sens de conservatisme est l'INVERSE de celui des deux scanners existants**,
et c'est délibéré. `contains_unquoted_metacharacter` et `_split_compound_command`
traitent le reliquat comme *à l'intérieur* du guillemet, parce que leur
conservatisme à eux consiste à **refuser** (ils décident d'une autorisation).
Ici la fonction décide d'une **létalité** : le conservatisme consiste à **ne pas
exempter**. Les deux choix sont le même principe — fail-closed — appliqué à des
questions de sens opposé. Le contrôle est épinglé (L5,
`echo "unterminated > /etc/passwd` reste `True`).

### D6 — Un TROISIÈME scanner de guillemets, et pourquoi on ne refactorise pas

Ce plan ajoute un troisième scanner POSIX de régions citées, à côté de
`contains_unquoted_metacharacter` (`tier1.py:625`) et `_split_compound_command`
(`tier1.py:509`). C'est une duplication assumée, et bornée :

- **Fusionner les trois est hors périmètre** de ce ticket p1. Chacun porte son
  propre conservatisme documenté (D5), et deux d'entre eux sont sur le chemin
  d'**autorisation** — les toucher pour réparer une **létalité** mettrait le
  chemin d'autorisation en jeu pour rien. La règle de la maison est claire :
  la vélocité n'autorise pas à élargir la surface d'un correctif p1.
- **La dette est nommée et épinglée** : L5 comprend un test de **parité des
  frontières de citation** — sur un corpus court d'arêtes (`'…\…'`, `"…\"…"`,
  `"…\\"`, guillemet non terminé, guillemets imbriqués), les trois scanners
  doivent s'accorder sur *où commence et finit une région citée*, chacun gardant
  son verdict propre. Un futur qui corrigerait l'un sans les autres casse ce
  test.
- **Suivi fiché** : l'extraction d'un `_quote_spans(command)` partagé part en
  ticket de suivi, pas ici.

## Livrables

### L1 — `_mask_quoted_redirect_chars(command: str) -> str` (`tier1.py`)

Scanner POSIX à un passage, placé juste au-dessus de
`is_tier3_dangerous_for_lethality`. Sémantique, alignée sur celle que
`_split_compound_command` documente déjà :

- Hors quotes : `'` et `"` ouvrent une région.
- Dans `"…"` : `\X` est une paire d'échappement consommée atomiquement (donc
  `\"` ne ferme pas la région) ; un `"` ferme.
- Dans `'…'` : la barre oblique inverse est littérale ; seul un `'` ferme.
- **Dans toute région citée, un `<` ou un `>` est remplacé par une espace.**
  Rien d'autre n'est remplacé.
- Guillemet non fermé en fin de chaîne → **retourner la commande inchangée**
  (D5).

Longueur préservée (substitution 1 pour 1). Aucun accès disque, aucun `cwd`,
signature `(str) -> str` — la fonction reste purement lexicale, comme l'exige la
leçon de cpp#143 (`permissions.py:922-968`).

Le docstring nomme : l'incident (cpp#157, mort du pilote mika#2179), la raison
du masque à deux caractères (D1), l'ordre (D3), et le sens inversé du
fail-closed (D5).

### L2 — Le branchement dans `is_tier3_dangerous_for_lethality` (`tier1.py:382`)

Une seule ligne : `_mask_quoted_redirect_chars` en position la plus intérieure
de la composition (D3). Le docstring existant est étendu d'un paragraphe
cpp#157, dans la forme des paragraphes cpp#130 et cpp#154 qui le précèdent —
notamment la phrase « ORDER IS LOAD-BEARING », mise à jour pour trois retraits.

`is_tier3_dangerous` n'est **pas** modifiée. L'absence de diff sur cette
fonction est la preuve de l'AC1 côté refus.

### L3 — Le même masque en tête de `_redirect_destination_veto_reason` (`permissions.py`)

Import de `_mask_quoted_redirect_chars` depuis `tier1`, appliqué à `command`
avant `_redirect_targets` (D4). Le docstring de la fonction — qui pose
l'invariant « toute cible atteignant cette fonction est passée par
`_is_contained_redirect_target` » — est mis à jour pour dire que les cibles
fantômes citées n'y arrivent plus du tout.

### L4 — Test anti-vacuité qui DISTINGUE (AC3)

Nouvelle classe `TestTier3QuotedRedirectCharLethality` dans `tests/test_tier1.py`,
placée après `TestTier3ContainedRedirectLethality`, dans la forme des deux
classes qui la précèdent (cpp#130, cpp#154).

**Rejeu 1 — le rouge qui devient vert (la fermeture) :**
```python
assert is_tier3_dangerous_for_lethality("sed 's/=.*/=<set>/'") is False
assert is_tier3_dangerous_for_lethality("echo 'a>b'") is False
assert is_tier3_dangerous_for_lethality('echo "a>b"') is False
assert is_tier3_dangerous_for_lethality("echo 'x <(id)'") is False
```
Les quatre rendent `True` sur `main` : le rouge est capturé et collé dans le
corps de la PR.

**Rejeu 2 — le vert qui reste vert (le discriminant) :**
```python
assert is_tier3_dangerous_for_lethality("grep x > /etc/y") is True
assert is_tier3_dangerous_for_lethality("echo a > $HOME/z") is True
assert is_tier3_dangerous_for_lethality("echo a > ~/x") is True
assert is_tier3_dangerous_for_lethality("echo a > ../x") is True
assert is_tier3_dangerous_for_lethality("echo 'a>b' > /etc/passwd") is True
```
`True` **avant et après**. Un correctif qui rendrait l'un de ces cinq `False`
est faux — c'est ce que le rejeu 2 existe pour attraper. (Les exemples
substitués aux deux exemples faux du corps sont mesurés en M4.)

**Bout en bout, sur le vrai décideur :**
```python
assert _denial_is_terminal("Bash", {"command": INCIDENT_2179}, cwd) is False
assert _denial_is_terminal("Bash", {"command": "grep x > /etc/y"}, cwd) is True
```
dans `tests/test_policy_devpilot.py`, avec la chaîne intégrale de l'incident en
constante nommée. Le classificateur seul ne suffit pas : c'est
`_denial_is_terminal` qui a tué le pilote.

### L5 — Non-régression et bornes (AC2, D1, D5, D6)

1. **Le refus est intact** — `is_tier3_dangerous` rend `True` sur les quatre
   commandes du rejeu 1. Le correctif ne rend rien autorisé.
2. **D1, le contrôle qui distingue les deux variantes de masque** —
   `is_tier3_dangerous_for_lethality("echo 'rm -rf /'")` reste `True`, de même
   que `"bash -c 'id'"` et `"sed -i 's/a/b/'"`. Un masque qui blanchirait la
   région citée entière rendrait le premier `False`.
3. **D5, le guillemet non terminé** — `echo "unterminated > /etc/passwd` reste
   `True`.
4. **Les arêtes de cpp#130 et cpp#154 sont NOMMÉES, pas réécrites** — les
   classes `TestTier3DevnullRedirectLethality` (`tests/test_tier1.py:1180`) et
   `TestTier3ContainedRedirectLethality` (`tests/test_tier1.py:1232`) doivent
   rester vertes **sans modification**. En particulier
   `test_strip_never_swallows_the_next_line` : le masque préserve les longueurs
   et ne touche pas aux fins de ligne, donc `echo done >\nbash -c 'id'` reste
   `True`.
5. **Parité des scanners (D6)** — sur un corpus court d'arêtes de citation, les
   trois scanners s'accordent sur les frontières des régions citées.

## Séquence

1. L1 seul (scanner pur, testable en isolation).
2. L4 en **rouge** — capturer la sortie du rejeu 1 (le rejeu 2 est déjà vert,
   c'est le point).
3. L2 (le branchement) → L4 entièrement au vert.
4. L3 (le veto), L5.
5. `uv run pytest` complet. Toute rupture dans les suites cpp#38/#42/#128/#130/#154
   est un **arrêt**, pas un test à ajuster.

## Acceptance Criteria

- **AC1** — Un `<` ou `>` à l'intérieur d'une portée entre guillemets (simples
  ou doubles) n'est pas un opérateur de redirection et ne compte pas comme
  indice tier3 de létalité. Le correctif vise la **portée des guillemets** dans
  le tokenizer de `is_tier3_dangerous_for_lethality`, PAS une exemption par
  « chaîne toute-lecture-seule ».
  → **Vérification :** L1 + L2 ; L4 rejeu 1 ; et la preuve négative que
  l'exemption n'est pas « toute-lecture-seule » — `sed 's/=.*/=<set>/'` **seul**
  rend `False`, ce qu'une exemption par chaîne laisserait à `True`.
- **AC2** — Une **vraie** redirection hors guillemets reste tier3-létale.
  → **Vérification :** L4 rejeu 2, sur `grep x > /etc/y`, `echo a > $HOME/z`,
  `echo a > ~/x`, `echo a > ../x`, `echo 'a>b' > /etc/passwd` — tous mesurés
  `True` sur `main` (M4) et devant rester `True`. **Substitution d'exemples,
  documentée en M4 :** `cmd >> fichier` (exemple d'origine) est **déjà** non
  létal sur `main` par cpp#154 et n'exerce donc plus ce contrôle ; l'intention
  de l'AC2 est inchangée.
- **AC3 (anti-vacuité qui DISTINGUE)** — Deux rejeux, pour que le test ne passe
  pas avec le mauvais correctif :
  1. `sed 's/=.*/=<set>/'` seul, `echo 'a>b'`, `echo "a>b"`, `echo 'x <(id)'` :
     `True` sur `main`, `False` avec le correctif. Rouge capturé, collé dans la
     PR.
  2. Une redirection réelle : `True` **AVANT ET APRÈS**. **Correctif d'exemple,
     documenté en M4 :** le corps nommait `grep x > /tmp/out`, mesuré `False`
     sur `main` (cpp#154) — l'AC était insatisfaisable telle qu'écrite. Le
     contrôle est porté par `grep x > /etc/y` et `echo a > $HOME/z`, mesurés
     `True` sur `main`. Un correctif qui rend l'un des deux `False` est faux.
  → **Vérification :** L4, sortie rouge collée dans le corps de la PR.
- **AC4 — supprimé** (par le corps du ticket, re-scopé le 2026-09-05). Aucune
  règle de `permissions.yaml` ne vise `.env` ; le refus vient de la forme. Rien
  à documenter. **Ce plan ne le réintroduit pas.**

## Hors portée (repris du ticket, inchangé)

- Le refus de forme lui-même (`_bash_allow_is_chain_safe = False` sur la chaîne
  à `;`) — il est correct qu'il refuse ; le bug est uniquement la **létalité**
  injustifiée via un `>` entre guillemets.
- Pourquoi le pilote diagnostiquait `gh` (auth transitoire).

**Ajouté par ce plan :**
- La fusion des trois scanners de guillemets en un `_quote_spans` partagé (D6) —
  fichée en suivi, pas faite ici.
- Le miroir Rust côté `mika` (`permission_pre_classifier.rs`) : la létalité est
  une décision **claude-pilot** (`_denial_is_terminal`), il n'y a pas de miroir
  à synchroniser pour ce correctif. Le miroir divergent déjà nommé
  (`tier1.py:647`, cpp#41) est un autre sujet et n'est pas touché.

## Fire-Disposition

Deux des cinq livrables sont des **détecteurs** (L4, L5). Leur disposition au
tir est fixée ici, avant écriture, conformément à la gate mika#1574.

| Livrable | Nature | Tir sur état PRÉEXISTANT | Tir à l'EXÉCUTION |
|---|---|---|---|
| **L4 rejeu 1** — anti-vacuité (4 formes citées + bout en bout) | détecteur | **Attendu rouge**, et ce rouge est le livrable : capturé et collé dans le corps de la PR (AC3). Un rejeu 1 qui ne serait PAS rouge avant L2 invalide le test, pas le code. | **(c) halte-et-remontée.** Un échec après L2 signifie que le masque ne couvre pas une des formes citées : arrêt, pas d'ajustement du test. |
| **L4 rejeu 2** — discriminant (5 redirections réelles) | détecteur | **Attendu VERT** — c'est tout l'intérêt : ces cinq sont `True` sur `main` (M4). Un rouge préexistant contredirait M4 : halte et remontée, la substitution d'exemples de l'AC2/AC3 serait à rouvrir. | **(c) halte-et-remontée.** Un rouge après L2 signifie que le masque a mordu sur une vraie redirection. Arrêt. |
| **L5** — non-régression (cpp#38/#42/#128/#130/#154), bornes D1/D5, parité D6 | détecteur | **Attendu vert.** Un rouge préexistant sur `main` est une découverte, pas un test à corriger : halte et remontée avant toute modification. | **(c) halte-et-remontée.** **Interdit explicitement : ajuster, marquer `xfail`, ou réécrire un test cpp#130/#154** — l'AC2 exige qu'ils soient *nommés*, pas *réécrits*. |
| **L1/L2/L3** — scanner, branchement, veto | **pas des détecteurs** | Sans objet. | Sans objet : purement lexicaux, aucun état lu ni écrit, aucune base, aucun fichier. Le seul effet observable est la valeur de retour de `_denial_is_terminal`. |

**Aucun détecteur de ce plan n'écrit d'état, n'ouvre de ticket, ni ne notifie.**
Ce sont des tests `pytest`. La disposition unique est **(c) halte-et-remontée**,
avec la seule exception nommée du rouge *attendu et capturé* du rejeu 1 avant L2.

## Risques

- **R1 — « on rend non létale une commande qui écrit hors du worktree ».** Non :
  seul un `<`/`>` **en portée citée** est masqué, et bash n'y voit aucune
  redirection non plus. Une redirection réelle est hors quotes par définition,
  n'est pas masquée, et reste létale (L4 rejeu 2). Et dans tous les cas la
  commande reste **refusée** — aucun octet n'est écrit (D2).
- **R2 — « le masque cache un verbe dangereux entre guillemets ».** Il ne le
  peut pas : seuls deux caractères sont remplacés, jamais un mot. `echo 'rm -rf /'`
  reste létal, et c'est un test (L5, D1). C'est précisément ce que la variante
  « blanchir la région citée » aurait cassé.
- **R3 — scanner de guillemets incorrect sur une arête d'échappement.** Le sens
  du fail-closed borne le dommage : un guillemet non terminé ou mal analysé qui
  fait *sous-estimer* la région citée laisse le `>` visible → verdict de `main`
  → létal. Le seul dommage possible est une mort qui subsiste, jamais une
  létalité perdue à tort. L5 épingle les arêtes ; D6 épingle la parité.
- **R4 — divergence future entre les trois scanners.** Nommée en D6, épinglée
  par le test de parité de L5, et fichée en suivi. Ce plan ne prétend pas la
  fermer.
- **R5 — le correctif n'atteint pas la quatrième mort si la vraie cause était
  ailleurs.** Écarté par mesure : M1 montre que le segment porte la létalité
  **seul**, et M3 que `_denial_is_terminal` bascule `True → False` sur la chaîne
  intégrale de l'incident, avec `cwd` réel. Le contrôle négatif de la chaîne
  privée du segment (`False` sur `main`) exclut l'agrégation.

## Réconciliation corps ↔ plan (checkpoint Phase 2.5)

Le checkpoint de réconciliation a relevé **deux divergences de prémisse**, toutes
deux du même type : un exemple du corps contredit par la mesure de `main` à
`6d63ebb`. Elles sont détaillées en **M4**, et résolues par **édition du corps du
ticket** (chemin 1 du checkpoint), pas par une réinterprétation silencieuse :

1. **AC2** — `cmd >> fichier` est déjà non létal (cpp#154). Exemple remplacé par
   `echo a > ~/x` / `echo a > ../x`. Intention inchangée.
2. **AC3 contrôle 2** — `grep x > /tmp/out` est déjà non létal (cpp#154), ce qui
   rendait l'AC insatisfaisable. Contrôle porté par `grep x > /etc/y` et
   `echo a > $HOME/z`.

Aucune AC n'est renommée ni supprimée par cette réconciliation ; l'AC4 était
déjà supprimée par le re-scopage du corps, antérieur à ce plan.

Cette divergence est elle-même une donnée : le ticket a été re-scopé le
2026-09-05 sur une mesure prise **avant** que cpp#154 ne merge dans `main` la
veille. C'est la deuxième fois en deux jours que le checkpoint attrape une
prémisse de ce ticket — la première ayant été le diagnostic « létalité par
agrégation », lui aussi faux, lui aussi attrapé ici.

## Références

- `src/claude_pilot/tier1.py:203` — le motif `>` générique de `TIER3_PATTERNS`,
  aveugle aux guillemets. La cause racine.
- `src/claude_pilot/tier1.py:382` — `is_tier3_dangerous_for_lethality` et ses
  deux retraits existants (cpp#130, cpp#154) ; « ORDER IS LOAD-BEARING ».
- `src/claude_pilot/tier1.py:509` — `_split_compound_command`, sémantique POSIX
  de citation que L1 reprend.
- `src/claude_pilot/tier1.py:625` — `contains_unquoted_metacharacter`, le
  deuxième scanner, et son conservatisme de sens opposé (D5).
- `src/claude_pilot/permissions.py:727-768` — `_denial_is_terminal`, le
  consommateur unique de la létalité.
- `src/claude_pilot/permissions.py:1083` — `_redirect_destination_veto_reason`
  et son invariant, mis à jour par L3 (D4).
- `src/claude_pilot/permissions.py:922-968` — `_is_sanctioned_tmp_scratch` et la
  leçon « lexical, jamais résolu » (cpp#143).
- `tests/test_tier1.py:1180` / `:1232` — les deux classes de létalité à
  conserver telles quelles.
- `docs/plans/2026-09-04-002-fix-154-redirection-fichier-letalite-plan.md` — le
  plan de la veille, dont cpp#154 est la cause des deux divergences de M4.
- Incident : mort du pilote de mika#2179, 2026-09-05.
