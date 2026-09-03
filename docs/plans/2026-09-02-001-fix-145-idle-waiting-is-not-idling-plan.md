---
issue: claude-pilot#145
title: Attendre n'est pas être inactif - Plan
type: fix
scope_repo: claude-pilot
priority: p1-important
date: 2026-09-02
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# Attendre n'est pas être inactif - Plan

## Goal Capsule

**Objectif.** Le watchdog tue des sessions qui travaillent. Les six sessions
tuées ont la même dernière ligne : un **résultat d'outil venait d'arriver**, puis
300 s de silence pendant que la session **attendait le modèle**. Attendre
quelqu'un d'autre n'est pas être inactif.

**Moyens.** Nommer la fenêtre d'attente comme un état distinct du silence, lui
donner son propre budget, et faire dire au message de mort ce qu'il a mesuré
**et** ce qu'il n'a pas vu. Sans jamais retirer au garde-fou sa capacité de tuer.

**Hiérarchie d'autorité.** ACs du ticket (AC1-AC6 du corps, AC7 ajouté au
grooming avec sa mesure) > ce plan > jugement de l'implémenteur.

**Conditions d'arrêt.**
- S'arrêter si le correctif rend le watchdog incapable de tuer. AC2 est un
  contrôle négatif obligatoire, et AC7 en porte un second : une session qui ne
  reprend **jamais** après un résultat d'outil doit rester tuable. Un garde-fou
  qui ne tue plus n'est pas réparé, il est retiré.
- S'arrêter si le correctif se réduit à relâcher `idleTimeoutMs`. Le corps
  l'exclut explicitement (« axe 3 seul » est hors périmètre) : relâcher un seuil
  pour compenser une heuristique fausse masque le défaut.
