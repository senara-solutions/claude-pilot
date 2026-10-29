# cpp#146 — les plafonds d'attente, mesurés plutôt qu'affirmés

**Ticket :** senara-solutions/claude-pilot#146
**Branche :** `fix/146-plafonds-mesures`
**Date :** 2026-10-29

## WHY

Le ticket demandait trois choses : mesurer la durée réelle des attentes, régler `toolWaitCeilingMs` sur environ 2 fois le p95, et rendre visible le temps de créneau perdu. Il était bloqué depuis le 10/09 faute d'horodatage par ligne (cpp#168 volet 1). Ce blocage est levé : tous les journaux depuis le 2026-09-10 sont horodatés.

## Ce que la mesure établit (2026-10-29, `scripts/measure-wait-silences.py`, 3435 journaux)

**La bonne quantité, c'est un silence, pas une durée d'outil.** Les plafonds d'attente n'agissent que pendant un silence du SDK plus long que `idleTimeoutMs`. Un sous-agent bavard réarme l'échéance : l'un a tourné 2066 s sans rien déclencher. Le p95 des durées d'outil se compte en secondes, et un plafond à « 2 fois le p95 » tuerait donc tous les longs builds.

| état au début du silence | silences ≥ 300 s | le plus long | temps tenu | ≥ 900 s | ≥ 1800 s |
|---|---|---|---|---|---|
| `awaiting_tool` | 294 | 1680 s | 40,8 h | 12 | 0 |
| `awaiting_model` | 143 | 512 s | 14,1 h | 0 | 0 |
| autre | 93 | 651 s | 9,7 h | 0 | 0 |

- Le silence d'outil le plus long (1680 s) est un `gh pr checks --watch` avec `timeout: 1680000`, c'est-à-dire **exactement le cap Bash** `BASH_MAX_TIMEOUT_MS` (agent.py). Les suivants sont des surveillances de CI (`sleep 900; gh pr checks`, `--watch`) et des `cargo test` de 900 à 1000 s. Les commandes sont identifiées dans les transcripts.
- Garde-fous déclenchés sur la fenêtre : `error_max_turns` 46, `idle_timeout` 13, `stall_detected` 6, `prompt_cache_dead` 3. **`toolWaitCeiling`, `modelWaitCeiling` et `rateLimitCeiling` : 0.**

## Décisions

1. **Aucune valeur ne change.** `toolWaitCeilingMs = 1800 s` n'est pas emprunté à cpp#133 : il est dérivé du cap Bash (1680 s), pour qu'une commande autorisée à courir jusqu'au cap ne soit jamais tuée d'abord par le pilote. La queue mesurée bute sur ce cap, avec 120 s de marge. `modelWaitCeilingMs = 900 s` vaut 1,76 fois le plus long silence de modèle observé (512 s).
2. **La dérivation est tenue par un test** (`tests/test_wait_ceilings_derivation.py`). Il compare les deux constantes, là où l'assertion existante de `test_agent.py` comparait le cap à un littéral, et il exige que le plafond du modèle reste sous celui des outils.
3. **La justification de `types.py`** remplace l'analogie par la dérivation et les chiffres.
4. **L'instrument du point 3** est `scripts/measure-wait-silences.py`. Il classe les silences par état et rend le temps tenu, la colonne que le ticket voulait voir, ainsi que les garde-fous déclenchés et les silences les plus longs. Stdlib seule, typé (mypy strict).

## Hors périmètre, ticket séparé

Le temps de créneau réellement perdu ne vient pas des plafonds, qui ont coûté 0. Il vient des pilotes qui surveillent la CI : environ 13 h cumulées, jusqu'à 28 min par appel, plus des appels gh, sur le créneau unique de mika-dev. Le texte du ticket est préparé à part : le pilote ne surveille plus la CI.
