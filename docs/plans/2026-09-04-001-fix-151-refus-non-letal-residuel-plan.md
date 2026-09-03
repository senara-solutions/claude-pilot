---
issue: claude-pilot#151
title: Un refus tue encore une session sur trois — létalité résiduelle (B) et règle bash-grep (A) - Plan
type: fix
scope_repo: claude-pilot
priority: p1-important
date: 2026-09-04
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# Un refus tue encore une session sur trois — létalité résiduelle (B) et règle bash-grep (A) - Plan

## Goal Capsule

**Objectif.** Le ticket porte deux demandes de natures différentes. **(B)** un
refus de permission ne doit jamais terminer une session — c'est un changement
de la **boucle**, il ne relâche aucune permission. **(A)** la règle `bash-grep`
refuse une commande composée dont les membres sont en lecture seule — c'est un
changement de **règle**, il élargit une permission. Ce plan les traite
séparément, les séquence explicitement, et rend (B) livrable **sans** (A).

**Recommandation de séquence : B d'abord, A ensuite — et A seulement sur GO
explicite de Vincent.** Ce n'est pas une préférence, c'est un compte. Sur les
huit commandes refusées listées dans le corps du ticket, la forme que l'AC3
nomme (`cd <dir> && for …`) en couvre **une seule**. Les sept autres sont des
compositions d'autres formes (`;` entre `sed`/`echo`/`gh`, `&& … ||` autour de
`command -v`, `; env | grep`, `&& grep -nE`) qu'aucune extension de
`bash-for-loop-safe-body` ne touche, plus un `mkdir` qui appartient à cpp#150.
Corriger A ferme 1 chemin sur 8 ; B ferme la classe. C'est la mesure qui porte
l'avis, et l'arbitrage reste à l'architecte puis à Vincent.

**Moyens.** Pour (B) : d'abord rendre la mort **lisible** — aujourd'hui rien,
dans aucun journal, ne distingue une session que claude-pilot a *demandé* de
tuer d'une session morte *malgré* lui. Ensuite seulement, la rendre non
létale. Pour (A) : une extension anchor-préservée d'une règle tight existante,
avec une contrainte de confinement sur la cible du `cd`.

**Hiérarchie d'autorité.** ACs du corps du ticket (AC1–AC5) > ce plan >
jugement de l'implémenteur. **AC4 est non négociable** et gouverne toute
décision d'élargissement : une commande composée dont **un seul** membre est
destructeur reste refusée. La garde ne devient pas un passe-plat.

**Conditions d'arrêt.**
- S'arrêter si un correctif de (B) transforme un refus en autorisation. (B) ne
  touche pas `policy.py`, ni `policies/permissions.yaml`, ni la décision
  rendue par `create_permission_handler`. Il ne touche que ce qui arrive
  **après** que le refus a été rendu.
