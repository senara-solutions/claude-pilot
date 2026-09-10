---
issue: claude-pilot#158
title: Trois scanners de guillemets, et deux divergent déjà — extraire un `_quote_spans()` partagé - Plan
type: fix
scope_repo: claude-pilot
priority: p3-hygiene
date: 2026-09-10
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# Trois scanners de guillemets, et deux divergent déjà — extraire un `_quote_spans()` partagé - Plan

## Goal Capsule

**Objectif.** `src/claude_pilot/tier1.py` porte trois scanners POSIX indépendants
de régions citées — `_split_compound_command` (autorisation, `:729` pré-fix),
`contains_unquoted_metacharacter` (autorisation, `:826` pré-fix) et
`_mask_quoted_redirect_chars` (létalité, cpp#157). Deux divergent déjà sur
`main`, et cpp#158 en mesure une **troisième**, non caractérisée avant ce plan
(voir *Mesure*). Ce plan extrait un scanner unique, `_quote_spans(command) ->
list[tuple[int, int, bool]]`, POSIX-correct, et rebranche les trois
consommateurs dessus. La sémantique unifiée est celle déjà POSIX de
`_mask_quoted_redirect_chars` (cpp#157) — c'est elle qui ne change **pas**.

**Ce que ce ticket touche, et pourquoi c'est sensible.** Deux des trois
consommateurs sont sur le chemin d'**autorisation** (`_split_compound_command`,
`contains_unquoted_metacharacter`) — c'est `contains_unquoted_metacharacter`
qui décide, avant toute autre vérification, si une commande contient une
substitution non citée. Unifier corrige un vrai bug de sécurité, pas seulement
une dette de duplication : voir M4. La contrainte du ticket est explicite —
*si l'unification élargit ce qu'autorise le chemin d'autorisation, il faut
l'arrêter et le signaler, pas le livrer déguisé en refactor.* Ce plan documente
**chaque** entrée dont le verdict change, et prouve pour chacune que le
changement va (a) vers le comportement POSIX-correct et (b) ne rend permis, en
exécution bash réelle, rien qui ne l'était déjà.

## Mesure

Sonde exécutée le 2026-09-10 contre le worktree de ce ticket, HEAD `main`
`a89a0a5`. Décideurs réels importés directement — aucun scanner réimplémenté ;
chaque verdict cité vient soit du code réel (`main` vs. la branche du fix), soit
d'une exécution `bash` réelle (le seul oracle qui compte pour « qu'est-ce que ça
exécute vraiment »).

### M1 — `_quote_spans` n'existe pas encore

`grep -rn '_quote_spans' src/ tests/ docs/` ne trouve le nom que dans des
commentaires et un plan (cpp#157 D6, cpp#157 docstring `tier1.py:424`,
`test_tier1.py:1513`, `docs/solutions/security-issues/…`) — jamais de code. Rien
à adopter ; l'extraction part de zéro.

### M2 — La divergence D1 du ticket, reconfirmée

| entrée | `_split_compound_command` | `contains_unquoted_metacharacter` |
|---|---|---|
| `echo "a\\"` | région **encore ouverte** | région **fermée** |

