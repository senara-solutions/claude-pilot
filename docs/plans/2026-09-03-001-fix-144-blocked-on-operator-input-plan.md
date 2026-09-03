---
issue: claude-pilot#144
title: Une session qui pose une question à un opérateur absent ne sort plus Success - Plan
type: fix
scope_repo: claude-pilot
priority: p1-important
date: 2026-09-03
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# Une session qui pose une question à un opérateur absent ne sort plus Success - Plan

## Goal Capsule

**Objectif.** Un pilote headless qui termine en posant une question à
l'opérateur sort `[done] Success` — mesuré sur 4 des 30 dernières sessions,
zéro livrable, aucune alerte. Le contrat se perd quand le modèle contourne le
refus de `AskUserQuestion` en rendant la question comme du texte terminal : le
SDK voit un tour propre et rend un `ResultMessage` de sous-type `success`.

**Moyens.** Marquer la session au moment structurel où elle existe déjà — le
`policy:deny` d'un `AskUserQuestion` — et ne consulter ce marqueur qu'à la
sortie, exactement sur la forme additive de `pr_created` (mika#940) et de
`rate_limited` (cpp#119) : un nouveau sous-type de `ResultJson`, jamais un
nouveau statut, jamais un champ requis.

**Hiérarchie d'autorité.** ACs du corps du ticket (AC1-AC5) > ce plan >
jugement de l'implémenteur. AC3 impose une mesure de faux-positifs sur
30 sessions de production ; ce plan ne peut pas la produire depuis ce dépôt de
travail (aucun accès à `/var/log/claude-pilot/`) et le dit explicitement au
lieu de fabriquer un chiffre — voir § Hors périmètre.

**Conditions d'arrêt.**
- S'arrêter si le correctif rend `Success` moins fiable pour une session qui
  a réellement livré. AC2 est un contrôle négatif obligatoire : un refus qui
  n'empêche pas la livraison ne doit jamais peser sur la sortie.
- S'arrêter si le correctif tente de deviner, depuis le TEXTE du tour final,
  qu'une question a été posée sans jamais appeler `AskUserQuestion`. C'est une
  heuristique non mesurée sur le corpus du ticket (AC3 le refuse
  explicitement) ; elle est nommée en dette, pas implémentée en dette
  silencieuse.
- S'arrêter si le correctif introduit un nouveau `Literal` de statut. `status`
  reste `"success" | "error" | "terminated"` ; seul `subtype` (déjà `str`
  libre) porte l'information neuve.

**Profil d'exécution.** Un dépôt, quatre fichiers source
(`src/claude_pilot/guardrails.py`, `src/claude_pilot/permissions.py`,
`src/claude_pilot/agent.py`, `src/claude_pilot/types.py`) et trois fichiers de
test. Séquentiel.

**Tail ownership.** PR sur `claude-pilot`, `Closes #144`.

## Product Contract

### Résumé

Le pilote a déjà, pour un problème structurellement identique
(`pipeline_incomplete`, mika#940), le bon patron : un flag sticky posé pendant
la session, consulté une seule fois — au `ResultMessage` — pour rebasculer un
`success` apparent vers un statut distinct. Ce ticket applique le même patron
à un signal différent : `AskUserQuestion` refusé par policy, plutôt que
l'absence de `gh pr create`.

### Le signal retenu, et pourquoi il est structurel et non heuristique

Deux options existent pour détecter « la session a fini sur une question » :

1. **Structurel** — la session a appelé `AskUserQuestion` et la policy l'a
   refusé (`[policy:deny] AskUserQuestion`, `permissions.py`). C'est un fait
   du protocole SDK, pas une lecture de texte : soit l'outil a été appelé et
   refusé, soit non. Zéro faux positif possible sur CE signal — un
   `AskUserQuestion` non refusé (tier1.5 auto-answer, relay `answer`) n'arme
   jamais le marqueur.
2. **Heuristique** — repérer, dans le texte du tour final, qu'il **contient**
   une question destinée à l'opérateur, y compris quand `AskUserQuestion`
   n'est jamais appelé (2 des 4 sessions du corps du ticket). Aucun signal SDK
   n'existe pour ça ; il faudrait pattern-matcher la prose du modèle.

Ce plan livre **uniquement (1)**. AC3 du ticket exige, pour toute heuristique,
un taux de faux positifs **mesuré sur les 30 sessions du corpus** cité dans le
corps (`/var/log/claude-pilot/*.stderr`, 2026-09-02). Ce dépôt de travail n'a
pas accès à ce répertoire — c'est un chemin serveur de production, pas un
artefact versionné. Écrire une heuristique texte sans cette mesure, c'est
exactement ce qu'AC3 refuse d'accepter comme preuve. Le choix est donc :
livrer le signal structurel, mesurable et déjà zéro-faux-positif par
construction, et **nommer** la moitié texte-seul comme non couverte plutôt que
de la couvrir avec un chiffre inventé.

**Conséquence directe : ce correctif couvre 2 des 4 sessions nommées dans le
corps du ticket** (`64a8cd19` et une des trois autres qui appellent
`AskUserQuestion` avant de le contourner en texte) — pas les deux qui posent
la question en texte pur sans jamais appeler l'outil. C'est une limite
connue, documentée ici et dans le corps de la PR, pas un point aveugle
découvert après coup.

### Le mécanisme

Sur la forme de `pr_created` (`guardrails.py:233`, mika#940) et de
`rate_limited` (`guardrails.py:405`, cpp#119) :

1. **Armement** (`permissions.py`, la branche `pd.decision == "deny"` qui gère
   déjà le fail-closed par défaut ET les refus par règle explicite) — quand
   `tool_name == "AskUserQuestion"`, appelle
   `guardrails.note_operator_question_denied(detail)`. `detail` est le même
   résumé déjà calculé pour `log_policy_deny` — aucun nouveau parsing.
   Sticky : un flag booléen `_operator_question_denied` plus le texte de la
   DERNIÈRE question refusée (`_operator_question_summary`), pour AC1
   (« la question posée reproduite dans le message de sortie »).
2. **Lecture** (`agent.py`, branche `ResultMessage`, juste avant le bloc
   `pipeline_incomplete` existant) — si `status == "success"` ET
   `guardrails.operator_question_denied` ET **pas**
   `guardrails.pr_created` (même signal que mika#940, pas un nouveau) :
   rebascule `subtype = "blocked_on_operator_input"`, `status = "error"`, et
   `termination_reason` reproduit la question refusée.

Le contrôle négatif d'AC2 tombe de la même condition qui protège déjà
`pipeline_incomplete` : `not guardrails.pr_created`. Un refus qui n'empêche
pas la livraison ne pèse jamais — le marqueur ne compte qu'à la sortie,
jamais en cours de session.

### Pourquoi réutiliser `pr_created` plutôt qu'un second signal « delivered »

`pr_created` est déjà le seul signal de livraison que ce dépôt sait produire
(détection `gh pr create` sur les blocs `Bash`, mika#940). Il porte la même
limite documentée dans son propre commentaire — un commit sans PR ne compte
pas comme livré, et `gh pr create --help` compte à tort. Ce plan hérite de
cette limite plutôt que d'en inventer une seconde définition de « livré » :
deux définitions divergentes de la même notion dans le même fichier seraient
pires que la limite unique et déjà documentée.

### Pourquoi `status="error"`, pas un nouveau `Literal`

`ResultJson.status` reste `Literal["success", "error", "terminated"]` —
inchangé. `subtype` est déjà un `str` libre (`types.py:183`), donc
`"blocked_on_operator_input"` s'ajoute sans toucher le schéma. C'est **exactement**
la forme de `pipeline_incomplete` (mika#940) : le docstring de `ResultJson`
documente déjà que « an added subtype reaches a human as text and nothing
else until a companion change lands downstream » — un consommateur qui ne
connaît que `status` voit un `"error"` déjà dans son vocabulaire ; un
consommateur qui inspecte `subtype` (comme `dispatch-lib`, `jq -r
'.subtype // empty'` opaque, cf. `types.py:1-8`) voit une nouvelle chaîne
qu'il peut choisir d'ignorer ou de router.

### Hors périmètre

- **La détection texte-seul (2 des 4 sessions du ticket).** Nommée ci-dessus,
  non implémentée — AC3 refuse une heuristique non mesurée sur le corpus, et
  ce dépôt n'a pas accès au corpus. Suivi possible : un marqueur dans le
  system prompt (« si tu dois poser une question à l'opérateur, appelle
  TOUJOURS AskUserQuestion, même si tu sais qu'il sera refusé ») déplacerait
  les deux sessions texte-seul vers le signal structurel — c'est un
  changement de prompt, pas de code, et mérite son propre ticket avec sa
  propre mesure avant/après.
- **Autoriser `AskUserQuestion` dans le pilote.** Exclu par le corps : le
  refus est correct, il n'y a personne pour répondre.
- **La cause du blocage sous-jacent** (`fatal: not a git repository`). Fichée
  côté mika, hors périmètre du corps.
- **Le garde-fou `idleTimeout`** (mika#2125) — ticket distinct.

## Acceptance criteria

**AC1** — Un `AskUserQuestion` refusé par `policy:deny` marque la session ; si
elle se termine ensuite sans commit ni PR, la sortie est un statut distinct
de `Success`, nommé pour ce qu'il est, avec la question posée reproduite dans
le message de sortie.
→ *Unité :* `guardrails.note_operator_question_denied` (armement,
`permissions.py`) + la branche de reclassification dans `agent.py` (lecture).
→ *Preuve :* `tests/test_permissions.py::test_denied_ask_user_question_marks_the_session`
(l'armement) et `tests/test_agent.py::test_operator_question_denied_with_no_deliverable_yields_blocked_status`
(la sortie : `status="error"`, `subtype="blocked_on_operator_input"`, la
question dans `termination_reason`).

**AC2** — Contrôle négatif obligatoire : une session qui prend un
`policy:deny` sur `AskUserQuestion`, poursuit, et livre un commit ou une PR
sort `Success` normalement.
→ *Unité :* la condition `not guardrails.pr_created` dans `agent.py` — le
marqueur ne pèse qu'en l'absence de livraison.
→ *Preuve :* `tests/test_agent.py::test_operator_question_denied_but_pr_created_still_reports_success`.

**AC3** — Nommer explicitement la détection retenue et son taux de faux
positifs mesuré sur le corpus des 30 sessions.
→ *Détection retenue :* le signal structurel SDK (`AskUserQuestion` refusé
par policy) — voir § Le signal retenu. **Taux de faux positifs : 0 par
construction** sur ce signal — un `AskUserQuestion` qui n'est PAS refusé
(tier1.5 auto-answer, ou une réponse `answer` du relay) n'arme jamais le
marqueur, et le marqueur ne pèse qu'à la sortie sous la condition AC2. Le taux
mesuré sur le corpus des 30 sessions n'est **pas produit par ce plan** — ce
dépôt de travail n'a pas accès à `/var/log/claude-pilot/`, qui est un chemin
serveur de production hors du checkout. Cette mesure appartient à l'opérateur
au déploiement (comparer les 30 sessions déjà journalisées à ce que la
nouvelle branche `blocked_on_operator_input` aurait rendu) et est signalée
comme telle dans le corps de la PR plutôt que remplacée par un chiffre
fabriqué.
→ *Couverture partielle assumée :* la moitié texte-seul du corpus (2 des 4
sessions) n'est PAS couverte par ce correctif — voir § Hors périmètre.

**AC4** — Test de régression sur les quatre transcriptions nommées (fixtures
figées) : les quatre rendent un statut non-`Success`, et au moins deux
sessions qui ont réellement livré rendent `Success`.
→ *Portée réduite, assumée :* sans accès aux transcriptions réelles de
`/var/log/claude-pilot/{64a8cd19,29250cb5,8aa4aa1d,1ccde033}*.stderr`, ce plan
ne peut pas rejouer les quatre séquences d'événements SDK exactes — les
fixtures « figées depuis les journaux » de cpp#145 (AC4 de ce ticket-là) ont
pu être construites parce que l'agent qui a écrit ce plan avait ce répertoire
sous la main ; celui-ci ne l'a pas. Ce que ce plan livre à la place :
- Une fixture synthétique reproduisant le MÉCANISME décrit dans le corps du
  ticket pour la moitié `AskUserQuestion`-appelé-puis-contourné (deny suivi
  d'un tour texte terminal, `ResultMessage` `success`) →
  `test_operator_question_denied_with_no_deliverable_yields_blocked_status`.
- Le contrôle positif symétrique (refus + livraison réelle → `Success`) →
  `test_operator_question_denied_but_pr_created_still_reports_success`.
- Le contrôle négatif sur le marqueur lui-même (aucune question refusée →
  `Success` inchangé, non-régression pure) →
  `test_genuine_success_without_operator_question_stays_success`.
Le rejeu sur les quatre transcriptions RÉELLES reste dû ; il appartient à
l'opérateur (accès au répertoire de logs) au moment du déploiement, avant de
fermer #144, pas à ce plan.

**AC5** — Le statut est lisible par l'appelant : il apparaît dans le code de
sortie ou la charge utile de rappel que le dispatcher consomme.
→ *Unité :* `exit_code = 1` (comme toute branche `status != "success"` déjà
en place) et `ResultJson.to_line()` — la même ligne JSON stdout que
`pipeline_incomplete` et `rate_limited` consomment déjà côté `dispatch-lib`
(`jq -r '.subtype // empty'`, opaque, `types.py:1-8`).
→ *Preuve :* même test AC1 — assertion sur `exit_code == 1` et sur la ligne
JSON stdout.

## Fire-Disposition

Les tests livrés par ce plan (`test_denied_ask_user_question_marks_the_session`,
`test_operator_question_denied_with_no_deliverable_yields_blocked_status`,
`test_operator_question_denied_but_pr_created_still_reports_success`,
`test_genuine_success_without_operator_question_stays_success`) s'exercent
tous sur des fonctions que cette PR introduit ou modifie
(`note_operator_question_denied`, la branche de reclassification dans
`agent.py`), avec des séquences de messages littérales du fichier de test —
aucun ne balaie un état préexistant du dépôt ni ne lit un journal à
l'exécution. **(c) halte-et-remontée** pour tout tir : un échec de
`test_operator_question_denied_but_pr_created_still_reports_success` (le
contrôle négatif d'AC2) en particulier ne reçoit aucune exception — il
signifierait que le correctif transforme des succès réels en échecs, ce
qu'AC2 interdit explicitement.

## Phases

### Phase 1 — Le marqueur sur le garde-fou

`src/claude_pilot/guardrails.py`. Sur la forme exacte de `_pr_created` /
`pr_created` (mika#940, `guardrails.py:161-237`) :

- `_operator_question_denied: bool = False`, `_operator_question_summary:
  str | None = None` dans `__init__`.
- Propriétés `operator_question_denied` / `operator_question_summary`.
- `note_operator_question_denied(summary: str | None) -> None` : arme le
  flag (sticky), écrase le résumé (la question la PLUS RÉCENTE refusée, pas
  la première — c'est celle la plus proche de ce que la session a fait
  ensuite).

### Phase 2 — L'armement dans permissions.py

`src/claude_pilot/permissions.py`, dans la branche `pd.decision == "deny"` de
`create_permission_handler` (celle qui gère déjà le fail-closed par défaut ET
les refus par règle explicite — PAS la branche `escalate`, qui halte déjà
inconditionnellement le run et ne peut donc jamais atteindre un
`ResultMessage` `success` naturel). Juste avant `return
_record_decision(...)` :

```python
if tool_name == "AskUserQuestion" and guardrails is not None:
    guardrails.note_operator_question_denied(detail)
```

`detail` est déjà calculé (`_summarize_input(tool_name, tool_input)`) pour
`log_policy_deny` — aucun nouveau code de résumé.

### Phase 3 — La lecture dans agent.py

`src/claude_pilot/agent.py`, branche `ResultMessage`, avant le bloc
`pipeline_incomplete` existant (mika#940, `agent.py:351-376`) :

```python
if (
    status == "success"
    and guardrails.operator_question_denied
    and not guardrails.pr_created
):
    subtype = "blocked_on_operator_input"
    status = "error"
    question = guardrails.operator_question_summary or "(unrecorded)"
    termination_reason = (
        "Session ended after an AskUserQuestion call was denied by policy "
        "(headless pilot, no operator present) and no 'gh pr create' Bash "
        f"call followed. Denied question: {question}"
    )
```

Placé AVANT le bloc `pipeline_incomplete` : les deux conditions peuvent
techniquement toutes deux être vraies (un pilote dev-pilot avec
`CLAUDE_PILOT_REQUIRE_PR=1` qui a aussi essuyé un refus de question) ; le
sous-type `blocked_on_operator_input` est le diagnostic le plus spécifique
des deux et gagne. `pipeline_incomplete` reste inchangé et continue de
couvrir son propre cas (gated `CLAUDE_PILOT_REQUIRE_PR=1`) quand le nouveau
bloc ne s'applique pas.

### Phase 4 — Le docstring

`src/claude_pilot/types.py`, `ResultJson` — ajouter l'entrée
`"blocked_on_operator_input"` à la liste des sous-types documentée
(`types.py:160-178`), sur le même format que les entrées `pipeline_incomplete`
et `rate_limited` déjà présentes.

### Phase 5 — Les preuves

- `tests/test_guardrails.py` : le marqueur démarre à `False` ; `note_operator_question_denied`
  l'arme et fixe le résumé ; il reste sticky à travers un tour ultérieur, et
  le résumé suit la question la PLUS RÉCENTE.
- `tests/test_permissions.py` : un `AskUserQuestion` refusé par le
  fail-closed par défaut arme le marqueur avec la question dans le résumé, et
  reste non-terminal (`interrupt=False`, cpp#128) ; un `Bash` refusé sur la
  même branche N'arme PAS le marqueur (garde de nom, miroir du garde
  `pr_created` de mika#940) ; `guardrails=None` ne casse rien.
- `tests/test_agent.py` (AC1/AC2/AC4-partiel, AC5) : trois tests intégration
  via `run_agent` + fake SDK stream, guardrail pré-armé par
  `note_operator_question_denied` (le seul point d'entrée réaliste — le canal
  `can_use_tool` n'est pas simulé par ces fakes) :
  1. refus + pas de PR → `status="error"`, `subtype="blocked_on_operator_input"`,
     question dans `termination_reason`, `exit_code==1`.
  2. refus + `gh pr create` observé avant le `ResultMessage` → `status="success"`
     inchangé (AC2).
  3. aucun refus → `status="success"` inchangé (non-régression du chemin
     existant).

## Commandes de vérification

```bash
uv run pytest tests/test_guardrails.py tests/test_permissions.py tests/test_agent.py tests/test_types.py -q
uv run pytest -q
uv run ruff check .
uv run mypy src
```

## Risques

| risque | mitigation |
|---|---|
| Le nouveau sous-type casse un consommateur aval qui switche sur `status` | `status` reste `"success" \| "error" \| "terminated"` — aucun `Literal` neuf ; le docstring de `ResultJson` documente déjà ce contrat pour `pipeline_incomplete` |
| Le marqueur transforme un vrai succès en échec | AC2 : condition `not guardrails.pr_created`, testée explicitement en négatif |
| Confusion avec `pipeline_incomplete` (même forme, gated différemment) | `blocked_on_operator_input` n'est PAS gated par `CLAUDE_PILOT_REQUIRE_PR` — c'est un défaut structurel, pas un contrat d'opt-in dev-pilot ; ordre de branchement documenté en Phase 3 |
| La moitié texte-seul du ticket (AC3/AC4, 2 des 4 sessions) reste non couverte | Nommée explicitement en § Hors périmètre et dans AC3/AC4 ci-dessus, pas laissée implicite ; suivi possible via marqueur system-prompt, ticket distinct |
| Mesure de faux-positifs AC3 non produite depuis ce dépôt | Nommée explicitement plutôt que fabriquée ; 0% par construction sur le signal structurel livré, mesure corpus déléguée à l'opérateur au déploiement |
