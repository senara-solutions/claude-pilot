---
ticket: cpp#219 (volet 1)
kind: fix
class: classification + observability (timing unchanged)
status: ready-for-qa
---

# Classer un stall mid-turn en `stream_stalled`, pas `idle_timeout` — Plan (volet 1)

Plan a posteriori (spawn direct). Reprend `docs/solutions/tooling-decisions/a-mid-turn-stall-is-not-an-idle-name-it-distinctly.md` + le groom cpp#219.

## Goal Capsule

Un stall mid-turn (turn ouvert : `message_start` + `content_block_start`, pas de `message_stop`, puis pings seuls) tombe dans l'état IDLE (`_awaiting_model` déjà clearé, aucun tool pendant) → meurt en `idle_timeout`, **blanchi** et **indistinguable** d'un idle vrai (mesure cpp#214 : 4 morts, dont la mort SILENCIEUSE du 09-27 10:13Z). **But** : le nommer distinctement (`stream_stalled`) et le rendre observable, **sans changer le timing**. Palliatif chiffré = cpp#214(a) (480 s) déjà mergé ; ceci est le vrai correctif de classification.

## Le fix (volet 1, borné — TIMING INCHANGÉ)

- `guardrails.py` : état `_turn_open` (True sur `message_start`, False sur `message_stop` — ces deux noms seulement, robuste multi-blocs).
- `types.py` : nouveau `GuardrailAbortReason.guardrail` littéral `stream_stalled` (additif, comme `watchdog_error`).
- `guardrails.py` `_idle_watchdog` : à l'expiration du budget idle en `_wait_state IDLE` → si turn ouvert `_abort("stream_stalled", <détail>)` ; sinon `idle_timeout` inchangé. Même deadline (`idleTimeoutMs` = 480 s) : **aucun nouveau plafond**, `_wait_state` intact. Surface via le chemin d'abort existant (`log_guardrail`).

## Fail-safe

Seul un turn CLAIREMENT ouvert est reclassé. Tout le reste (turn fermé, rien vu, deltas sans `message_start`, event None) retombe sur `idle_timeout` — **une mort n'est jamais ratée**, seul un stall clair est renommé.

## Hors portée

- **Volet 2** (heartbeat de génération pour distinguer slow-vs-dead et attendre plus longtemps) = **infaisable** (investigation rendue : aucun signal côté client distinct d'un ping keepalive). Clos, pas de code.
- **Surfaçage opérateur** = consommateur mika-side du nouveau reason : **mika#2568** (`_halt_family` doit connaître `stream_stalled`) doit merger AVANT le redeploy. Hors de ce repo.

## Acceptance criteria

- [x] **AC1 — stall mid-turn → `stream_stalled`.** `message_start` + `content_block_start`, pas de `message_stop`, silence > `idleTimeoutMs`, personne en attente → reason `stream_stalled`, PAS `idle_timeout`.
- [x] **AC2 (deux sens) — idle vrai → `idle_timeout`.** turn fermé (dernier event = `message_stop`) OU rien depuis le début, personne en attente → `idle_timeout` inchangé.
- [x] **AC3 — les 4 plafonds cpp#145 inchangés.** un stall en AWAITING_TOOL/MODEL reste sur SON plafond (jamais reclassé) ; reasons `awaiting_model`/`awaiting_tool`/`rate_limited`/`watchdog_error` intactes ; cpp#214a idle=480 inchangé.
- [x] **AC4 — timing inchangé.** `stream_stalled` tombe a la MÊME deadline idle (survit a la moitié du budget ; les plafonds 10 s ne le retiennent pas).
- [x] **AC5 — fail-safe.** état de turn ambigu → `idle_timeout` (fallback). Deux tests.

## Fire-Disposition

- **Feu** : classe de morts silencieuses mid-turn blanchies en `idle_timeout` (mesure cpp#214). **Traité** : nommé `stream_stalled` + surfacé via le chemin d'abort — un stall ne s'évanouit plus. **Timing non touché** (le 480 s de cpp#214a reste le filet).
- **Vérif de sortie** : QA MPC sur le code (feu vert reçu) ; **redeploy conditionné a mika#2568 mergé** (le dispatch doit connaître le reason). Restart moteur ensuite.
- **Résidu** : volet 2 clos (infaisable). Aucun.

## Preuve / vérif (verbatim)

`uv run pytest` → **1489 passed** ; `uv run ruff check .` → clean ; `uv run mypy src` → clean ; `./scripts/verify-pipeline.sh main-updated` → passed (docs+source).

## Références

- Solution : `docs/solutions/tooling-decisions/a-mid-turn-stall-is-not-an-idle-name-it-distinctly.md`.
- Mesure : cpp#214 (commentaire) ; palliatif : cpp#214(a) (#221, 480 s). Plafonds : cpp#145. Consommateur : mika#2568. PR : #222.
