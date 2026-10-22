---
title: "Les régions entre guillemets se lisent comme bash les lit - Plan"
type: fix
date: 2026-10-22
class: admission (refus de plus, aucune admission de plus) — tier1 et policy ; létalité inchangée
artifact_contract: ce-unified-plan/v1
execution: code
product_contract_source: ce-plan-bootstrap
---

# Les régions entre guillemets se lisent comme bash les lit - Plan

## Goal Capsule

- **Objectif :** une commande dont les régions entre guillemets, telles que `_quote_spans` les calcule, s'ouvrent là où bash n'ouvre aucun guillemet est refusée.
- **Moyen :** `split_is_ambiguous` (voir `docs/solutions/security-issues/a-splitter-cut-bash-does-not-make-is-refused-not-guessed.md`) nomme aussi ces formes. Les deux chemins d'admission les refusent déjà avant de lire un segment, et le refus n'est pas terminal.
- **Autorité :** porte du classifieur. Le merge revient à Vincent.
- **Arrêt :** une bascule refusé → admis sur le corpus réel arrête le travail. Toute bascule admis → refusé est nommée et pesée.

## Product Contract

### Problem Frame

Les régions entre guillemets doivent suivre la lecture de bash, qui n'en ouvre ni dans un commentaire, ni après un ouvrant de heredoc, et relit à sa façon les guillemets d'une expansion `${…}` entre guillemets doubles. `_quote_spans`, le scanner de guillemets partagé par le découpeur, par la détection des substitutions et par le chemin de létalité, ouvre une région à chaque `'` ou `"` non échappé hors région.

Vérifié à travers le handler complet, hors et sous confinement, contre bash 5.3 : chacune des formes est désormais refusée.

### Requirements

- R1. **Les formes refusées**, chaque fois là où une région de `_quote_spans` s'ouvre sans que bash ouvre de guillemet :
  - dans un commentaire (un `#` en début de mot, hors région, jusqu'à la fin de la ligne), quand la région dépasse la fin du commentaire. Une région qui finit avec le commentaire ne compte pas : un commentaire en fin de commande garde sa lecture. Un `#` qui suit un caractère échappé est au milieu d'un mot, comme pour bash ;
  - après un ouvrant de heredoc (`<<`, pas `<<<`), hors région et hors commentaire, quel que soit son délimiteur, vide compris ;
  - dans une région entre guillemets doubles, une expansion `${…}` qui contient un guillemet simple, ou que la fin de la région coupe avant son accolade fermante.
- R2. **Les deux chemins.** Les sites d'appel actuels de `split_is_ambiguous` restent les seuls : en tête de `is_safe_bash_command`, et dans `_bash_allow_is_chain_safe` après le bloc heredoc. Le heredoc sanctionné reste une preuve sur la commande entière, placée avant.
- R3. **Aucune admission nouvelle. Létalité inchangée.** `_quote_spans`, le découpeur et `_denial_is_terminal` ne changent pas. Le refus n'est pas terminal.
- R4. **Lecture prudente.** Là où lire comme bash demanderait un second analyseur, le prédicat refuse plus large : tout guillemet qui suit un ouvrant de heredoc compte, que bash le lise dans le corps ou non.

### Scope Boundaries

- Une forme dont les régions divergent mais que d'autres gardes refusent déjà sur les deux chemins n'entre pas dans le prédicat (même règle que le doc du découpeur cité plus haut).
- Le heredoc sanctionné de la policy reste admis : il ne consulte pas le découpeur, et son refus est placé après lui.
- L'évaluateur `per_spawn` (bashlex, sur option) n'est pas touché.
- Des résiduels hors de ce périmètre sont suivis en privé.

## Planning Contract

### Key Technical Decisions

- KTD1. **Étendre `split_is_ambiguous` plutôt que corriger `_quote_spans`.** Le scanner sert aussi `_mask_quoted_redirect_chars` et le découpeur, que le chemin de létalité lit. Corriger ses régions déplacerait la létalité de refus existants dans les deux sens. Il sert aussi `contains_unquoted_metacharacter` : une région en moins dans un commentaire y retirerait des détections, et donc créerait des admissions, contraires à R3. Le refus en amont donne la garantie aux admissions et laisse tout le reste intact, comme pour le découpeur.
- KTD2. **Un prédicat séparé, `_quote_opens_where_bash_reads_none`, appelé par `split_is_ambiguous`.** La marche existante de `split_is_ambiguous` reste telle quelle ; les trois termes ont leur propre marche, chacun mutable seul.
- KTD3. **Heredoc : tout guillemet après l'ouvrant, sans lire le corps.** Délimiter le corps comme bash (fin de la ligne de l'ouvrant, ligne terminatrice, `<<-`, ouvrants en file) est un second analyseur dont chaque erreur serait une sous-détection. Le terme large coûte zéro admission sur le corpus réel, comme le terme exact mesuré à côté.
- KTD4. **Expansion entre guillemets doubles : profondeur d'accolades.** Une expansion imbriquée est suivie par profondeur ; un antislash est apparié comme `_quote_spans` l'apparie. Hors guillemets doubles, les guillemets d'une expansion sont lus par bash comme le scanner les lit, ou plus largement (vérifié), donc aucun terme.
- KTD5. **Chaque terme fait rougir un test passant par le handler**, sur au moins un chemin où il est seul à refuser.
- KTD6. **Coût borné.** La recherche d'une région dans un commentaire ou après un ouvrant se fait par dichotomie sur les débuts de régions, triés et disjoints : le prédicat reste linéaire sur une entrée hostile.