`_split_compound_command` ne traite `\X` comme paire d'échappement que si `X`
est `"` — sur `\\"`, sa première contre-oblique passe, la seconde s'apparie avec
le guillemet fermant et l'avale. `contains_unquoted_metacharacter` consomme `\X`
atomiquement (n'importe quel `X`) et ferme — POSIX, et ce que
`_mask_quoted_redirect_chars` (cpp#157) faisait déjà.

### M3 — Une troisième divergence, jamais caractérisée avant ce plan

Aucun des trois scanners pré-fix n'a de traitement de la contre-oblique **hors
guillemets**, sauf `_mask_quoted_redirect_chars` (cpp#157, qui l'a ajouté pour
sa propre raison — cf. son commentaire d'en-tête `tier1.py:452-463` sur
`main`). Conséquence mesurée sur `main` :

| entrée (préfixe) | `_split_compound_command` | `contains_unquoted_metacharacter` | `_mask_quoted_redirect_chars` |
|---|---|---|---|
| `echo \'` | ouvre une région **fantôme** | ouvre une région **fantôme** | n'ouvre **rien** (correct) |

Un guillemet échappé par une contre-oblique **hors** toute région citée est un
guillemet **littéral** pour bash — il n'ouvre rien. Vérifié contre bash réel :

```
$ echo \'$(echo INJECTED)
'INJECTED
```

La substitution s'exécute réellement ; bash ne voit aucune région après le `\'`.

### M4 — La conséquence sur `main` : un vrai contournement d'autorisation, pas seulement une divergence

```python
>>> contains_unquoted_metacharacter("echo \\'$(echo INJECTED)")
False
>>> is_safe_bash_command("echo \\'$(echo INJECTED)")
True
```

Sur `main`, la contre-oblique-guillemet **fantôme** avale la vraie substitution
`$(echo INJECTED)` dans ce que le scanner croit être une région simple-citée
(donc inerte). Le chemin d'autorisation classe **safe** une commande portant
une substitution shell active et non citée — précisément la classe de bug que
`contains_unquoted_metacharacter` existe pour attraper (cpp#41, mika#944).
C'est un **faux négatif d'autorisation préexistant à cpp#157**, indépendant de
la divergence D1, révélé par la même investigation.

### M5 — Fuzzing différentiel exhaustif : chaque entrée dont le verdict change

Méthodologie : un harnais compare le code AVANT (copie figée de `tier1.py` à
`a89a0a5`) et APRÈS ce fix, sur les quatre fonctions
(`_split_compound_command`, `contains_unquoted_metacharacter`,
`_mask_quoted_redirect_chars`, `is_safe_bash_command`), sur un corpus
**exhaustif** : alphabet `{'"\\;$()`>a}`, longueurs 0 à 6, préfixé par `""` et
`"echo "` — **2 222 222 chaînes**. Aucun scanner n'est réimplémenté ; les deux
côtés sont le vrai code importé.

| fonction | entrées divergentes | sens |
|---|---|---|
| `_split_compound_command` | 27 160 | segmentation change (voir *Sécurité* — jamais un segment dangereux caché) |
| `contains_unquoted_metacharacter` | 17 408 | 14 838 `False→True` (**plus** de détection) / 2 570 `True→False` (voir *Sécurité*) |
| `_mask_quoted_redirect_chars` | **0** | extraction pure, aucun delta comportemental |
| `is_safe_bash_command` | 9 094 | 7 914 `True→False` (durcissement) / **1 180 `False→True`** (le cas que la contrainte du ticket demande de prouver) |

`_mask_quoted_redirect_chars` à **zéro** divergence confirme M7 : cette fonction
avait déjà la sémantique atomique hors-quotes (cpp#157) que `_quote_spans`
généralise ; son branchement sur le scanner partagé ne change rien à son
comportement observable, quel que soit le corpus.

### M6 — Les 7 914 durcissements sont, par construction, une fermeture de faux négatifs

`True→False` sur `is_safe_bash_command` ne peut se produire que si le nouveau
scanner détecte une substitution ou ferme une région que l'ancien manquait —
jamais l'inverse (aucune vérification n'est retirée, seule leur exactitude
change). Chaque cas est un ancien faux `allow` corrigé, dans la lignée directe
de M4.

### M7 — Les 1 180 assouplissements, classés et prouvés contre bash réel

C'est la mesure qui répond directement à la contrainte du ticket. Partition
**disjointe**, sur les 1 180 entrées `False→True` de `is_safe_bash_command` :

**Classe A — pilotée par la segmentation (325 entrées)**, c'est-à-dire les cas
où le refus `main` ne venait ni de `contains_unquoted_metacharacter` ni de
`is_tier3_dangerous` (toutes deux inchangées pour ces entrées), mais de
`_split_compound_command` produisant un segment-résidu qui ne matchait aucune
liste sûre. Chacune exécutée contre **bash réel** (`bash -x -c`, comptage des
lignes de trace `+` au niveau top) :

| sous-classe | nombre | preuve |
|---|---|---|
| erreur de syntaxe bash (rien ne s'exécute) | 284 | `bash -c` rend le code 2, aucune trace |
| exactement UNE instruction top-level exécutée | 41 | 1 seule ligne `+` de trace |
| **DEUX instructions ou plus exécutées** | **0** | — |

Exemple représentatif (`echo \'';'`) : `main` segmente en
`["echo \\''", "'"]` — le second fragment (`'` isolé) ne matche aucune liste
sûre, `main` refuse. Le fix voit une seule région `'...'` réelle après le `\'`
littéral et ne segmente pas. Vérifié contre bash réel :
```
$ echo \'';'
';
```
Une seule commande, un seul argument — le `;` est du texte cité, pas un
séparateur. Le refus `main` était un faux positif de segmentation, pas une
protection réelle.

**Classe B — pilotée par `contains_unquoted_metacharacter` (855 entrées)**,
c'est-à-dire les cas où `main` refusait spécifiquement parce que l'ancien
scanner voyait une substitution non citée. Pour chacune, tout marqueur
(`` ` ``, `$(`, `$'`) est localisé dans la sortie du `_quote_spans` du fix, et
la commande **entière** est ensuite exécutée contre bash réel :

| sous-classe | commandes uniques | preuve |
|---|---|---|
| marqueur dans une région simple-citée **non fermée** | 721 | **100 %** erreurs de syntaxe bash (code 2) — rien ne s'exécute |
| marqueur dans une région simple-citée **fermée** | 86 | 62 s'exécutent (code 0) ; dans les **62**, le marqueur apparaît **littéralement** dans stdout (0 cas où il aurait disparu, signature d'une vraie substitution) ; 24 sont des erreurs de syntaxe (parenthèses non appariées de l'alphabet, sans rapport avec la citation) |
| marqueur dans une région double-citée (fermée ou non) | 48 (instances) | 46 erreurs de syntaxe ; 2 s'exécutent et impriment le marqueur littéralement (`\"\`"` → `` "` `` ; règle `\X` déjà établie et inchangée) |

**Conclusion de M7.** Sur les 2 222 222 chaînes du corpus, **chaque** entrée où
le chemin d'autorisation autorise davantage a été vérifiée mécaniquement contre
bash réel, et tombe dans l'une de trois catégories toutes sûres : (1) bash
refuse de l'analyser — rien ne s'exécute, le verdict tier1 est sans objet ; (2)
bash exécute exactement l'instruction unique que le fix reconnaît, jamais une
deuxième cachée ; (3) le marqueur que l'ancien scanner croyait actif est du
texte littéral pour bash — aucune substitution ne se produit, avec ou sans
tier1. **Aucune entrée du corpus ne contredit cette classification.**

## Décisions de conception

### D1 — Le rebranchement n'est PAS une réimplémentation-signature identique

Le corps du ticket esquissait `_quote_spans(command) -> list[tuple[int, int]]`.
Ce plan retient `list[tuple[int, int, bool]]` — un troisième champ, `closed`.
Raison : le ticket demande explicitement que « chacun des trois gard[e] son
propre verdict sur le guillemet non fermé — c'est une décision par appelant, pas
une propriété du scanner ». Sans un signal `closed` non ambigu, un appelant ne
peut pas distinguer « la région s'arrête à `len(command)` parce qu'elle est
bien formée et se termine là » de « elle s'arrête à `len(command)` parce
qu'elle ne s'est jamais fermée » — les deux produisent le même `end`. Le champ
`closed` rend cette distinction explicite sans que le scanner lui-même prenne
parti. C'est un écart assumé par rapport à l'esquisse du corps, signalé ici.

### D2 — La règle atomique s'applique aussi HORS guillemets, comme demandé

Le corps du ticket spécifie littéralement : « `\X` en paire dans `"…"`,
contre-oblique littérale dans `'…'`, **`\X` en paire hors quotes** ». C'est
exactement la règle déjà implémentée par `_mask_quoted_redirect_chars`
(cpp#157) et absente des deux autres. `_quote_spans` l'adopte pour les TROIS
consommateurs — c'est la source de M3/M4 et de la classe B de M7.

### D3 — Chaque appelant garde son verdict propre sur le guillemet non fermé

`_quote_spans` rend `closed=False` pour une région qui atteint la fin de la
chaîne sans fermeture — un FAIT lexical, pas un choix de politique.
- `_split_compound_command` : un span, fermé ou non, est toujours traité comme
  opaque (aucun séparateur reconnu à l'intérieur) — c'est déjà ce que donne
  gratuitement `closed=False` quand `end == len(command)` : aucune branche
  spéciale n'est nécessaire, le fail-closed « refuser » de D5/cpp#157 est
  préservé sans code dédié.
- `contains_unquoted_metacharacter` : pour une région simple-citée, fermée ou
  non, **rien** n'est scanné (les simples quotes sont inertes, point). Pour une
  région double-citée, fermée ou non, le scan de `` ` ``/`$(` continue jusqu'à
  la fin du span — comportement inchangé par rapport à `main`.
- `_mask_quoted_redirect_chars` garde sa propre inversion (cpp#157 D5) : si le
  DERNIER span n'est pas fermé, la commande est rendue **inchangée**. Le
  scanner garantit qu'un span non fermé est nécessairement le dernier (une
  région qui ne se ferme jamais épuise la chaîne, donc plus aucun span ne peut
  suivre) — la vérification `spans[-1][2] is False` suffit.

Aucune de ces trois politiques n'est « corrigée » vers les deux autres — le
ticket l'interdit explicitement pour le masque de létalité (« le sens inversé
du masque de létalité est porteur (cpp#157 D5) et ne doit pas être aligné sur
les deux autres »), et ce plan étend la même discipline aux deux scanners
d'autorisation entre eux (ils étaient déjà d'accord ; rien à aligner).

### D4 — `_mask_quoted_redirect_chars` : extraction pure, prouvée par zéro divergence

Un premier passage d'implémentation a réintroduit un bug ici : masquer
aveuglément tout `<`/`>` dans `[start, end)` du span, sans respecter la
consommation `\X` — ce qui démasquait un `>` échappé (`\>`, littéral pour bash)
que le masque devait laisser visible (comportement documenté cpp#157 : « The
escaped character is consumed but NEVER masked »). Détecté par le fuzzing
différentiel M5 (1 336 divergences sur le second passage de corpus), corrigé en
répliquant la consommation `\X` dans la boucle de masquage elle-même — pas
seulement dans `_quote_spans`. M5 confirme **zéro** divergence après correction,
sur les 2 222 222 chaînes. C'est la preuve empirique que ce consommateur n'a
aucun delta comportemental — attendu, puisque `_quote_spans` généralise
exactement la sémantique que cette fonction avait déjà (D2).

### D5 — `TestQuoteScannerBoundaryParity` : mise à jour par mesure, jamais supprimée

Conformément à l'exigence explicite du ticket. Le corpus existant est repris
tel quel, sauf : (1) la ligne `double-quote-double-backslash` (D1/M2), dont les
trois colonnes attendues passent à `False, False, False` (accord) ; (2) une
ligne ajoutée, `backslash-single-outside` / `backslash-double-outside`
(D2/M3), qui n'existait pas dans la version cpp#157 du test car aucun des deux
scanners pré-fix n'avait de traitement hors-quotes à caractériser. La ligne
`unterminated-*` (D2/cpp#157) reste inchangée — l'inversion volontaire du
masque n'est pas touchée. Un nouveau test,
`test_the_three_scanners_now_agree_on_every_pinned_boundary`, rend explicite
ce que l'extraction promet réellement : accord sur toute frontière sauf
l'inversion documentée.

## Livrables

### L1 — `_quote_spans(command: str) -> list[tuple[int, int, bool]]` (`tier1.py:628`)

Scanner unique, POSIX-correct, place juste avant `_split_compound_command`.
Docstring nommant D1/D2/D3, la preuve M4, et référençant les trois
consommateurs.

### L2 — `_split_compound_command` rebranché (`tier1.py:729`)

Remplace son suivi d'état `quote_state` ad hoc par une table
`{start: end}` dérivée de `_quote_spans`, consultée à chaque position `i` pour
sauter tout un span d'un coup. La logique de détection des séparateurs
(`;`, `&&`, `&`, `|`, `\n`, `\r`) est **inchangée** — seule la détermination des
frontières citées change.

### L3 — `contains_unquoted_metacharacter` rebranché (`tier1.py:826`)

Remplace son suivi d'état par une consultation de `_quote_spans` ; pour un span
double-cité, un balayage interne (répliquant la règle `\X` atomique) cherche
`` ` `` / `$(` ; pour un span simple-cité, rien n'est balayé ; hors span, la
logique de détection (`` ` ``, `$(`, `$'`) est **inchangée**.

### L4 — `_mask_quoted_redirect_chars` rebranché (`tier1.py:427`)

Remplace son suivi d'état par une itération sur les spans de `_quote_spans`,
avec sa propre boucle de consommation `\X` (D4) pour ne jamais démasquer un
caractère échappé. Zéro delta comportemental (M5, D4).

### L5 — Tests (`tests/test_tier1.py`)

- `TestQuoteScannerBoundaryParity` mise à jour par mesure (D5).
- `TestQuoteSpansSharedScanner` (nouvelle) : tests directs de `_quote_spans` —
  forme de retour, règle atomique dans/hors guillemets, guillemet simple sans
  échappement, span non fermé, contre-oblique finale sans paire.
- `TestSharedQuoteSpansSecurityFixes` (nouvelle) : les quatre cas qui ferment
  M4 et M7 — le contournement `$(...)`/backtick corrigé, le faux positif
  `\'x'$(...)'`  corrigé (les deux vérifiés contre bash réel dans les
  docstrings), et le cas de segmentation `\'';'`.
- Anti-vacuité : chaque assertion de sécurité de `TestSharedQuoteSpansSecurityFixes`
  rend un verdict opposé sur `main` (M4, M7) — vérifié en exécutant les mêmes
  assertions contre la copie figée pré-fix pendant l'investigation.

## Séquence

1. L1 seul (scanner pur, testable en isolation) + `TestQuoteSpansSharedScanner`.
2. L4 (masque, létalité) — le consommateur qui doit rester à zéro delta ; le
   fuzzing différentiel (M5) contre la suite `TestTier3QuotedRedirectCharLethality`
   existante confirme avant de toucher aux deux autres.
3. L2, L3 (autorisation) — ensemble, puisque le fuzzing différentiel (M5) les
   couvre conjointement via `is_safe_bash_command`.
4. `TestQuoteScannerBoundaryParity` mis à jour (D5), `TestSharedQuoteSpansSecurityFixes`
   ajouté.
5. `uv run pytest` complet — 1101/1101 attendu vert (aucune régression dans les
   suites cpp#38/#41/#44/#103/#128/#130/#154/#157/#166).
6. `ruff check .`, `mypy src`, `./scripts/verify-pipeline.sh`.

## Acceptance Criteria

- **AC1** — Un unique `_quote_spans()` existe et les trois consommateurs le
  branchent ; aucune régression dans les suites `TestTier3*` (létalité) ni dans
  les tests d'intégration `is_safe_bash_command` déjà présents.
  → **Vérification :** L1-L4, suite complète verte (1101/1101, sortie
  ci-dessous).
- **AC2** — La divergence D1 (`echo "a\\"`) est résolue vers le comportement
  POSIX, mesuré par les trois oracles de `TestQuoteScannerBoundaryParity`.
  → **Vérification :** L5, ligne `double-quote-double-backslash` passe à
  `False, False, False`.
- **AC3** — Aucun assouplissement du chemin d'autorisation n'est livré sans
  preuve. Chacune des 1 180 entrées `False→True` mesurées (M5) est classée et
  vérifiée contre bash réel (M7) ; zéro entrée non expliquée.
  → **Vérification :** M7, script de classification conservé dans le corps de
  cette investigation (voir *Références*), sortie collée ci-dessous dans la
  section *Sécurité*.
- **AC4 — anti-vacuité** — Les tests de `TestSharedQuoteSpansSecurityFixes`
  rendent le verdict opposé contre la copie pré-fix de `tier1.py`.
  → **Vérification :** M4 (le contournement), rejoué contre la copie figée
  pré-fix : `contains_unquoted_metacharacter` y rend `False`,
  `is_safe_bash_command` y rend `True` — les deux s'inversent avec le fix.

## Hors portée

- Reformer la règle d'échappement double-quote elle-même (`\X` atomique pour
  tout `X`, plutôt que la règle bash stricte — backslash spécial seulement
  devant `$`, `` ` ``, `"`, `\`, newline). Cette simplification est **déjà**
  celle de `contains_unquoted_metacharacter` et `_mask_quoted_redirect_chars`
  sur `main` (cpp#41, cpp#157) ; ce ticket ne la relitige pas, il l'étend au
  troisième consommateur.
- Le miroir Rust (`permission_pre_classifier.rs`, mika). Déjà nommé divergent
  (cpp#41) ; hors périmètre, comme pour cpp#157.
- `\;`, `\&`, `\|` hors guillemets dans `_split_compound_command` — bash les
  traite comme séparateurs littéraux échappés, et le scanner pré-fix (comme le
  scanner post-fix) ne le reconnaît pas. Ce n'est PAS un scanner de
  **guillemets** ; c'est un comportement de `_split_compound_command`
  spécifique aux séparateurs, orthogonal à `_quote_spans`, inchangé par ce
  ticket dans les deux sens (mesuré : aucune entrée de M5 n'isole ce cas).

## Sécurité — le chemin d'autorisation n'est pas élargi

**La question posée par le ticket : est-ce qu'unifier permet, en exécution
réelle, quelque chose que `main` refusait ?** Réponse : non, démontré par M7,
pas seulement affirmé.

**Le sens dominant est un durcissement.** 7 914 des 9 094 divergences de
`is_safe_bash_command` (M5/M6) sont `True→False` — d'anciens faux `allow`
fermés. Le cas le plus net (M4) : `echo \'$(echo INJECTED)` était classé sûr
sur `main` alors que bash exécute réellement la substitution — un contournement
d'autorisation vivant, indépendant de la lettre du ticket (qui ne nommait que
D1) et découvert par la même investigation.

**Le sens inverse (1 180 entrées) est entièrement expliqué et vérifié contre
bash réel, jamais seulement contre le scanner lui-même** — c'est le point
méthodologique central de M7 : je ne fais pas confiance à ma propre lecture de
`_quote_spans` pour juger si un assouplissement est sûr, je fais exécuter la
commande PAR bash et je regarde ce qui se passe réellement. Trois catégories,
et rien en dehors :

1. **Erreur de syntaxe bash** (1 005 sur 1 180 en commandes uniques comptées :
   284 + 721 ; plus 46 sur 48 dans la classe double-citée). Rien ne s'exécute.
   Le verdict tier1 est sans objet — bash refuse la commande avant qu'elle
   atteigne quoi que ce soit.
2. **Exécution d'une seule instruction, celle que le fix reconnaît** (41 cas,
   classe A). Vérifié par comptage des lignes de trace `bash -x` — zéro cas à
   plus d'une instruction.
3. **Marqueur littéral, jamais substitué** (86 + 48 cas, classe B, fermées) —
   la région où le marqueur se trouve est une VRAIE région citée reconnue à la
   fois par `_quote_spans` et par bash (la congruence n'est pas accidentelle :
   `_quote_spans` implémente la même règle atomique que le lexer de bash pour
   décider où une citation commence et finit). Vérifié directement : dans
   chacun des cas qui s'exécutent, le marqueur apparaît tel quel dans stdout.

**Argument structurel, au-delà de l'échantillon.** `_quote_spans` ne fait
qu'une chose différemment de `main` : il reconnaît qu'un guillemet précédé
d'une contre-oblique **hors** citation est littéral (D2), exactement comme
bash. Chaque fois que cette correction déplace une frontière, elle la déplace
**vers** la frontière que bash reconnaît réellement — jamais ailleurs. Il en
découle que : (a) tout marqueur que `_quote_spans` classe maintenant « dans une
vraie région simple-citée » est un marqueur que bash lui-même n'exécute pas
(les simples quotes sont absolues en POSIX — aucune exception, aucun
échappement) ; (b) toute segmentation que `_split_compound_command` calcule
maintenant correspond aux frontières d'instruction que bash calcule réellement
— un segment ne peut donc jamais dissimuler une deuxième instruction que bash
exécuterait sans que le nouveau découpage la voie aussi. M7 est la vérification
empirique de cet argument sur 2 222 222 chaînes, pas un substitut à l'argument.

**Ce qui reste un jugement assumé, signalé explicitement.** La preuve M7 porte
sur un corpus généré (alphabet court, longueur ≤ 6, deux préfixes) — exhaustif
sur ce corpus, pas une preuve formelle universelle. Le fondement qui généralise
au-delà de l'échantillon est l'argument structurel ci-dessus (congruence avec
la règle de citation de bash), pas une énumération infinie. Si MPC juge cet
argument insuffisant pour un chemin d'autorisation, le point de dissent précis
est là : la classification empirique est complète sur ce qui a été testé, la
généralisation au-delà repose sur la correspondance de règle, pas sur un second
échantillon indépendant.

## Réconciliation — ce que le corps du ticket n'anticipait pas

Le corps du ticket (cpp#157 D6) ne nommait qu'**une** divergence (D1, `echo
"a\\"`) et demandait explicitement que les verdicts de guillemet non fermé de
chaque appelant restent distincts (jamais alignés). L'investigation de ce plan
en ajoute deux éléments non anticipés par le corps, tous deux plus favorables à
la sécurité que le corps ne le prévoyait :

1. **M3/M4** — une **troisième** divergence, jamais mesurée avant ce plan,
   dont la résolution ferme un contournement d'autorisation actif
   (`$(echo INJECTED)` exécuté alors que `main` le classe sûr) — pas seulement
   une incohérence de style entre scanners.
2. **D4** — un bug introduit puis corrigé DANS ce ticket (le masquage aveugle
   d'un `\X` consommé, dans `_mask_quoted_redirect_chars`), attrapé par le
   fuzzing différentiel avant tout commit visible. Il n'atteint jamais `main` ;
   nommé ici pour la traçabilité de la méthode, pas comme un residu.

Aucune AC du ticket n'est renommée ni supprimée. Le corps demandait une
extraction qui préserve trois verdicts distincts sur le guillemet non
fermé (D3) — tenu ; et une résolution POSIX-correcte de D1 — tenue (M2/AC2).
Ce que ce plan ajoute, ce sont M3/M4/M7 : la preuve, demandée par la contrainte
de sécurité du ticket lui-même, qu'aucune divergence — nommée dans le corps ou
non — ne se traduit par un assouplissement exploitable.

## Références

- `src/claude_pilot/tier1.py:427` — `_mask_quoted_redirect_chars`, dont la
  sémantique (déjà POSIX pour la règle hors-quotes) est celle que
  `_quote_spans` généralise.
- `src/claude_pilot/tier1.py:628` — `_quote_spans`, le scanner partagé (L1).
- `src/claude_pilot/tier1.py:729` — `_split_compound_command` rebranché (L2).
- `src/claude_pilot/tier1.py:826` — `contains_unquoted_metacharacter`
  rebranché (L3).
- `tests/test_tier1.py:1509` — `TestQuoteScannerBoundaryParity`, mise à jour
  par mesure (D5).
- `tests/test_tier1.py:1631` — `TestQuoteSpansSharedScanner` (nouvelle, L5).
- `tests/test_tier1.py:1682` — `TestSharedQuoteSpansSecurityFixes` (nouvelle,
  L5).
- `docs/plans/2026-09-05-001-fix-157-letalite-guillemets-tier3-plan.md` — D6,
  la dette nommée que ce ticket ferme, et la sémantique atomique hors-quotes
  déjà présente dans le masque.
- Issue : `senara-solutions/claude-pilot#158`.