- S'arrêter si l'implémenteur **ré-ajoute** le comptage du flux. Il est déjà
  livré (cpp#123/#125) et n'a sauvé aucune des six.

**Profil d'exécution.** Un dépôt, deux fichiers :
`src/claude_pilot/guardrails.py` (l'état d'attente, le budget, le message) et
`src/claude_pilot/agent.py` (l'endroit qui sait qu'un résultat d'outil vient de
partir). Séquentiel.

**Contrainte de livraison — pas un prérequis.** `claude-pilot` n'est pas dans
`DISPATCHABLE_REPOS` (`mika/crates/mika-agent/src/webhook_dispatch.rs:102-107`) :
ce ticket **ne partira pas** par la boucle autonome. Il s'implémente en mode 2 de
CLAUDE.md — `/mika` interactif dans ce dépôt. Élargir cette liste est une
décision de périmètre de sécurité qui appartient à l'opérateur ; **ce plan n'en
dépend pas et ne la demande pas.** S'y ajoute mika#2141 : depuis le 2026-08-04
aucun pilote ne peut commiter, donc aucun dispatch autonome n'est à attendre de
toute façon.

**Tail ownership.** PR sur `claude-pilot`.

## Product Contract

### Résumé

Le watchdog possède un seul compteur — « rien vu depuis N secondes » — et trois
choses très différentes le nourrissent : le modèle est muet, un outil s'exécute,
le modèle réfléchit avant son premier jeton. La troisième a tué six sessions.
On la sépare des deux autres, on lui donne un budget nommé, et on rend le
message de mort honnête.

### La mesure, et ce qu'elle corrige aux axes du ticket

Les trois dernières lignes hors flux de chaque session tuée
(`/var/log/claude-pilot/*.stderr`) :

| session | dernière ligne avant `[guardrail]` |
|---|---|
| `aae80d84`, `1bb1fa9c`, `c2f35431`, `bbfff9ec`, `9b13ee7f`, `3d5fe1ec` | `[debug] user message (tool result) received` |

**n=6, unanime. Jamais un `[tool:request]` resté sans réponse.** Le résultat
d'outil était arrivé, `note_activity()` avait repoussé l'échéance — puis rien.
Sur `aae80d84`, l'outil était un `Edit` (instantané) auto-approuvé ; ce qui a
duré 300 s, c'est le **premier jeton du tour suivant**.

Trois conséquences, qui changent le travail :

1. **L'axe 1 du corps est déjà livré.** `note_stream_activity`
   (`guardrails.py:171`, cpp#123) et `note_activity` (`:203`, cpp#125) repoussent
   l'échéance ; `agent.py:222` et `:247` les câblent sur `StreamEvent` et
   `UserMessage`. Le ré-implémenter ne changerait rien. **AC1 est donc un test de
   non-régression sur du code en place, pas du travail neuf** — et le plan le dit
   pour qu'un implémenteur ne « répare » pas ce qui fonctionne.
2. **L'axe 2 n'est le mécanisme d'aucune des six.** La fenêtre entre la décision
   de permission et le résultat d'outil est un trou réel — rien n'y réarme, et
   `pause_idle_timer` ne couvre que l'aller-retour du relais
   (`permissions.py:1112`/`:1177`), donc pas les outils auto-approuvés ni leur
   exécution. Mais dans les six cas l'outil avait déjà rendu : **AC3 est dû, il
   ne porte pas le correctif.**
3. **Le trou létal est la fenêtre « résultat d'outil → premier jeton »**, où la
   session attend le modèle. C'est AC7.

### Le nom du défaut

Le watchdog ne mesure pas l'inactivité : il mesure **l'absence d'un signal qu'il
attend**. Trois états distincts produisent ce silence —

| état | qui l'on attend | doit tuer ? |
|---|---|---|
| le modèle est muet (fin de génération, rien ne suit) | personne | **oui**, c'est le cas d'origine |
| un outil s'exécute (`cargo build` de six minutes) | l'outil | non — AC3 |
| le tour suivant n'a pas encore produit son premier jeton | le modèle | non — AC7 |

— et un seul compteur les confond. Le correctif ne relâche rien : il **rend les
trois distinguables**, puis n'en tue qu'un aux 300 s d'origine.

### Pourquoi un budget nommé plutôt qu'une exemption

Une exemption pure (« pendant l'attente, le compteur ne tourne pas ») rendrait la
session immortelle si le modèle ou l'outil ne revient jamais — précisément le
zombie que `rateLimitCeilingMs` (cpp#133) existe pour éviter sur l'axe du
throttling. On applique la **même forme** que cpp#133, qui est déjà la doctrine
du fichier : l'attente a son propre plafond, plus généreux que 300 s, et son
propre motif d'arrêt. Le contrôle négatif d'AC7 tombe alors naturellement — une
session qui ne reprend jamais meurt au plafond, avec un motif qui dit qu'elle
attendait.

### Hors périmètre

- **La chaîne de permissions et le chain-veto.** Le corps l'écarte, mesure à
  l'appui : `c5201301` est tuée avec **zéro** refus et `3d5fe1ec` en fait cinq en
  travaillant le plus. Les refus ne sont pas la variable.
- **mika#2121** (`callback_delivered_without_pr_url`) et **mika#2108** (bac à
  sable sans gitdir parent) — distincts, nommés par le corps.
- **mika#2141** — depuis le 2026-08-04 aucun pilote ne peut commiter. Cause
  distincte, ticket distinct ; elle explique pourquoi ce correctif ne suffira pas
  seul à rendre la boucle productive, elle ne le remplace pas.
- **Relâcher `idleTimeoutMs`** (axe 3 seul). Exclu par le corps. Le seuil de
  300 s reste **inchangé** pour le silence vrai ; seuls les états nouvellement
  distingués reçoivent un budget propre.
- **Élargir `DISPATCHABLE_REPOS`.** Décision de périmètre de sécurité, hors
  grooming (voir § Contrainte de livraison).

## Acceptance criteria

Transcrits depuis le corps de claude-pilot#145 — AC1-AC6 de l'auteur, AC7 ajouté
au grooming avec sa mesure — chacun avec l'unité d'implémentation qui le
satisfait et l'artefact qui le prouve.

**AC1** — L'heuristique de progrès est révisée : le flux de contenu et
l'avancement de tour comptent comme progrès. Tests unitaires sur les deux
signaux, séparément.
→ *Unité :* **aucune ligne neuve** — livré par cpp#123 (`guardrails.py:171`,
`agent.py:222`) et cpp#125 (`guardrails.py:203`, `agent.py:247`). L'unité est le
verrou de non-régression.
→ *Preuve :* tests 5.1a (un `StreamEvent` de progrès repousse l'échéance) et
5.1b (une `UserMessage` la repousse sans incrémenter le compteur de flux), écrits
séparément comme l'AC l'exige. Ces deux tests **passent sur `main`** : c'est
attendu, et le plan le dit — voir § Preuve de non-vacuité.

**AC2** — **Contrôle négatif obligatoire.** Une session réellement inactive —
aucun flux, aucun tour, aucun outil — doit **toujours** être tuée à l'échéance.
→ *Unité :* la branche `idle_timeout` d'origine est laissée intacte pour cet
état ; le seuil de 300 s ne bouge pas.
→ *Preuve :* test 5.2, qui doit être **vu passer au rouge** quand on neutralise
le seuil — la manipulation est décrite dans le test, comme l'AC l'exige.

**AC3** — Le temps passé en attente d'un outil non résolu n'alimente pas le
compteur d'inactivité. Test avec un outil dont la réponse tarde au-delà du seuil.
→ *Unité :* Phase 2 — état `AwaitingTool`, armé à l'émission du `tool_use`,
désarmé à la `UserMessage` correspondante.
→ *Preuve :* test 5.3 — outil émis, aucun événement pendant `2 × idleTimeoutMs`,
session vivante ; et son contrôle négatif, l'outil qui ne revient jamais, tuée au
plafond avec le motif d'attente.

**AC4** — Rejeu sur fixtures figées : les cinq sessions nommées, avec leurs
comptes de flux et d'outils réels, ne doivent **pas** être tuées par
l'heuristique révisée. `c5201301` (72 événements, 2 outils) est le cas limite à
trancher explicitement.
→ *Unité :* Phases 2 et 3.
→ *Preuve :* test 5.4, fixtures **figées dans le fichier** — séquences
d'événements reconstruites depuis les journaux, pas relues à l'exécution (les
journaux tournent). **Tranchage explicite de `c5201301` :** elle est **sauvée**,
et pour la même raison que les quatre autres — sa dernière ligne est aussi
`user message (tool result) received`. Ses 72 événements et 2 outils la font
paraître inerte, mais l'inertie n'est pas ce qui l'a tuée : c'est la même fenêtre
d'attente. Une session peu productive qui attend le modèle attend quand même.

**AC5** — Le message du garde-fou, quand il tue, nomme ce qu'il a mesuré **et**
ce qu'il n'a pas vu.
→ *Unité :* Phase 4 — le message porte le motif (silence vrai / attente d'outil /
attente du modèle), le **temps depuis le dernier signal**, la **nature de ce
dernier signal**, et le compteur de flux **de la fenêtre** en plus du cumul de
session.
→ *Preuve :* test 5.5 — assertions positives sur les quatre éléments, et
assertion **négative** : le message ne dit plus « No meaningful progress »
lorsqu'un signal a été observé dans la fenêtre.

**AC6** — Mesure post-correctif : proportion de sessions terminées par
`idle_timeout`, **par session terminée**, avant et après.
→ *Unité :* Phase 6 — un script de comptage versionné, pas une commande de
console.
→ *Preuve :* `scripts/measure-idle-lethality.sh` rendant `tuées / terminées` sur
une fenêtre de journaux, avec la mesure de référence **avant** consignée dans la
PR : 5/10 le 2026-09-01, sessions tuées à 21 appels d'outils en moyenne contre
13,8 pour les survivantes. Un compte brut est explicitement refusé par l'AC.

**AC7** *(ajouté au grooming, corps mis à jour le 2026-09-02)* — Le temps écoulé
entre la remise d'un résultat d'outil et le premier événement de flux du tour
suivant n'est pas compté comme inactivité, ou l'est sous un budget distinct et
explicitement nommé. Rejeu sur la trace d'`aae80d84`. Contrôle négatif : une
session qui ne reprend jamais doit rester tuable.
→ *Unité :* Phase 3 — état `AwaitingModel`, armé à la `UserMessage`, désarmé au
premier `StreamEvent` de progrès, plafonné par `modelWaitCeilingMs`.
→ *Preuve :* tests 5.6a (trace d'`aae80d84` rejouée : vivante) et 5.6b (jamais
de reprise : tuée au plafond, motif `awaiting_model`, **pas** `idle_timeout`).

## Fire-Disposition

Ce plan livre des **détecteurs** — les tests de la phase 5 — dont trois gardent
un contrat de préservation : 5.2 (le watchdog tue encore), 5.3-négatif et
5.6b (les plafonds tuent encore). Par le Fire-Disposition Gate (mika#1574), la
disposition se déclare contre le schéma canonique : **(a) exception nommée**,
**(b) livré désactivé**, **(c) halte-et-remontée**.

**Le tir au déploiement est structurellement impossible.** Chaque détecteur
s'exerce sur une fonction que cette PR introduit ou modifie, avec des séquences
d'événements littérales du fichier de test ; aucun ne balaie un état préexistant
du dépôt ni ne lit un journal à l'exécution. Il n'existe donc pas de classe
« violation héritée » capable de faire échouer une PR sans rapport.

- **5.1a, 5.1b, 5.3, 5.4, 5.5, 5.6a (comportement attendu) → (c)
  halte-et-remontée.** Un tir prouve que le correctif ne fait pas ce qu'il
  annonce, ou — pour 5.1 — qu'une régression a été introduite dans du code
  livré. On corrige le code, jamais le test.
- **5.2 (contrôle négatif d'AC2) → (c) halte-et-remontée, sans exception
  possible.** Un tir signifie que le watchdog ne tue plus une session vraiment
  inactive : le garde-fou aurait été retiré, pas réparé. Aucune allowlist n'est
  offerte ; une exception ici viderait AC2 de son sens.
- **5.6b et le contrôle négatif de 5.3 (les plafonds) → (c) halte-et-remontée.**
  Un tir signifie qu'un état d'attente est devenu immortel — le zombie que
  cpp#133 a établi qu'il ne fallait pas créer.

**Aucun détecteur n'est livré désactivé (b) ni ne porte d'allowlist (a) :** leur
domaine est le diff de cette PR, donc un tir désigne toujours un défaut du
correctif, jamais un héritage.

## Phases

### Phase 1 — Nommer les trois états

`src/claude_pilot/guardrails.py`.

**1.1** Un état d'attente explicite, porté par le garde-fou :

```python
class _WaitState(Enum):
    """Ce que la session attend pendant un silence (cpp#145).

    Le watchdog ne mesure pas l'inactivité : il mesure l'absence d'un signal.
    Trois causes produisent ce silence et une seule justifie l'arrêt à
    `idleTimeoutMs`. Les confondre a tué six sessions productives dans la nuit
    du 2026-08-31 au 09-01 — toutes juste après un résultat d'outil.
    """
    IDLE = "idle"                      # personne n'est attendu → seuil d'origine
    AWAITING_TOOL = "awaiting_tool"    # un outil s'exécute (AC3)
    AWAITING_MODEL = "awaiting_model"  # le tour suivant n'a pas produit (AC7)
```

**1.2** Deux plafonds, sur la forme déjà établie par `rateLimitCeilingMs`
(cpp#133) : `toolWaitCeilingMs` et `modelWaitCeilingMs` dans
`ResolvedGuardrailConfig` et `GUARDRAIL_DEFAULTS` (`types.py`), avec valeur par
défaut et commentaire disant ce qu'ils bornent. **`idleTimeoutMs` reste à
300 000** — le corps du ticket exclut de le relâcher, et l'état `IDLE` continue
de l'utiliser tel quel.

**1.3** Le watchdog consulte l'état à l'échéance : `IDLE` → arrêt inchangé ;
`AWAITING_*` → ne pas tuer, ré-examiner à la fenêtre suivante, et n'arrêter
qu'au plafond correspondant, avec un motif distinct. C'est **exactement** la
boucle `while` de `_idle_watchdog` déjà écrite pour `rate_limited`
(`guardrails.py:495-512`) : on étend une forme existante, on n'en invente pas.

### Phase 2 — L'attente d'outil (AC3)

**2.1** `note_tool_requested(tool_name)` : passe l'état à `AWAITING_TOOL` et
ancre l'instant. Appelée depuis `agent.py` là où le `tool_use` est observé —
**pas** depuis `permissions.py`, dont le rappel ne couvre ni les outils
auto-approuvés hors relais ni la durée d'exécution
(`permissions.py:1112`/`:1177` n'entourent que l'aller-retour du relais).

**2.2** `note_activity()` (la `UserMessage` porteuse du résultat) désarme
`AWAITING_TOOL`. Elle existe déjà ; elle gagne la transition d'état.

### Phase 3 — L'attente du modèle (AC7, le trou létal)

**3.1** `note_activity()` ne se contente plus de repousser l'échéance : elle
passe l'état à `AWAITING_MODEL`. C'est la ligne exacte des six journaux — le
résultat d'outil est remis, et la session attend le tour suivant.

**3.2** `note_stream_activity()` — le premier événement de progrès du tour
suivant — remet l'état à `IDLE`. La fenêtre se ferme sur la preuve que le modèle
a repris.

**3.3** À l'échéance en `AWAITING_MODEL`, ne pas tuer avant
`modelWaitCeilingMs`, puis arrêter avec le motif `awaiting_model` — **jamais**
`idle_timeout`, pour que `dispatch-lib` et l'opérateur lisent « le modèle n'a
jamais répondu », pas « la session s'est tue ». Même discipline que
`_abort_rate_limit_ceiling` (`guardrails.py:526`).

### Phase 4 — Le message honnête (AC5)

Le message actuel dit « No meaningful progress for 300s » en citant 3844
événements de progrès. Il gagne : le **motif** (lequel des trois états), le
**temps depuis le dernier signal**, la **nature de ce dernier signal**
(`tool result` / `stream event` / `turn boundary`), et le compteur **de la
fenêtre** en plus du cumul de session. Le cumul reste — il est utile — mais il
cesse d'être la seule chose citée à côté du mot « aucun ».

### Phase 5 — Les preuves

Dans `tests/`, sur la convention du dépôt.

- **5.1a / 5.1b (AC1, non-régression).** Un `StreamEvent` de progrès repousse
  l'échéance ; une `UserMessage` la repousse **sans** incrémenter
  `stream_activity_count`. Deux tests séparés, comme l'AC l'exige.
- **5.2 (AC2, contrôle négatif).** Aucun signal : tuée à l'échéance, motif
  `idle_timeout`. Le test documente la manipulation qui doit le faire rougir
  (neutraliser le seuil), comme l'AC le demande.
- **5.3 (AC3).** Outil émis, aucun événement pendant `2 × idleTimeoutMs` :
  vivante. **Contrôle négatif attenant :** l'outil qui ne revient jamais est tuée
  à `toolWaitCeilingMs`, motif `awaiting_tool`.
- **5.4 (AC4).** Les cinq sessions, séquences **figées dans le fichier**,
  reconstruites depuis les journaux — jamais relues à l'exécution, les journaux
  tournent. `c5201301` incluse, avec en commentaire la raison de son sauvetage.
- **5.5 (AC5).** Quatre assertions positives sur le message, une négative : plus
  de « No meaningful progress » quand un signal a été vu dans la fenêtre.
- **5.6a / 5.6b (AC7).** La trace d'`aae80d84` rejouée : vivante. Jamais de
  reprise : tuée à `modelWaitCeilingMs`, motif `awaiting_model` et **pas**
  `idle_timeout`.

### Phase 6 — La mesure (AC6)

`scripts/measure-idle-lethality.sh` : sur une fenêtre de journaux, rend
`tuées_par_idle / sessions_terminées` — un **taux**, l'AC refusant explicitement
un compte brut. Le comptage des appels d'outils utilise le marqueur validé,
`user message (tool result) received`, et **non** `[tool]` : le corps du ticket
documente que ce dernier a produit une conclusion fausse, depuis retirée. La
mesure de référence avant correctif (5/10 ; 21 vs 13,8 appels) est consignée dans
le corps de la PR.

### Preuve de non-vacuité

Le correctif n'est pas vide si, et seulement si, la suite **échoue sur `main`** :
5.3, 5.4, 5.5, 5.6a et 5.6b doivent y échouer. **5.1a, 5.1b et 5.2 doivent y
passer** — et c'est le point : 5.1 atteste du code déjà livré par cpp#123/#125
(donc vert des deux côtés, par construction et non par vacuité), et 5.2 prouve
que le contrôle négatif contrôlait déjà quelque chose avant qu'on y touche. Un
plan qui prétendrait faire échouer 5.1 sur `main` se tromperait sur ce qu'il
répare.

### Ancrages vérifiés (contrôle de confiance, 2026-09-03)

Relevé sur la branche à `2b57f5f`. Les citations du plan se vérifient toutes —
`types.py:60` (`idleTimeoutMs=300_000`), `guardrails.py:171` / `:203`
(`note_stream_activity` / `note_activity`), `agent.py:222` / `:247` (leur
câblage), `guardrails.py:304` (`has_tool_use`), la boucle `while` de
`_idle_watchdog` et `_abort_rate_limit_ceiling`. Trois précisions que la lecture
du code ajoute, et qu'un implémenteur paierait cher à redécouvrir :

**A. Le site d'armement d'`AWAITING_TOOL` existe déjà.** Phase 2.1 dit « depuis
`agent.py` » pour écarter `permissions.py` — la raison est juste, la coordonnée
est à affiner. `agent.py` ne parcourt pas les blocs : il passe l'`AssistantMessage`
à `SessionGuardrails.on_assistant_message`, qui calcule `has_tool_use` à
`guardrails.py:304`. **Armer là**, sur le booléen déjà calculé, plutôt que
d'ajouter un second parcours de blocs dans `agent.py`. L'exclusion de
`permissions.py` (`:1112`/`:1177`, aller-retour du relais seulement) reste
intégralement valable — c'est elle qui porte l'intention.

**B. Deux `Literal` à étendre, sinon `mypy` casse la porte qualité.** Les motifs
d'arrêt sont typés en deux endroits, et `awaiting_tool` / `awaiting_model`
doivent apparaître dans les deux :
- `types.py:178-180` — `GuardrailAbortReason.guardrail`
- `guardrails.py:547-549` — le paramètre `guardrail` d'`_abort`

L'ajout est **additif**, exactement comme `rate_limited` l'a été en cpp#119 : le
commentaire de `types.py:172-177` documente déjà que les consommateurs qui ne
connaissent que les valeurs d'origine continuent de parser la forme JSON. C'est
la garantie sur laquelle s'appuie la dernière ligne de la table des risques.

**C. `_reset_idle_timer` annule et recrée la tâche.** `on_assistant_message`
l'appelle à `guardrails.py:381` — ce n'est pas un `_bump_idle_deadline`, la
tâche watchdog est détruite puis reconstruite. La transition d'état doit donc
être posée de manière à ce que la **tâche neuve** la lise : l'état vit sur
l'instance, pas dans la closure de la tâche. Poser l'état avant que la tâche ne
reparte, et ne jamais supposer qu'une tâche en vol observera un changement
décidé après son armement.

### Correction de la prémisse (revue du 2026-09-03)

Le grooming et ce plan affirment : « **n=6, unanime. Jamais un `[tool:request]`
resté sans réponse.** » La première moitié est fausse, et la revue multi-agents
l'a attrapée avant le merge. Relevé sur les journaux :

| session | trois dernières lignes |
|---|---|
| `c56a973e`, `c5201301`, `aae80d84` | trailers, **puis** le résultat d'outil |
| `3d5fe1ec`, `f26add11`, `e2f0ef97` | résultat d'outil, **puis** `message_delta` / `message_stop` |

Dans ces trois-là, le SDK délivre les trailers de fin du tour **après** le
résultat d'outil. Ils appartiennent à `_PROGRESS_STREAM_EVENT_TYPES`, donc une
règle « tout événement de progrès signifie que le modèle a repris » ferme la
fenêtre d'attente à l'instant précis où elle doit s'ouvrir : **la moitié des
sessions que ce ticket existe pour sauver seraient mortes quand même**, avec en
prime un message affirmant « nobody outstanding ».

Deuxième correction, sur l'axe 2. Le plan écrit que la fenêtre outil « n'est le
mécanisme d'aucune des six ». C'est exact pour la mort de ces six sessions, mais
la conclusion qu'on en tirait — tenir l'attente d'outil dans un état scalaire —
ne l'est pas : sur **177 paires réelles dépêche→résultat**, **67 événements de
production arrivent pendant qu'un outil est encore en vol**. Génération et
exécution d'outil se chevauchent sur le fil. Un état scalaire est donc effacé en
plein vol par de la génération ordinaire, et `toolWaitCeilingMs` devient une
configuration morte — un bouton qui se lit comme une protection et n'en est pas
une.

**Ce que la livraison fait donc, et que le plan ne prescrivait pas :** les
trailers ne prétendent jamais que le modèle a repris, et les outils en vol sont
**comptés** (`_pending_tool_uses`) plutôt qu'énoncés. Les AC ne bougent pas ;
c'est leur mécanisme qui est corrigé.

**Leçon de méthode, gardée exprès.** La prémisse venait d'une trace unique citée
dans le ticket, généralisée à six sans être vérifiée sur les six. Le plan a
ensuite gravé cette généralisation en commentaire de code. La revue a coûté six
agents ; la vérification aurait coûté un `grep`.

## Commandes de vérification

```bash
uv run pytest tests/ -k guardrail
uv run pytest tests/
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
```

## Risques

| risque | mitigation |
|---|---|
| Un état d'attente devient immortel | Plafonds `toolWaitCeilingMs` / `modelWaitCeilingMs` sur la forme cpp#133 ; tests 5.3-négatif et 5.6b |
| Le watchdog ne tue plus rien | AC2 intact, seuil `idleTimeoutMs` inchangé pour l'état `IDLE` ; test 5.2 |
| L'implémenteur ré-ajoute le comptage du flux (déjà livré) | Dit trois fois : Conditions d'arrêt, § La mesure, AC1 ; et 5.1 passe sur `main` |
| `AWAITING_TOOL` armé au mauvais endroit (relais) et manquant les outils auto-approuvés | Phase 2.1 impose `agent.py`, avec la raison et les lignes de `permissions.py` qui l'excluent |
| Fixtures AC4 relues à l'exécution alors que les journaux tournent | Séquences figées dans le fichier de test, exigé par AC4 et répété en 5.4 |
| Le nouveau motif casse un consommateur aval | `awaiting_model` et `awaiting_tool` sont des motifs **nouveaux** ; `idle_timeout` garde sa forme, donc les classificateurs existants de `dispatch-lib` continuent de matcher ce qu'ils matchaient |
