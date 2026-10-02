---
ticket: cpp#267 REOPENED (mika#2627, mika#1960)
kind: fix
class: Correction de comportement harness (NON admission) — CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 dans l'env du CLI (switch primaire) + le hook PreToolUse Agent|Task FORCE run_in_background=False en ceinture
status: landed (clone isolé, branche fix267b-force-foreground-cpp) — gate MPC + preuve servie requis (non admission)
supersedes: docs/plans/2026-10-01-006-fix-267-strip-run-in-background-lethality-plan.md (cpp#269, inoperant)
---

# Le hook PreToolUse Agent FORCE `run_in_background=False` (ne le STRIP pas) — un dispatch de fond détaché ne peut pas survivre à une session headless — Plan (fix267b)

cpp#269 (fusionné) a mis le bon *placement* mais la mauvaise *réécriture* : il **retirait** la clé. Ce plan corrige la réécriture en place sur le même hook `create_subagent_model_inherit_hook` (cpp#263). Aucun nouveau chemin, aucun nouveau câblage.

## Cause racine — pourquoi cpp#269 était INOPERANT

Dans le CLI embarqué, **« Agents run in the background BY DEFAULT »** et la porte de premier plan est `run_in_background !== false`. cpp#269 **retirait** la clé (`_maybe_strip_run_in_background` faisait `pop`). Or retirer la clé = demander le défaut = **toujours en arrière-plan**. Un 2e pilote (`bb9163e1`, mika#2627, 140 tours, $59.72) est mort de la **même** mort APRÈS le déploiement de #269 : sa stderr porte 3 `agent_dispatch_run_in_background_stripped` (le hook a bien tiré), pourtant chaque appel `Agent` est revenu en **11–26 ms** et la session s'est terminée sur « waiting on them ». Les dispatches sans clé étaient en arrière-plan aussi. **`strip ≠ foreground`** : la seule valeur que le CLI lit comme premier plan est la clé **présente et littéralement `False`**.

## Le feu (origine, n=1 fatal, 2026-10-01, pilote 624656b1)

Au tour 113, le pilote lance 8 relecteurs **tous avec `run_in_background: true`**, puis **rend la main**. En headless rien ne le réveille : `ResultMessage` → clôture → relecteurs morts → revue perdue, `PIPELINE_INCOMPLETE`. Classe latente : **5 pilotes sur 9** dispatchent en arrière-plan. Récidive post-#269 : `bb9163e1`.

## La correction (prescrite MPC, révisée) — l'INTERRUPTEUR D'ENVIRONNEMENT d'abord, le hook en ceinture

**Nouveau fait (MPC, n=1, session interactive).** Forcer `run_in_background=False` dans l'`updatedInput` n'est **pas garanti** non plus : une reprise de mika#2630 a vu ses agents partir en asynchrone malgré un `false` explicite (le harness peut réinterpréter une réécriture d'entrée). Le **vrai** interrupteur du CLI embarqué est la variable d'environnement **`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`**.

### `agent.py` — couche PRIMAIRE (variable d'environnement)

Lu dans le CLI embarqué (`claude_agent_sdk/_bundled/claude`, binaire épinglé — symboles `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS` et `backgroundTasksDisabled` présents) :

```js
function Bl(){ return d3().backgroundTasksDisabled || a.CLAUDE_CODE_DISABLE_BACKGROUND_TASKS }
// schéma de l'outil Agent construit conditionnellement :
n = Bl()||k8() ? e.omit({run_in_background:!0}) : e
```

Avec la variable positionnée, `run_in_background` **disparaît du schéma** des outils Agent **et Bash** : le modèle ne peut plus le demander, et le CLI injecte le texte « only synchronous subagents ». C'est le gate du CLI lui-même, pas une réécriture qu'il pourrait réinterpréter — donc c'est l'interrupteur primaire.

- **Valeur** : `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`. Le gate est une lecture **truthy brute** de la chaîne (toute valeur non vide déclenche) ; `"1"` est la forme canonique Claude Code d'un flag `DISABLE_*`. Une chaîne vide **ne** désactiverait **pas**.
- **Mécanisme** : passé via l'option **`ClaudeAgentOptions.env`** du SDK (et non `os.environ`). Le transport sous-processus du SDK construit l'env du CLI en `{**os.environ_sans_CLAUDECODE, "CLAUDE_CODE_ENTRYPOINT": …, **options.env, "CLAUDE_AGENT_SDK_VERSION": …}` (`_internal/transport/subprocess_cli.py`) — une entrée de `options.env` atterrit donc dans l'environnement **effectif** du sous-processus CLI et l'emporte sur l'héritée. Constante `_CLI_FORCE_FOREGROUND_ENV` ; passée en `dict(...)` (copie) pour ne jamais muter la constante partagée.
- **Portée** : touche **uniquement** le sous-processus CLI/agent lancé par le SDK. Rien à voir avec le sous-processus **relais** de `transport.py` (dont `scrub_env` est un autre chemin, laissé intact — et `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS` ne contient aucun motif scrubé de toute façon).

### `permissions.py` — couche CEINTURE (hook, inchangée)
- **`_force_foreground_dispatch(tool_input)`** (renommé depuis `_maybe_strip_run_in_background`) : renvoie un `tool_input` neuf avec `run_in_background = False` **explicite et présent** dès que la valeur courante est autre que `False` littéral — **truthy OU absente** (absente = défaut de fond du CLI). No-op (`None`) **uniquement** si déjà `False` littéral. Ne mute pas l'original. Garde: `if tool_input.get("run_in_background") is False: return None`.
- **`create_subagent_model_inherit_hook`** (étendu, même hook) : compose les deux réécritures dans **un seul** `updatedInput`.
  1. cpp#263 : hérite le modèle de session si fenêtre strictement plus petite → `review_degraded` reason `agent_dispatch_model_inherit`.
  2. cpp#267/fix267b : force `run_in_background = False` → `review_degraded` reason **`agent_dispatch_forced_foreground`** (renommé). `updatedInput` **uniquement**, **jamais** de `permissionDecision`. Déclenchement sur truthy OU absent ; no-op si déjà `False`.

Le hook reste **ceinture et bretelles** : l'interrupteur d'env est le switch primaire ; le hook est le repli (au cas où une version/config du CLI ne lirait pas la variable). Changement `agent.py` : seulement l'ajout de l'option `env` sur `ClaudeAgentOptions` (couche primaire ci-dessus) ; `shell.py` inchangé.

## Side effects (effet de bord — évalué, accepté)

`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS` retire aussi `run_in_background` du schéma de l'outil **Bash** (même gate `Bl()`). Conséquence pour les pilotes :

- Un pilote ne peut plus **détacher** une commande Bash longue en arrière-plan. Une telle commande (p.ex. un build) s'exécute désormais **au premier plan** : elle doit se terminer **dans le tour**, et est soumise au **timeout** de l'outil Bash (un build qui dépassait auparavant en tâche de fond peut maintenant atteindre ce timeout).
- **Acceptable, et cohérent avec l'intention du correctif.** Un pilote headless qui détache un build Bash puis rend la main subit **exactement la même mort** que le cas Agent : la tâche détachée est orpheline à la clôture de session (`ResultMessage`). Le Bash au premier plan est donc le comportement headless **correct**, pas une régression — c'est la même doctrine « rien de détaché ne survit à une session headless ».
- **Seule implication à noter** : un build réellement long qui dépendait du Bash de fond doit désormais tenir dans le budget tour/timeout du premier plan. Aucun skill connu de ce pipeline n'en dépend de façon essentielle (Monitor, etc. — à confirmer par MPC sur transcripts réels au gate, AC révisé ci-dessous).

## Contrôles orthogonaux (rewrite ⊥ effect-verification)

### (i) Audit sur CHAQUE réécriture
`review_degraded` reason dédié `agent_dispatch_forced_foreground` (placement `pre_tool_use_hook`). Model-inherit garde `agent_dispatch_model_inherit` ; les deux co-occurrent, chacun auditable.

### (ii) Tests
- **Env primaire** (`test_cpp267_run_agent_sets_disable_background_tasks_env_in_cli_subprocess`, `test_cpp267_force_foreground_env_constant_is_canonical_truthy`) : assertion sur le **vrai mécanisme** — l'`env` que le pilote passe à `ClaudeAgentOptions` (capturé via un `ClaudeAgentOptions` espion, pas un mock du CLI) porte `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`, valeur non vide (truthy), et c'est une **copie** de la constante (non mutable). Le SDK fusionnant `options.env` dans l'env effectif du sous-processus CLI (`{**os.environ, …, **options.env, …}`), une clé présente ici est présente dans le CLI lancé.
- **Réécriture** (`test_cpp267_run_in_background_is_forced_false_when_truthy`, `test_cpp267_absent_run_in_background_is_forced_false`, `test_cpp267_already_false_is_noop`) : la logique met `run_in_background` à `False` **présent** (pas retiré) pour truthy ET pour absent ; no-op si déjà `False` ; sans muter l'entrée.
- **AC3 RED/GREEN** (`test_cpp267_invariant_red_on_strip_green_on_force_false`) : l'invariant Prime *aucune tâche de fond ne survit à la fin de session en headless*, encodé par un modèle de la porte CLI (`run_in_background !== false` via `_dispatch_is_background`). **ROUGE sur le comportement #269** (strip → clé absente → défaut de fond → invariant violé) ; **VERT après fix267b** (clé présente et `False`). Simulation du ROUGE : on reproduit le `pop` de #269 sur la forme fondatrice et on montre `_dispatch_is_background(stripped) is True`.
- **Invariant SDK** (`test_cpp267_invariant_no_background_dispatch_forwarded_via_sdk_control_request`) : assertion sur **sémantique CLI** — l'`updatedInput` transmis porte `run_in_background` **présent et `=== False`** (la seule valeur que la porte accepte comme premier plan ; contrat comportemental : le résultat de l'outil Agent ne revient qu'à la FIN du sous-agent). Atteinte prouvée via `Query._handle_control_request` + `hook_callback` (pas d'appel direct). Modèle de session inconnu pour isoler.
- **Combiné** (`test_cpp267_model_inherit_and_background_strip_combine_on_one_dispatch`) : forme 624656b1/bb9163e1 (`model: sonnet` + `run_in_background: true` sur 1M), piloté SDK : `model` **retiré** ET `run_in_background` **forcé présent-et-`False`**, les deux audits, **aucun** `permissionDecision`.