## Implementation Units

### U1. Les régions de `_quote_spans` qui s'ouvrent hors de la lecture de bash sont refusées (`src/claude_pilot/tier1.py`)

**Goal :** R1 à R4.
**Files :** `src/claude_pilot/tier1.py`, `tests/test_quote_regions_match_bash.py`.
**Approach :**
1. `_quote_opens_where_bash_reads_none(command)` : une marche hors région (sauts de région et appariement de l'antislash pris à `_quote_spans`) qui lit commentaires et ouvrants de heredoc (`_parse_heredoc_opener`), puis une marche dans chaque région entre guillemets doubles pour les expansions.
2. `split_is_ambiguous` le consulte ; sa docstring nomme les nouvelles formes.

**Test scenarios :**
- Négatifs, handler complet, hors et sous confinement, refus non terminal :
  - chemin tier1 : chaque forme derrière une tête que tier1 admet seule, avec la preuve que `is_tier1_auto_approve` refuse ;
  - chemin policy : commentaire et expansion derrière une tête que tier1 refuse et qu'une règle admet, avec la preuve que la policy rend `allow` sur la commande entière.
- Positifs, mêmes chemins : `#` en milieu de mot, `$#`, commentaire sans guillemet, guillemet échappé dans un commentaire, heredoc sans guillemet après l'ouvrant, `${…}` simple entre guillemets doubles, expansion hors guillemets doubles, `\${` entre guillemets doubles, heredoc sanctionné.

**Execution note :** commit vert d'abord, puis rouge-avant terme par terme par mutants en mémoire ; un mutant identité doit rester vert.

## Verification Contract

- Suite complète, `ruff` et `mypy` verts.
- Rouge-avant terme par terme sur le commit vert : chaque terme et chaque condition neutralisés seuls font rougir au moins un test ; le mutant identité survit.
- Parité sur le corpus réel, handler complet, hors et sous confinement, `ff98d71` contre l'implémentation finale, létalité distinguée. Mesure préalable : sur 35 925 commandes distinctes, 308 portent l'une des formes et aucune n'est admise sur `ff98d71`. Résultat final : 35 957 commandes distinctes, **aucune bascule**, ni de décision ni de létalité (admises : 29 464 hors confinement, 29 822 sous confinement, identiques). Le prédicat se déclenche sur 307 d'entre elles, toutes déjà refusées avec la même létalité (290 non terminales, 17 terminales).
- Rouge-avant, résultat : 30 mutants en mémoire sur le commit vert, tous rouges ; le mutant identité survit. À travers le handler, chacune des trois formes rougit. Les conditions qui y restent vertes (début de commande, here-string, caractères de début de mot autres que les blancs) couvrent des formes que d'autres gardes refusent déjà, et ont leurs tests directs.
- Revue adverse indépendante, deux passages : ses constats sont corrigés et épinglés par des tests.

## Definition of Done

Commits locaux sur `fix/les-r-gions-entre-guillemets-se-lisent-comme`, partie de `origin/main` (`ff98d71`). Pas de ticket public. Titre, commits et corps sobres. Arrêt avant le push, pas de merge.
