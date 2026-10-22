---
title: "Une commande que le découpeur ne coupe pas comme bash est refusée - Plan"
type: fix
date: 2026-10-22
class: admission (refus de plus, aucune admission de plus) — tier1 et policy ; létalité inchangée
artifact_contract: ce-unified-plan/v1
execution: code
product_contract_source: ce-plan-bootstrap
---

# Une commande que le découpeur ne coupe pas comme bash est refusée - Plan

## Goal Capsule

- **Objectif :** une commande que `_split_compound_command` coupe, pour les formes nommées en R2, là où bash ne coupe pas n'est plus admise.
- **Moyen :** un prédicat, `split_is_ambiguous`, nomme les formes où le découpage peut différer de celui de bash. Les deux chemins d'admission les refusent avant de lire un segment.
- **Autorité :** porte du classifieur. Le merge revient à Vincent.
- **Arrêt :** une bascule refusé → admis sur le corpus réel arrête le travail. Toute bascule admis → refusé est nommée et pesée.

## Product Contract

### Problem Frame

`_split_compound_command`, que tier1 et la policy partagent, coupe sur un CR isolé, sur une continuation de ligne (antislash puis retour à la ligne) et sur un séparateur échappé (`\;`, `\|`, `\&`). Bash ne coupe à aucun de ces endroits. Le texte qui suit une telle coupe atterrit dans un autre segment que celui de sa commande, et les gardes de cette commande ne le voient plus.

Vérifié sur `11c1b29` par rejeu à travers `create_permission_handler`, hors et sous confinement : sur le chemin tier1 comme sur le chemin policy, une commande coupée ainsi est admise segment par segment.

### Requirements

- R1. **Les deux chemins.** `is_safe_bash_command` (tier1, et chaque segment jugé par la policy) et `_bash_allow_is_chain_safe` (allow de la policy) refusent une commande pour laquelle `split_is_ambiguous` est vrai, avant toute lecture de segment, formes sanctionnées sur la commande entière comprises.
- R2. **Les formes refusées :**
  - un CR, où qu'il soit ;
  - hors guillemets, une continuation de ligne ou un séparateur échappé suivi de texte de la même instruction, ou placé juste après `<` ou `>` ;
  - entre guillemets doubles, une continuation de ligne.
- R3. **Une coupe qui ne déplace rien n'est pas ambiguë** : seuls des blancs suivent, jusqu'à un vrai séparateur ou la fin. La frontière d'instruction du découpeur est alors celle de bash.
- R4. **Aucune admission nouvelle. Létalité inchangée.** Le découpeur n'est pas modifié ; `_denial_is_terminal` non plus. Le refus n'est pas terminal.

### Scope Boundaries

