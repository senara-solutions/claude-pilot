---
title: "fix(agent): le plafond dollars est transmis au CLI (cpp#197)"
type: fix
status: active
date: 2026-10-29
issue: senara-solutions/claude-pilot#197
---

# Le plafond dollars est transmis au CLI

## Pourquoi

`_sdk_guardrail_kwargs` transmettait `max_turns` mais s'arrêtait à `pass` pour `maxBudgetUsd`.
Le sous-type de terminaison `error_max_budget_usd`, déjà listé dans `SDK_TERMINATION_SUBTYPES`,
n'avait donc aucun émetteur, et le commentaire « application-level guardrails still apply » était
faux : le pilote n'a aucun garde coût applicatif (`guardrails.py` ne lit jamais le coût).

## Prémisse vérifiée à la source

Les pilotes tournent sous l'auth abonnement, sans clé API. Le plafond a-t-il un sens ?

- `claude-agent-sdk` 0.2.152 expose `ClaudeAgentOptions.max_budget_usd` et le traduit en
  `--max-budget-usd` (`subprocess_cli.py`).
- Le CLI embarqué coupe quand `maxBudgetUsd` est défini et que le coût du ledger
  (`costLedger.totalCostUSD()`) l'atteint, sans condition sur le type d'auth. Il émet alors un
  `ResultMessage` `error_max_budget_usd` ; un coût NaN fait refuser la suite (fail-closed).
- Le ledger est une estimation tokens × tarif, équivalent API, calculée aussi sous abonnement :
  les coûts cités dans mika#2496 (#2484 = 86 USD) viennent de runs pilotes.

Le plafond borne donc une dépense notionnelle, pas une facture. C'est la grandeur que la règle
« 120 tours / 40 USD » et l'événement moteur `pilot_cost_overrun` mesurent déjà.

## Décisions

1. `_sdk_guardrail_kwargs` pose `max_budget_usd = config.maxBudgetUsd` quand il est > 0.
2. Le défaut reste 0 = désarmé. Le seuil et l'armement (`--max-budget`, passé par dispatch-lib)
   restent côté moteur, après lecture de la population `pilot_cost_overrun`.
3. Rien à changer sur le chemin de sortie : `error_max_budget_usd` donne déjà `status=terminated`
   et `termination_reason`.

## Tests

`tests/test_max_budget_usd.py` passe par `run_agent`, capture les options remises au
`ClaudeSDKClient` et construit la ligne de commande du CLI à partir d'elles :

- plafond armé : `--max-budget-usd 40.0` est dans la commande (échoue sans le correctif) ;
- plafond à 0 : pas de drapeau ;
- un `ResultMessage` `error_max_budget_usd` sort en `terminated`, avec son sous-type et son coût.

## Hors périmètre

Admission du classifieur : non touchée. Le diff restreint, il n'élargit rien.
