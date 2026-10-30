---
title: "fix(agent): un message utilisateur sans résultat d'outil est nommé au journal (cpp#184, résidu du défaut 1)"
type: fix
status: active
date: 2026-10-30
issue: senara-solutions/claude-pilot#184
---

# Un message utilisateur sans résultat d'outil est nommé au journal

## Pourquoi

cpp#311 journalise chaque appel d'outil (`[tool:use]`). Relu à la source sur les 22 pilotes
lancés depuis son déploiement (2026-10-29), le journal apparie presque tout : chaque
`[tool:use]` a son `user message (tool result) received`. Il reste **40 lignes « tool result »
sans aucun appel en attente**, de 1 à 5 par pilote, dans 20 pilotes sur 22.

Elles ont toutes la même forme :

- elles arrivent 111 à 252 s après le résultat précédent, sans aucun événement de flux entre
  les deux ;
- le tour suivant du modèle démarre environ 1,5 s après elles.

Exemple, pilote d368a2a0 : un `Edit` rend son résultat à 14:28:36.761Z. Une seconde ligne
« tool result » arrive à 14:30:55.707Z, et le `message_start` suivant à 14:30:57.328Z. Le
transcript mika de la session (`AssistantMessage` seuls) ne montre aucun appel entre les deux.
Aucun hook des settings ne dure deux minutes.

La ligne est écrite pour **tout** `UserMessage` (`agent.py`, branche `UserMessage`), quel que
soit son contenu. Le journal affirme donc un résultat d'outil là où il n'y en a pas, et l'on ne
sait pas ce que le pilote a reçu. C'est la phrase même du défaut 1 : « on ne peut pas savoir ce
que le pilote tente ». Le SDK 0.2.152 porte pourtant de quoi le dire : `UserMessage.origin`
(« renseigné sur les tours injectés : notifications de tâche, messages de canal ou de pair… ;
un message de résultat d'outil ne le porte jamais ») et la forme de `content`.

## Décisions

1. **La ligne existante ne change pas pour un vrai résultat d'outil.** Trois lecteurs en
   dépendent au caractère près : `scripts/measure-idle-lethality.sh`,
   `scripts/measure-wait-silences.py` et `tests/test_measure_idle_lethality.py`.
2. **Un `UserMessage` qui ne porte aucun bloc `tool_result` a sa propre ligne**, par exemple
   `[user:message] no tool result: origin=auto-continuation, content=1 text (312 chars)`.
   - `origin` : la valeur de `kind`, ou `none`. La valeur est réduite à `[A-Za-z0-9_-]` et
     tronquée, parce que la CLI peut émettre des valeurs que ce SDK ne connaît pas.
   - `content` : `str`, ou le compte de chaque type de bloc (`2 text`), avec la longueur totale
     du texte.
   - **Jamais le texte lui-même** : un message de pair porte un corps, et un message injecté
     peut citer n'importe quoi.
3. **Cette ligne n'est pas soumise à `verbose`.** Elle est rare (40 lignes sur 22 pilotes) et
   c'est l'anomalie qu'on cherche à lire, comme `[tool:use]`.
4. **Les lecteurs de mesure deviennent exacts sans être modifiés** : ils cessent de compter
   comme résultats d'outil des messages qui n'en sont pas.

## Hors périmètre, constaté et non corrigé

`_tool_result_count` renvoie 1 pour un message sans bloc `tool_result`, au nom du « plancher
conservateur » (cpp#145). Un tel message retire donc un outil en attente. Sur les 40 cas
mesurés, aucun outil n'était en attente : l'effet est nul en pratique. Le corriger toucherait la
comptabilité des attentes de `guardrails` (famille cpp#145, cpp#146), pas la journalisation.
Le fait est signalé, il n'est pas corrigé ici.

## Défaut 2 du ticket : rien d'actionnable, mesures

- **3587fe25 (14/09)** et **2a6c2112 (29/09)** sont relus dans cpp#311 : un groom qui
  progressait (TTFT, classe cpp#177), et une boucle de sondage fermée à la source par cpp#267.
- **Depuis le 2026-10-03, sur 405 pilotes** : 373 succès, 3 `error_during_execution:after_deny`
  (classe cpp#151), 23 `error_max_turns`. Leurs transcripts montrent que **les 23 écrivaient
  jusqu'au bout**. Au plus 27 appels séparent la dernière écriture (`Edit`, `Write`,
  `git commit`/`push`, `gh pr create`) de la fin ; 12 en sont à 5 appels ou moins. Aucun ne
  boucle : les séries de plus de 3 appels de même résumé sont des `Edit` successifs sur un même
  fichier, ou des `Read` à des décalages différents. Le seul pilote qui finit sur une attente
  (949fa0f8, `sleep 600` sur les checks de PR) avait déjà ouvert sa PR. C'est la surveillance de
  CI, une autre classe.
- **Les 22 pilotes depuis cpp#311**, journalisés en entier : 22 succès. Le plus long écart entre
  deux appels est de 908 s, une seule fois.

Un détecteur « N tours sans écriture » n'aurait arrêté aucun pilote non convergent sur cette
période, et mika#2277 a mesuré ce prédicat faux sur des pilotes sains en phase de lecture.

## Tests

Dans `tests/test_tool_use_log.py`, par `run_agent`, comme cpp#311 :

- un `UserMessage` porteur d'un `tool_result` garde la ligne historique, au caractère près, et
  n'écrit pas de ligne `[user:message]` ;
- un `UserMessage` sans `tool_result`, avec `origin`, écrit `[user:message]` avec l'`origin` et
  la forme, et pas la ligne historique ;
- un contenu en chaîne, sans `origin`, donne `origin=none, content=str` ;
- le texte du message n'apparaît jamais au journal, même s'il contient un faux secret ;
- un `kind` hostile (saut de ligne, séquence ANSI) est réduit à des caractères sûrs.

Contrôle négatif : les tests 2 à 5 échouent sans le correctif.

`tests/test_agent.py::test_user_message_branch_rearms_idle_and_logs_debug` simulait un résultat
d'outil par `UserMessage(content="tool result")`, une chaîne. Une chaîne n'est pas un résultat
d'outil : sa fixture porte désormais un vrai `ToolResultBlock`, et l'assertion sur la ligne
historique reste inchangée.