- Préservés : `find … -exec … {} \;` (cpp#33), le gabarit fondateur de la boucle `for` en lecture seule (cpp#151), une continuation devant une ligne qui s'ouvre sur `|`, une continuation ou un séparateur entre guillemets simples, `\\` suivi d'un retour à la ligne.
- Le heredoc sanctionné reste admis : c'est une preuve sur la commande entière, qui ne consulte pas le découpeur, et son corps est une donnée que bash ne joint pas. Le refus côté policy est placé après lui.
- L'évaluateur `per_spawn` (bashlex, sur option) n'est pas touché.
- Des résiduels hors de ce périmètre sont suivis en privé.

## Planning Contract

### Key Technical Decisions

- KTD1. **Refuser la continuation de ligne, ne pas la joindre.** Corpus réel : 27 commandes portent une continuation, 24 hors guillemets. Joindre comme bash supposerait que les gardes lisent le mot que bash reconstruit. Ils lisent le texte brut, où la paire est toujours là (`shlex` garde le retour à la ligne dans le jeton). Le mot qu'un garde juge ne serait donc pas celui que bash exécute. Refuser ne coûte rien sur le corpus (voir le Verification Contract). Un CR est refusé partout : 0 commande en porte.
- KTD2. **Le découpeur ne change pas.** Le faire coller à bash modifiait la létalité de refus existants, dans les deux sens, parce que le chemin de létalité lit ses segments. Le refus en amont donne la même garantie aux admissions et laisse la létalité intacte.
- KTD3. **Ne pas cesser de couper sur un séparateur échappé.** Mesuré sur le corpus : cela crée des admissions nouvelles, contraires à R4. Écarté au profit du refus.
- KTD4. **« Coupe qui ne déplace rien » (R3) plutôt qu'un refus brut.** Un refus de tout séparateur échappé et de toute continuation retirait `find … -exec … {} \;` (cpp#33) et le gabarit fondateur de cpp#151. Ces commandes se coupent là où bash finit l'instruction, et rien n'y change de segment.
- KTD5. **Un seul prédicat, deux sites d'appel.** Il est en tête de `is_safe_bash_command`, avant le préambule ce-work, le compound en lecture seule sous confinement et la sonde de revue, qui lisent tous des segments ou du texte brut. Il est aussi dans `_bash_allow_is_chain_safe`, juste après le bloc heredoc, avant les formes sanctionnées qui rendent `True` sans boucle par segment. Un garde placé sur un seul chemin n'est jamais atteint quand l'autre admet le premier.
- KTD6. **Un terme par forme, et chaque terme fait rougir un test passant par le handler.** Une forme que d'autres gardes refusent déjà sur les deux chemins n'entre pas dans le prédicat.

## Implementation Units

### U1. Refus des commandes au découpage ambigu (`src/claude_pilot/tier1.py`, `src/claude_pilot/permissions.py`)

**Goal :** R1 à R4.
**Files :** `src/claude_pilot/tier1.py`, `src/claude_pilot/permissions.py`, `tests/test_split_matches_bash.py`.
**Approach :**
1. `split_is_ambiguous` et `_text_follows_in_statement` dans `tier1.py`, avec les régions entre guillemets de `_quote_spans` et le même appariement atomique de l'antislash.
2. Appel en tête de `is_safe_bash_command`, puis dans `_bash_allow_is_chain_safe` après le bloc heredoc.
3. Commentaire du découpeur sur le CR mis en accord : la coupe ne reste que pour la létalité.

**Test scenarios :**
- Négatifs, à travers le handler complet, hors et sous confinement, refus non terminal :
  - chemin tier1 : chaque forme derrière une tête que tier1 admet seule (`git status`, `cd`, `git add`), avec la preuve que `is_tier1_auto_approve` refuse ;
  - chemin policy : chaque forme derrière une tête que tier1 refuse et qu'une règle admet (`uv run pytest`, `mkdir -p`, boucle `for` sanctionnée), avec la preuve que la policy rend `allow` sur la commande entière.
- Positifs, mêmes chemins : chaque coupe qui ne déplace rien, les formes entre guillemets simples, `\\` puis retour à la ligne, le heredoc sanctionné avec une continuation dans son corps.
- Directs sur `split_is_ambiguous`, y compris une redirection après la coupe, lue comme du texte.

**Execution note :** commencer par le commit vert, puis rouge-avant terme par terme, par mutants en mémoire : chaque site d'appel, chaque forme, chaque fin d'instruction, chaque appariement doit faire rougir au moins un test. Un mutant identité doit rester vert.

## Verification Contract

- Suite complète, `ruff` et `mypy` verts.
- Rouge-avant terme par terme sur le commit vert : 20 mutants, tous rouges ; le mutant identité survit.
- Parité sur le corpus réel, handler complet, hors et sous confinement, `11c1b29` contre l'implémentation finale, létalité distinguée : 35 925 commandes distinctes, **aucune bascule**, ni de décision ni de létalité (admises : 29 432 hors confinement, 29 790 sous confinement, identiques). Parmi elles, 43 portent un point de coupe contesté hors guillemets. Celles qui étaient admises le restent, parce que leur coupe ne déplace rien : 8 `find … -exec … {} \;` sur les deux chemins de confinement, et 2 continuations devant `|` sous confinement. Les autres étaient déjà refusées, avec la même létalité.

## Definition of Done

Commits locaux sur `fix/le-d-coupeur-ne-coupe-que-l-o-bash`, partie de `origin/main` (`11c1b29`). Pas de ticket public. Titre, commits et corps sobres. Arrêt avant le push pour l'audit, et pas de merge.