- S'arrêter si un correctif de (A) élargit la règle au-delà d'une forme
  entièrement ancrée à charset restreint. Le motif doit rester
  `^…$` de bout en bout, comme `bash-for-loop-safe-body` (cpp#92),
  `bash-git-show-redirect` (cpp#35) et `bash-explore-script-fallback` (cpp#100).
- S'arrêter si (A) est implémenté **avant** le GO explicite de Vincent. Le
  grooming produit ce plan et s'arrête là ; la boucle ne modifie pas sa propre
  surface de sécurité sans porte humaine.
- S'arrêter si (B) prétend corriger le diagnostic `ede_diagnostic` lui-même.
  Il n'appartient pas à ce dépôt (voir § Constat structurel).

**Profil d'exécution.** Un dépôt. (B) touche `src/claude_pilot/ui.py`,
`src/claude_pilot/permissions.py`, `src/claude_pilot/permission_events.py`,
`src/claude_pilot/agent.py`, `src/claude_pilot/types.py`. (A) touche
`src/claude_pilot/policies/permissions.yaml` et le commentaire d'exemption de
`src/claude_pilot/permissions.py`. Séquentiel : B puis A.

**Tail ownership.** PR sur `claude-pilot`. `Closes #151` seulement si les deux
volets livrent ; sinon PR (B) avec `Refs #151` et le volet (A) reste ouvert
avec sa condition de réveil (« quand Vincent donne le GO sur l'élargissement
de règle »).

## Constat structurel — ce que le grooming a mesuré

Trois constats de fait établis pendant ce grooming. Ils changent la forme du
correctif et doivent être lus avant les phases.

### C1 — `ede_diagnostic` n'appartient pas à claude-pilot

La ligne `[error] error_during_execution: [ede_diagnostic] result_type=user
last_content_type=n/a stop_reason=tool_use` est composée de deux moitiés
d'origines différentes :

- `[error] error_during_execution: ` vient de `ui.log_error()`
  (`src/claude_pilot/ui.py:56-57`), appelé depuis `agent.py:465`. C'est nous.
- `[ede_diagnostic] result_type=… last_content_type=… stop_reason=…` est la
  prose portée par `ResultMessage.errors[]`. Elle **n'existe nulle part dans ce
  dépôt** : `git log --all -S"ede_diagnostic" -- src/` ne rend rien, et le
  littéral n'apparaît que dans un commentaire de `tests/test_policy_devpilot.py`
  qui cite un journal. Il est émis par le binaire `claude` **embarqué dans le
  SDK** :
  `…/site-packages/claude_agent_sdk/_bundled/claude` (204 Mo, claude-agent-sdk
  0.2.148), où `grep -aob "ede_diagnostic"` rend cinq occurrences.

**Conséquence sur AC2.** AC2 demande que « le diagnostic `ede_diagnostic`
distingue “refus reçu, session poursuivie” de “erreur réelle de la boucle
SDK” ». Le texte de ce diagnostic est amont et hors de notre portée. AC2 est
donc satisfait **par-dessus** : claude-pilot classe le `ResultMessage` amont
contre son propre état de session et émet **son** sous-type distinctif. C'est
exactement la forme additive déjà validée par cpp#144 (nouveau `subtype` de
`ResultJson`, jamais un nouveau `status`) — le précédent est mergé, dans le
même fichier.

### C2 — la létalité d'un refus n'est journalisée nulle part

cpp#128 a séparé la **décision** (refuser) de la **létalité** (`interrupt=True`)
via `permissions._denial_is_terminal`. Deux classes restent délibérément
létales : le veto de destination (confinement worktree / control-plane) et le
Bash tier3-dangereux.

Mais `interrupt` **n'est enregistré nulle part** :
`ui.log_policy_deny(tool_name, detail, rule_id)` ne le prend pas en paramètre
(`ui.py:117-119`), et `permissions._record_decision` n'émet vers cm que
`decision` ∈ {allow, deny} et `rule_id` (`permissions.py:874-900`). Aucun
`grep` sur `interrupt` ne rend quoi que ce soit dans `permission_events.py`.

**Conséquence.** Aujourd'hui, devant les 8 sessions mortes du ticket, personne
— humain ou agent — ne peut dire lesquelles claude-pilot a **demandé** de tuer
(`interrupt=True`, comportement correct par conception) et lesquelles sont
mortes **malgré** `interrupt=False` (le vrai résidu de cpp#128). Les 8 ne sont
pas une classe : ce sont au moins deux classes superposées, et le `[bash-mkdir]`
de la ligne 8 est très probablement de la première (veto de destination,
cpp#150). **C'est ce qui rend la phase B0 non négociable et première** : sans
elle, AC5 mesure une population qu'on ne sait pas décrire.

### C3 — AC3 est plus large que sa formulation, et il faut le dire

La commande refusée est tronquée dans le journal : `_summarize_input` coupe à
200 caractères (`permissions.py:1486`), donc le corps `do … ; done` de la
boucle n'est **pas** récupérable depuis `/var/log/claude-pilot/*.stderr`. C'est
en soi un défaut d'observabilité pour tout post-mortem de permission (nommé ici,
non corrigé par ce plan).

Ce qui est établi malgré la troncature :
- Les cibles de la boucle sont `test_rescue_commit_no_verify`,
  `test_dev_groom_dirty_rescue`, … Dans `mika`, ce sont des fichiers
  `skills/bundled/_shared/tests/test_*.sh`.
- Leur idiome d'exécution documenté est
  `# Run: bash skills/bundled/_shared/tests/test_rescue_commit_no_verify.sh`
  (`test_rescue_commit_no_verify.sh:18`).

**Inférence, à confirmer par l'implémenteur, non tenue pour acquise :** le
corps de la boucle invoque `bash "$t.sh"` (ou `./$t.sh`), pas une commande de
la liste énumérée de `bash-for-loop-safe-body`
(`echo|printf|grep|cat|head|tail|ls|wc|find|test|[|dirname|basename`).

**Conséquence sur AC3.** Si l'inférence tient, ajouter un préfixe `cd <dir> &&`
au motif **ne débloque pas la commande observée** : le corps échoue au motif
indépendamment du préfixe. AC3 dit « `do <lecture seule>; done` » ; exécuter un
script de test n'est pas en lecture seule au sens de ce motif. Le volet (A) se
scinde donc en deux options de portées très différentes — voir § Fourche A.

Cela explique aussi la ligne 3 des huit refus : la boucle `for` **nue**, sans
`cd`, refusée elle aussi sous `[bash-grep]`. La prémisse du corps (« cpp#128
traitait la boucle `for` nue ») décrit la règle tight cpp#92, qui ne couvre que
les corps énumérés ; elle ne couvre pas ce corps-ci. Rien ici ne contredit
AC3 : cela en révèle le coût réel.

## Product Contract

### Fourche B — la létalité (prioritaire, aucun élargissement de permission)

**B0 — rendre la mort lisible (préalable, zéro changement de politique).**
Enregistrer `interrupt` là où la décision est déjà tracée :
- `ui.log_policy_deny(tool_name, detail, rule_id, *, terminal: bool)` → la
  ligne devient `[policy:deny] Bash: … [rule-id] (terminal)` /
  `(non-terminal)`.
- `permission_events.emit(...)` porte un champ `terminal: bool`.

Non négociable comme premier pas : c'est la garde de prérequis qui rend AC5
mesurable. Sans elle, « 0 morts parmi les sessions refusées » ne dit pas si le
zéro est atteint ou si la population a été redéfinie.

**B-repro — reproduire la mort.** Rejouer la trace exacte du ticket : refus
non terminal → `user message (tool result)` → `stop_reason=tool_use` → EDE.
La signature `last_content_type=n/a` est la piste : le dernier message `user`
n'a pas de bloc de contenu exploitable. Vérifier ce que `PermissionResultDeny`
transporte réellement sur le chemin chain-veto (`permissions.py:1053-1064`,
`message=veto_reason`) et sur le chemin règle (`permissions.py:1120-1128`,
`message=pd.reason`), y compris le cas où la raison est vide.

**B1 — classer (AC2).** Dans `agent.py`, au site terminal (`agent.py:~465`),
quand `subtype == "error_during_execution"`, consulter l'état de session pour
savoir si un `[policy:deny]` **non terminal** l'a précédé, et émettre un
sous-type distinct (forme proposée : `error_during_execution:after_deny`).
Additif : `status` reste `"success" | "error" | "terminated"`, seul `subtype`
(déjà `str` libre) porte l'information neuve. C'est la forme cpp#144 à
l'identique.

**B2 — rendre non létal (AC1).** Deux voies, à choisir **avec la mesure de
B-repro en main**, pas avant :
- **B2a — récupérer.** Sur EDE précédé d'un refus non terminal, reprendre la
  session via le chemin de reprise du SDK (`claude_agent_sdk/_internal/
  session_resume.py` existe) et poursuivre, avec un plafond explicite (une ou
  deux reprises par session ; `maxTurns=200` reste la borne globale — c'est la
  seule des quatre garde-fous qui borne réellement une boucle occupée, cf. le
  commentaire cpp#128 de `permissions.py`). Ne dépend d'aucun diagnostic amont.
- **B2b — prévenir.** Si B-repro identifie une cause locale (message de refus
  vide, `tool_result` orphelin), la corriger à la source. Préférable si elle
  existe : elle supprime la mort au lieu de la rattraper.
- **B2c — remonter.** Si la cause est strictement amont, ouvrir le ticket chez
  claude-agent-sdk / claude-code et livrer B2a comme filet. B2c ne remplace
  jamais B2a : un ticket amont n'est pas un correctif.

Recommandation : livrer **B2a inconditionnellement** (le filet ne dépend de
personne), et B2b en plus si B-repro le rend possible.

### Fourche A — la règle (élargissement, GO requis)

**A1 — la lettre d'AC3.** Étendre le motif de `bash-for-loop-safe-body` d'un
préfixe optionnel `cd <chemin> && `. L'exemption par `rule_id` dans
`_bash_allow_is_chain_safe` (`permissions.py:~439`) s'applique alors sans
modification — c'est elle qui empêche le re-découpage sur `&&`/`;` de vetoer
une forme que la règle tight a déjà prouvée.

Contrainte de confinement à ajouter, absente d'AC3 : la cible du `cd` doit être
**relative au worktree** — pas de `/` initial, pas de `~`, pas de `..`, charset
excluant `;`, `|`, `&`, backtick, `$`, `<`, `>`, `\`. Sans elle,
`cd /etc && for f in passwd; do cat $f; done` devient une lecture hors
worktree : `_destination_veto_reason` ne couvre que les **écritures**
(`mkdir`, `cp`/`mv`, `git show >`), pas les lectures. Même forme de vérification
statique de cible que `bash-git-show-redirect` (cpp#35), avec le même résidu
connu et accepté : aveugle aux liens symboliques (cpp#38).

Coût : faible. Effet mesuré : **1 des 8 refus** du ticket. Ne débloque pas la
commande observée si C3 tient.

**A2 — ce qui débloquerait réellement la commande observée.** Une règle tight
nouvelle sanctionnant `[cd <rel> && ] for t in <noms>; do bash <rel>/$t.sh;
done` — c'est-à-dire l'**exécution de scripts arbitraires du worktree** depuis
une boucle. C'est un élargissement d'une autre nature qu'A1, et il ne doit pas
être présenté comme « ajouter un préfixe `cd` ». Il exige le GO de Vincent en
propre, distinct de celui d'A1.

Le plan **ne recommande pas** A2 dans ce ticket. Un pilote qui doit lancer sa
suite de tests a une réponse plus étroite disponible : sanctionner l'invocation
d'**un** script de test à la fois (`bash <rel>/test_*.sh`), sans boucle, ce qui
laisse le modèle enchaîner N appels d'outils au lieu d'un — coûteux en tours,
nul en surface de sécurité nouvelle. À porter à l'architecte comme troisième
branche (A3) si Vincent veut débloquer l'exécution des tests sans ouvrir la
boucle.

### Ce que (B) ne fait pas

(B) ne touche ni `policy.py`, ni `policies/permissions.yaml`, ni la valeur de
retour de `create_permission_handler`. Aucune commande refusée aujourd'hui ne
devient autorisée. La classe létale délibérée de cpp#128 (veto de destination,
tier3-dangereux) **reste létale** : B1 et B2 ne s'appliquent qu'aux refus dont
`_denial_is_terminal` a déjà rendu `False`. Un veto de confinement continue
d'arrêter la session, ce qui est sa fonction.

## Acceptance criteria

Reprise fidèle des AC du corps, avec leur rattachement de phase.

- **AC1 (B, prioritaire)** — Un `[policy:deny]` **non terminal** sur un appel
  d'outil ne termine pas la session. Un test rejoue la trace exacte du corps —
  refus, `user message (tool result)`, `stop_reason=tool_use` — et assure que la
  session continue et peut émettre un appel suivant. → **B-repro + B2**.
- **AC2 (B)** — Le diagnostic distingue « refus reçu, session poursuivie » de
  « erreur réelle de la boucle SDK ». Satisfait par un sous-type émis par
  claude-pilot, **pas** par une modification du texte `ede_diagnostic`, qui est
  amont (C1). → **B1**.
- **AC3 (A)** — `cd <dir> && for t in …; do <lecture seule>; done` est accepté
  par `bash-grep`, comme l'est la boucle `for` nue depuis cpp#92/#128. Satisfait
  à la lettre par **A1**. Le corps de boucle réellement observé sort de
  « lecture seule » (C3) et relève d'A2/A3, sur GO distinct.
- **AC4** — Non-régression : une commande composée dont **un seul** membre est
  destructeur reste refusée. → tests de non-régression obligatoires sur A1 :
  `cd x && for t in a; do rm -rf $t; done`, `cd /etc && for f in passwd; do cat
  $f; done`, `cd .. && for …`, `cd ~ && for …`, `cd $(evil) && for …`,
  `cd $FOO && for …` (expansion de variable dans la cible), `cd 'a b' && for …`
  (espace dans le chemin) — tous refusés. Les deux derniers sont nommés par
  l'architecte en première passe (Q5) : le charset restreint les exclut déjà,
  et un test qui le PROUVE vaut mieux qu'un charset dont on suppose qu'il tient.
  Aucun de ces cas ne doit passer par l'exemption `rule_id`.
- **AC5** — Mesure rejouable, seuil écrit : sur les 40 dernières sessions, la
  proportion de sessions mortes en `error_during_execution` parmi celles ayant
  subi un refus tombe à **0**. → mesurable seulement **après** B0, sur une
  fenêtre fraîche post-déploiement, et sur la population « refus **non
  terminal** » — la population « refus terminal par conception » n'est pas
  concernée et B0 est ce qui permet de l'exclure sans redéfinir le chiffre en
  douce. Contrôles du corps conservés : critère de mort lu sur les cinq
  dernières lignes du stderr ; critère de refus compté sur tout le fichier ;
  contrôle discriminant des sessions sans refus.

## Fire-Disposition

Pré-spécifiée, pour que le résultat de la mesure ne se renégocie pas après
coup.

- **Si B-repro reproduit la mort avec une cause locale** → B2b est livré, B2a
  livré en filet. AC1 est tenu par un test qui rejoue la trace.
- **Si B-repro reproduit la mort sans cause locale** → B2a seul, plus B2c
  (ticket amont). AC1 est tenu par B2a ; le plan le dit au lieu de prétendre
  avoir corrigé la cause.
- **Si B-repro ne reproduit pas** → ne pas livrer B2 à l'aveugle. Livrer B0+B1
  (observabilité + classification, tous deux sans risque), puis rouvrir la
  mesure sur la fenêtre fraîche que B0 rend lisible. Un correctif non reproduit
  est une hypothèse déployée.
- **Si B0 montre que les 8 morts sont majoritairement `interrupt=True`** → le
  résidu n'est pas dans la boucle mais dans les règles de confinement, et le
  ticket doit être re-cadré (le volet B devient B0+B1 seuls). Cette issue est
  nommée d'avance parce qu'elle est plausible, pas parce qu'elle est attendue.
- **Sur (A)** : aucune ligne de règle n'est écrite avant le GO de Vincent. A1
  et A2 se demandent séparément.

## Phases

**Ordre : B0 → B-repro → B1 → B2 → [GO] → A1 → [GO distinct] → A2/A3.**
Chaque phase est livrable seule. (B) ne dépend pas de (A) ; (A) ne dépend pas
de (B).

### Phase B0 — journaliser la létalité (aucun changement de politique)
1. `ui.log_policy_deny` prend `terminal: bool` et le rend dans la ligne.
2. `permissions.create_permission_handler` passe la valeur déjà calculée par
   `_denial_is_terminal` aux quatre sites de refus
   (`permissions.py:1053`, `:1085`, `:1120`, et le site `deny_with_notify`).
   Le site `:1085` (veto de destination) est `interrupt=True` en dur : il passe
   `terminal=True` littéral.
3. `permission_events.emit` porte `terminal`. Fail-open inchangé.
4. Tests : chaque classe de refus rend la bonne valeur ; aucun test existant de
   décision ne change de verdict.

### Phase B-repro — reproduire
5. Test d'intégration rejouant la séquence du corps contre un transport
   simulé : refus non terminal → `tool_result` → tour suivant attendu.
6. Inspecter ce que le SDK transmet réellement pour un `PermissionResultDeny`
   dont `message` est vide ou absent. Documenter le résultat dans le test,
   qu'il reproduise ou non.

### Phase B1 — classer (AC2)
7. Marqueur de session « un refus non terminal a eu lieu », posé au site où le
   refus existe déjà (même geste structurel que `guardrails.
   operator_question_denied`, cpp#144).
8. Au site terminal d'`agent.py`, sous-type distinct quand
   `error_during_execution` suit ce marqueur. `status` inchangé.
9. Tests : contrôle positif (refus puis EDE → nouveau sous-type) **et** contrôle
   négatif (EDE sans refus → sous-type inchangé). Les deux dans le même test.

### Phase B2 — rendre non létal (AC1)
10. Selon la Fire-Disposition : B2b si cause locale, B2a en filet borné.
11. Plafond de reprise explicite et journalisé ; jamais de reprise silencieuse.
12. Test AC1 : la session poursuit et émet un appel d'outil **après** le refus.

### Phase A1 — préfixe `cd` (après GO)
13. Motif de `bash-for-loop-safe-body` étendu d'un préfixe optionnel
    `cd <chemin-relatif> && `, ancrage `^…$` préservé, charset de chemin
    excluant métacaractères, `/` initial, `~` et `..`.
14. Le commentaire d'exemption de `_bash_allow_is_chain_safe` est mis à jour
    pour décrire la forme étendue (le couplage par `rule_id` échoue fermé et
    reste inchangé).
15. Tests AC4 : la batterie de refus listée en AC4, plus les cas positifs.

### Phase A2/A3 — exécution de scripts de test (GO distinct, hors de ce plan)
16. Non planifié ici. Nommé pour que la décision soit prise, pas héritée.

## Commandes de vérification

```
cd claude-pilot
uv run pytest tests/ -q
uv run pytest tests/test_permissions.py tests/test_policy_devpilot.py -q
uv run ruff check src/ tests/
uv run mypy src/
```

Mesure AC5 (post-déploiement, fenêtre fraîche, après B0) :

```
# population : sessions ayant subi un refus NON TERMINAL
grep -l 'policy:deny.*(non-terminal)' /var/log/claude-pilot/*.stderr
# morts : motif sur les CINQ DERNIERES lignes seulement
for f in <ces fichiers>; do tail -5 "$f" | grep -q 'error_during_execution' && echo "$f"; done
# contrôle discriminant : sessions sans aucun refus -> doit rendre zéro mort
```

## Risques

- **Le plus grave : livrer A sans B.** Sept des huit refus mesurés ne sont pas
  de la forme qu'A corrige. Un GO donné sur A seul ferme un chemin et laisse la
  classe intacte — exactement la trajectoire cpp#127 → #143 → #150 → #128, où
  quatre règles ont été corrigées une à une pendant que la classe survivait. Ce
  plan ne peut pas empêcher ce choix ; il l'expose chiffré.
- **B2a rattrape sans guérir.** Une reprise bornée transforme une mort en
  ralentissement. C'est une amélioration réelle et une dette nommée : si la
  cause locale existe, B2b la ferme et B2a devient une ceinture.
- **Le zéro d'AC5 peut être atteint par redéfinition de population.** La garde
  est B0 : la population est « refus non terminal », établie par un champ
  journalisé, pas par un jugement rétrospectif.
- **A1 ouvre une lecture hors worktree si la cible du `cd` n'est pas
  contrainte.** Le confinement d'écriture existant ne couvre pas les lectures.
  Contrainte explicitée en phase 13 ; sans elle, A1 ne doit pas être livré.
- **Résidu accepté (cpp#38) :** la vérification statique de cible est aveugle
  aux liens symboliques. Même résidu que `bash-git-show-redirect`,
  `bash-cp-mv`, `bash-mkdir`. Nommé, non fermé ici.

## Hors périmètre

- La règle `bash-mkdir` — **cpp#150**, déjà ouvert. La ligne 8 des huit refus
  lui appartient.
- Le refus sur `AskUserQuestion` — **cpp#144**, fermé, autre logique.
- Le texte du diagnostic `ede_diagnostic` — amont, dans le binaire `claude`
  embarqué par claude-agent-sdk (C1). Non modifiable depuis ce dépôt.
- La troncature à 200 caractères de `_summarize_input`
  (`permissions.py:1486`), qui empêche un post-mortem de permission de voir le
  segment vétoé. Défaut réel d'observabilité, constaté pendant ce grooming,
  **non corrigé ici** : il mérite son propre ticket.
- A2/A3 (exécution de scripts de test depuis une boucle) : décision de Vincent,
  distincte du GO sur A1.