## Invariants (non-régression)

- **NON admission** : octroi Bash byte-identique ; `tier1.py` **0 ligne modifiée** ; `is_tier1_auto_approve` / `is_tier3_dangerous` / `TIER3_PATTERNS` / egress / `_denial_is_terminal` intouchés ; **aucun** `permissionDecision`.
- **cpp#263 intact** : model-inherit seul et en combinaison (test combiné). Les deux réécritures composent dans un seul `updatedInput`.

## État de livraison

- **LANDÉ** (clone isolé) : couche primaire env (`agent.py` : `_CLI_FORCE_FOREGROUND_ENV` + option `env` sur `ClaudeAgentOptions`) ; `_force_foreground_dispatch` (ceinture, force pas strip ; absent → `False` aussi) + hook/audit `forced_foreground` ; tests cpp#267 env-présence + réécriture + AC3 RED/GREEN. Suite verte, ruff/mypy propres, verify-pipeline OK (queues en rapport de remise).
- Aucun edit bloqué (non-admission ; classifier hors diff).

## Acceptance criteria

- [x] **AC0 — interrupteur d'env primaire.** `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` présent dans l'env que le pilote passe au sous-processus CLI de **chaque** session (`ClaudeAgentOptions.env`), prouvé par `test_cpp267_run_agent_sets_disable_background_tasks_env_in_cli_subprocess` (assertion sur le vrai mécanisme, pas un mock CLI). Retire `run_in_background` du schéma Agent **et** Bash.
- [x] **AC1 — force `False` explicite (ceinture).** Le hook met `run_in_background=False` sur un dispatch truthy OU absent ; déjà-`False` → inchangé (`_force_foreground_dispatch` + tests de réécriture). Conservé tel quel en défense en profondeur.
- [x] **AC2 — test sur sémantique CLI, pas présence de clé.** L'`updatedInput` réécrit a `run_in_background` **présent et `=== False`** (`test_cpp267_invariant_..._via_sdk_control_request`, assertion `updated["run_in_background"] is False`). Cadré comme : avec cette entrée, le résultat de l'outil Agent ne revient qu'à la fin du sous-agent ; preuve au niveau entrée = `run_in_background is False`.
- [x] **AC3 — invariant Prime ROUGE-avant/VERT-après.** `test_cpp267_invariant_red_on_strip_green_on_force_false` : ROUGE sur le strip de #269 (clé absente == fond == invariant violé), VERT sur force-`False`. RED-before noté dans le corps de PR.
- [ ] **AC4 — preuve SERVIE après déploiement** (résultats de l'outil Agent en **secondes**, pas en ms) : lue par MPC sur le premier pilote. **Hors de ce clone** (pas de déploiement ici) ; noté pour MPC.
- [ ] **AC5 — effet de bord Bash (gate MPC).** Rejeu sur transcripts réels : aucun pilote ne dépend de façon **essentielle** d'un `run_in_background` Bash (Monitor, etc.). Documenté en section Side effects comme acceptable (même mort headless qu'un Agent détaché) ; confirmation empirique = lecture MPC au gate. Hors de ce clone.
- [x] **Atteinte** : chemin de hook SDK conservé (`Query._handle_control_request` + `hook_callback`, pas d'appel direct) ; l'entrée transmise porte `run_in_background is False`.

## Fire-Disposition

- **Feu** : cpp#269 (strip) **inoperant** — retirer la clé demande le défaut de fond du CLI (`!== false`), donc le dispatch reste en arrière-plan et meurt à la clôture headless → revue perdue, `PIPELINE_INCOMPLETE` (récidive `bb9163e1`). Et même forcer `False` dans l'`updatedInput` n'est **pas garanti** (mika#2630 : async malgré `false`). **Traité** : couche primaire = `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` dans l'env du CLI (retire `run_in_background` du schéma Agent/Bash — le modèle ne peut plus le demander, gate du CLI lui-même) ; ceinture = le hook force `run_in_background=False` présent (truthy OU absent). **Cause racine enregistrée : le vrai interrupteur est la variable d'env du CLI, pas une réécriture d'entrée que le CLI peut réinterpréter ; `strip ≠ foreground` car le défaut CLI est l'arrière-plan.**
- **Effet de bord Bash** : la variable retire aussi `run_in_background` de Bash (build long → premier plan, dans le budget tour/timeout). Accepté : même mort headless qu'un Agent détaché ; comportement headless correct. Détail en section Side effects ; AC5 = rejeu MPC.
- **Vérif de sortie** : voir rapport de remise (pytest / ruff / mypy / verify-pipeline). AC4 (preuve servie) remis à MPC post-déploiement.
- **Résidu** : (1) parallélisme multi-message perdu (compromis piste (a)). (2) Fix racine plugin CE (interdiction par prompt) — ne tient pas seul. (3) Piste (b) (`_merge_stream`) non retenue : plus fidèle mais plus risquée.

## Références

- Solution : `docs/solutions/tooling-decisions/a-detached-background-dispatch-cannot-outlive-a-headless-session.md` (mise à jour : force `run_in_background=False`, ne pas strip — retirer la clé demande le défaut de fond).
- Plan superseded : `docs/plans/2026-10-01-006-fix-267-strip-run-in-background-lethality-plan.md` (cpp#269, strip, inoperant).
- Hook parent : `a-context-inheriting-dispatch-must-not-force-a-smaller-window-model.md` (cpp#257/263) ; `harness-runtime-tools-bypass-can-use-tool.md` (borne).
- Preuve : issue cpp#267 (rouverte) ; pilote 624656b1 (origine) ; `bb9163e1` (mika#2627, récidive post-#269).
