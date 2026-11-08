---
title: "fix(agent): une session tuée par l'expiration du jeton OAuth (401) est reprise après une attente bornée (#321)"
type: fix
status: done
date: 2026-11-08
origin: senara-solutions/claude-pilot#321
---

# Reprise après un 401 d'expiration (#321)

## Contexte

Le jeton OAuth de l'hôte vaut 8 h, et le proxy d'egress l'injecte côté hôte à
chaque requête. Entre son expiration et le renouvellement du fichier hôte,
chaque requête du pilote reçoit un 401 et la session s'arrête. Depuis #319,
cette mort sort en `status: error`, `subtype: api_error`, mais le vol est
perdu. Les renouvellements mesurés tombent 12 s et 3 min 15 s après le 401.

La reprise d'une session existe déjà (cpp#151) : un nouveau client avec
`resume=<session id>`. Ce ticket l'étend au 401, sous un budget séparé.

## Forme réelle d'une mort sur 401

Dans les transcripts, la mort sur 401 suit toujours la même séquence. D'abord,
un `AssistantMessage` synthétique (`model: "<synthetic>"`,
`error: "authentication_failed"`, un seul bloc texte). Ensuite, le
`ResultMessage` de #319 (`subtype: "success"`, `is_error: true`,
`api_error_status: 401`).

## Décisions

### D1. Un second contrôleur, `_AuthResumeController` (AC1, AC2, AC4)

Il suit le modèle de `_DenyResumeController`, avec son propre budget.
`should_resume` n'admet que la forme de #319 avec `api_error_status == 401`.
Il exclut aussi tout ce qui est exclu dans cpp#151 :
- `terminal_policy_deny` ;
- `terminal_reason` dans `ABORT_TERMINAL_REASONS` ;
- l'absence de `session_id` ;
- un budget épuisé ;
- un épisode clos.

Le bras s'arme au même endroit que celui de cpp#151, avant `close_final_turn`,
et finit lui aussi par un `break`.

**Un épisode par run.** L'épisode se clôt au premier message assistant réel, et
non synthétique, d'une session reprise. Un 401 qui vient ensuite n'est pas
repris.

**Attente aveugle.** Avant chaque reprise, la boucle de session attend
`AUTH_RESUME_WAIT_SECONDS` (60 s), au plus `AUTH_RESUME_MAX_ATTEMPTS` (10) fois.
Le fichier d'identifiants n'est jamais lu. L'attente passe par
`_auth_resume_sleep`, injectable : la suite de tests ne dort pas.

**Minuterie d'inactivité suspendue.** Elle est arrêtée pendant l'attente
(`pause_idle_timer` / `resume_idle_timer`, comme pour le relais). Les 10
minutes d'attente ne comptent donc pas contre `idleTimeoutMs`.

**Journal.** Chaque tentative écrit une ligne avec son rang et le budget
(`ui.log_auth_resume`).

**Message de reprise.** `AUTH_RESUME_NUDGE` est distinct de
`DENY_RESUME_NUDGE`. Il ne dit pas que le jeton a été renouvelé, puisque
l'attente est aveugle.

### D2. Le message synthétique ne compte pas comme un tour

Un `AssistantMessage` avec `error == "authentication_failed"` n'est plus passé
à `guardrails.on_assistant_message`. Sans cette exclusion, cinq tentatives
ratées suffisent à armer `stall_detected` (cinq tours sans appel d'outil, seuil
par défaut), bien avant la fin du budget. Un test le montre : sans l'exclusion,
le garde-fou s'arme à la 5e tentative. Le message reste journalisé et écrit au
transcript.

### D3. Budget épuisé (AC3)

Le 11e 401 passe par le bras de #319 : `status: error`, `subtype: api_error`,
sortie 1. Son `termination_reason` ajoute `resume attempts after 401: 10/10`.

Si le client repris ne s'ouvre pas, la boucle s'arrête comme dans cpp#151, et la
mort différée est rapportée en `api_error` avec le nombre de tentatives.
`last_resume` dit quel contrôleur a armé la dernière reconnexion. On rapporte
donc la bonne mort différée.

### D4. Tests (AC5)

Le premier test couvre, dans le même appel, quatre cas sur la forme réelle du
SDK :
- un 401 suivi d'une reprise saine donne `success`, avec une reprise
  journalisée, une attente de 60 s et `resume=sess_test` ;
- un 401 à chaque tentative donne `api_error`, sortie 1, après 10 tentatives et
  11 clients ;
- un 502 n'est pas repris ;
- un 401 après un refus terminal n'est pas repris.

Tests frères :
- une interruption (`aborted_tools`) n'est pas reprise ;
- un seul épisode par run ;
- les budgets sont séparés : avec `CLAUDE_PILOT_MAX_DENY_RESUMES=0`, le 401 est
  quand même repris ;
- `stall_detected` ne s'arme pas pendant 10 tentatives ;
- un client repris qui ne s'ouvre pas sort en `api_error` ;
- le message de reprise est distinct et n'affirme rien sur le jeton.

Contre-épreuve : sur main, 6 de ces 7 tests échouent. Le 7e est le contrôle
négatif.

### D5. Sonde (AC6)

Hors code : sur les 14 jours qui suivent le deploy, sur le ticket.

## Limites

- Chaque session reprise ouvre une nouvelle boucle de requête avec son propre
  `maxTurns`, comme dans cpp#151. Une seule reprise par run peut aboutir à une
  session qui travaille (un seul épisode). Le plafond effectif de tours
  augmente donc d'au plus un `maxTurns`.
- Le cas de #319 testé avec `_FakeClient` (même script à chaque client) passe
  désormais par 10 reprises avant de sortir en `api_error` : le classement reste
  celui de #319.

## Hors périmètre

- Le renouvellement du jeton, le proxy d'egress, le montage du bac à sable.
- La garde de durée côté dispatch : en lecture seule, `dispatch-lib.sh` n'a
  aucune garde de durée autour de l'appel à claude-pilot.
- La levée du palliatif humain, après la sonde AC6.
- Aucun ticket ouvert (gel jusqu'au 01/12).

## Acceptance criteria

- AC1 : un 401 ne termine pas le run. On attend, puis on reprend la même session
  avec un message propre. Une reprise saine sort en `success`.
- AC2 : l'attente est aveugle, à intervalle fixe de 60 s, au plus 10 fois.
  Chaque tentative est journalisée, et un seul épisode est repris par run.
- AC3 : budget épuisé, la sortie est `api_error`, code 1, et le
  `termination_reason` nomme les tentatives.
- AC4 : seul le 401 est repris. Les exclusions de cpp#151 tiennent, et les
  budgets sont séparés.
- AC5 : les tests utilisent la forme réelle du SDK, avec les quatre cas dans le
  même appel. L'attente est injectable.
