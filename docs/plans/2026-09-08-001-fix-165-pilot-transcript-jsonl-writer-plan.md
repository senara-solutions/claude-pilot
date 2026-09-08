---
issue: 165
type: fix
title: "claude-pilot écrit le transcript JSONL par message SDK dans ANTHROPIC_LOG_FILE (écrivain manquant de mika#2040)"
branch: fix/165/pilot-transcript-jsonl-writer
status: groomed
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
deepened: 2026-09-08
---

> **Grooming (orchestrateur, 2026-09-08) :** groomé par la main. Le gate architectural — l'AC0 spike-first exigé par mika-arch et ratifié par mika-prime (bearing #2040 « Voie A découpée ») — est **satisfait** : le spike de faisabilité a rendu **HOOK FIABLE (OUI)** (voir le commentaire du ticket #165, verdict daté 2026-09-08). L'incertitude « point de hook SDK introuvable/instable » est réfutée par le code. Ce plan implémente l'écrivain que le spike a débloqué. Pas de re-passe arch nécessaire : le spike EST la mesure que arch demandait.

> **Passe de durcissement (`/ce:plan` deepen, 2026-09-08) :** les AC, le périmètre et le verdict du spike sont **inchangés** — ils sont le contrat. Ajoutés : le site de hook **unique** (au lieu de deux), l'isolation des échecs d'écriture, le mapping `latency_ms`, les unités d'implémentation et le contrat de vérification.

# Plan — claude-pilot#165 : l'écrivain de transcript JSONL

## Contexte (la moitié manquante de mika#2040)

Le hook de transcript pilote (mika#1705) a un **producteur** (mika `skills/executor.rs` injecte `ANTHROPIC_LOG_FILE=~/.mika/data/pilot-transcripts/<task-id>.jsonl`, dossier monté en écriture dans le sandbox) et une **ingestion** (mika PR#2240 : reader `pilot_transcript.rs`, validation `schema_version:v1`, quarantaine, détecteur WARN `pilot_transcript_empty_after_dispatch`). **L'écrivain manque** : `grep ANTHROPIC_LOG_FILE` dans claude-pilot = 0. C'est la moitié-claude-pilot du split Prime (l'autre moitié, l'ingestion mika, se merge via mika#2240).

## Verdict du spike (AC0, satisfait — gate)

- **Point de hook unique et complet** : `src/claude_pilot/agent.py:419` — `async for message in _merge_stream(client, guardrail_watcher):` (flux du pilote headless). Traite déjà `AssistantMessage` (`:525`) et `ResultMessage` (`:559`). Tous les appels LLM y passent ; `can_use_tool` est un canal de permission séparé sans contenu LLM ; `shell.py:159`/`ipython/session.py` sont interactifs (hors dispatch).
- **Sérialisation triviale** : messages SDK `claude-agent-sdk` = `@dataclass` → `dataclasses.asdict()`.
- **Pas d'instabilité** : écrire sur le `AssistantMessage`/`ResultMessage` **complet**, PAS sur les `StreamEvent` partiels (`:359 include_partial_messages=True`) → 1 ligne/message, zéro double-comptage.

## Décisions techniques

### KTD1 — Un seul site de hook, en tête de boucle, pas deux sites dans les branches

Le brief de grooming disait « hook sur les branches `AssistantMessage`/`ResultMessage` ». La lecture du code dit mieux : **un seul `isinstance` en tête du corps de boucle**, juste après la garde `_GUARDRAIL_TRIP` (`agent.py:420-445`, qui `return`) et avant la branche `StreamEvent` (`:475`).

*Pourquoi* : la branche `ResultMessage` (`:559`) peut sortir par `break` **avant** tout traitement — `deny_resume.should_resume(...)` → `break` (`:580-582`, cpp#151). Un écrivain posé à l'intérieur de la branche perdrait exactement les `ResultMessage` des sessions qui meurent sur refus — la population que le transcript sert à diagnostiquer. En tête de boucle, aucun `break`/`continue` en aval ne peut escamoter une ligne, et il n'y a qu'un site à auditer.

```
async for message in _merge_stream(...):
    if message is _GUARDRAIL_TRIP: ... return 1
    record_sdk_message(message)        # ← site unique, no-op sauf Assistant/Result
    if isinstance(message, StreamEvent): ... continue
    ...
```

Le filtre `isinstance(message, (AssistantMessage, ResultMessage))` vit **dans** `record_sdk_message`, pas au site d'appel : le module possède son propre périmètre, et le `StreamEvent` partiel reste exclu par construction (spike §3).

### KTD2 — Canal latéral qui échoue ouvert, sur le modèle de `inbox_writer.py`

`inbox_writer.post_handoff` est le précédent exact dans ce repo : side-channel de fin de session, « failures are logged via stderr and never change the agent's exit code ». Le writer de transcript adopte la même posture — **il ne lève jamais**. Un disque plein, un dossier non monté ou un `PermissionError` ne doivent pas tuer une session de dispatch : le transcript est de l'observabilité, pas le produit.

*Conséquence de conception* : `record_sdk_message` enveloppe tout dans un `try/except Exception`, logge une fois par session (pas par message — un dossier absent produirait des milliers de lignes) et se désarme pour le reste de la session.

### KTD3 — `default=str` sur `json.dumps`, obligatoire

`dataclasses.asdict()` récurse sur les blocs (`TextBlock`, `ToolUseBlock`, …) mais s'arrête aux `Any` : `ToolUseBlock.input: dict[str, Any]`, `ResultMessage.structured_output: Any`, `ResultMessage.permission_denials: list[Any]`. Un objet non-JSON qui y passe ferait lever `json.dumps` — donc, par KTD2, perdrait la ligne silencieusement. `default=str` dégrade au lieu de perdre.

### KTD4 — `latency_ms` est renseigné sur la ligne `ResultMessage`

Le brief le laissait `null`. `ResultMessage.duration_api_ms: int` (SDK `types.py:1324`) est exactement la latence API de la session — le reader mika le lit en `i64_field("latency_ms")`. On le mappe. `AssistantMessage` n'a pas d'équivalent → `null` sur ces lignes.

### KTD5 — `model` par `getattr`, pas par accès direct

`AssistantMessage.model: str` existe ; **`ResultMessage` n'a pas de champ `model`**. `getattr(message, "model", None)` couvre les deux sans brancher.

## Contrat de ligne (schéma v1)

Source de vérité = le reader mika PR#2240 `crates/mika-agent/src/task_engine/pilot_transcript.rs`. Seul `schema_version` est requis ; tout le reste est nullable ; `request_body`/`response_body` acceptent une chaîne **ou** un objet JSON (le reader sérialise l'objet en forme compacte avant scrubbing).

```json
{"schema_version":"v1",
 "timestamp":"<datetime.now(timezone.utc), suffixe Z>",
 "provider":"anthropic",
 "model":"<getattr(message,'model',None)>",
 "response_body":<dataclasses.asdict(message)>,
 "tokens_in":<usage["input_tokens"]>,
 "tokens_out":<usage["output_tokens"]>,
 "latency_ms":<duration_api_ms sur ResultMessage, sinon null>}
```

Écriture append-only, une ligne par message, `json.dumps(..., default=str)` + `\n`, ouverture en mode `a` à chaque écriture (pas de handle long-vécu à traverser un crash), `flush` implicite par la fermeture du `with`.

**Caveat (documenté, non-bloquant)** : `ANTHROPIC_LOG_FILE` évoque le log HTTP brut ; le `request_body` sortant brut **n'est pas exposé** au point de consommation SDK → `request_body` reste `null` (le reader l'accepte nullable). Réponse + usage + model + latence = fiables et reproductibles. Un `request_body` réel serait une couche HTTP différente, hors v1.

## Unités d'implémentation

### U1 — `src/claude_pilot/transcript_writer.py` (nouveau module)

Miroir structurel de `inbox_writer.py` : docstring nommant le ticket et le contrat cross-repo, constantes de module, fonctions pures testables.

- `PILOT_TRANSCRIPT_SCHEMA_VERSION = "v1"` — constante de module, avec le commentaire de discipline cross-repo (un bump refuse toutes les lignes de l'autre côté ; accepter les deux versions pendant une release).
- `transcript_line(message) -> dict` — pur, sans I/O : mappe un message SDK vers le dict v1. Testable sans fichier.
- `record_sdk_message(message) -> bool` — lit `os.environ.get("ANTHROPIC_LOG_FILE")` **à l'appel** (no-op silencieux si absente ou vide, `.strip()` compris), filtre `isinstance(message, (AssistantMessage, ResultMessage))`, append une ligne. Ne lève jamais ; retourne `True` si une ligne a été écrite.
- **Dossier parent créé au vol** (`Path(path).parent.mkdir(parents=True, exist_ok=True)`, best-effort dans le même `try`). Le producteur mika monte le dossier, mais un chemin non monté est la classe d'échec la moins chère à supprimer — et sans ça, chaque ligne d'une session entière est perdue pour un `mkdir` manquant.
- **Désarmement après premier échec** : un `log_error("pilot-transcript", [...])` unique puis un drapeau de module qui coupe le writer pour le reste de la session — un dossier absent produirait sinon une ligne d'erreur par message SDK. Le drapeau est un global de module ; U4 le remet à zéro par fixture (`monkeypatch.setattr`) pour que l'ordre des tests ne porte pas d'état.

### U2 — Site de hook dans `agent.py`

Import du module + un appel `record_sdk_message(message)` en tête du corps de boucle (KTD1). Aucun autre changement dans `agent.py` — pas de nouvelle branche, pas de modification des branches existantes.

### U3 — Documentation du contrat dans `CLAUDE.md`

Une puce dans « Key design decisions » : le transcript pilote, la var d'env qui l'arme, le schéma versionné, le repo qui possède le reader. Sans ça, le prochain lecteur de `agent.py` ne peut pas trouver l'autre moitié du contrat.

### U4 — Tests (détecteur AC3)

`tests/test_transcript_writer.py` — les scénarios purs testent `transcript_line`/`record_sdk_message` directement ; le scénario d'intégration réutilise **la forme de faux client déjà en place dans `tests/test_agent.py`** (stream scripté de messages SDK pilotant `run_agent`). Si les helpers de `test_agent.py` (`_config`, `_assistant`, `_result`, `_init`) doivent être partagés, les remonter dans un `tests/conftest.py` plutôt que les dupliquer.


| Scénario | Entrée | Attendu |
|---|---|---|
| Anti-vacuité (AC1/AC3) | `run_agent` piloté par le faux client de `tests/test_agent.py`, `ANTHROPIC_LOG_FILE` pointé sur `tmp_path` | le fichier existe et contient **≥1 ligne** dont `json.loads(...)["schema_version"] == "v1"` |
| Conformité schéma (AC2) | un `AssistantMessage` avec `usage` | `tokens_in`/`tokens_out` renseignés, `provider == "anthropic"`, `model` = celui du message, `timestamp` finit par `Z` |
| Ligne `ResultMessage` (KTD4/KTD5) | un `ResultMessage` | `latency_ms == duration_api_ms`, `model is None`, `schema_version == "v1"` |
| No-op sans env var (AC1) | env var absente | aucun fichier créé, aucune exception |
| `StreamEvent`/`UserMessage` exclus (spike §3) | ces messages | aucune ligne écrite (pas de double-comptage) |
| Échec d'écriture non létal (KTD2) | chemin dans un répertoire inexistant | `run_agent` termine normalement, code de sortie inchangé, aucune exception propagée |
| Charge non sérialisable (KTD3) | `ToolUseBlock.input` contenant un objet arbitraire | la ligne est écrite, la valeur est dégradée en chaîne |

**Rouge avant, vert après** : le premier scénario échoue sur `main` aujourd'hui (aucun écrivain n'existe). Gate CI via `uv run pytest`.

## Acceptance Criteria

- **AC0 (spike — SATISFAIT)** — Faisabilité du hook confirmée (verdict HOOK FIABLE, commentaire #165).
- **AC1** — Quand `ANTHROPIC_LOG_FILE` est défini, claude-pilot écrit ≥1 ligne JSONL par message SDK (`AssistantMessage` + `ResultMessage`), avec `schema_version:"v1"`. No-op si la var est absente.
- **AC2** — Le JSONL correspond au schéma ingéré par mika (reader PR#2240) ; `schema_version` présent.
- **AC3 (détecteur)** — Test e2e claude-pilot : session courte → fichier attendu → anti-vacuité (≥1 ligne valide). Gate CI. Rouge avant, vert après.
- **AC4 (intégration cross-repo)** — Après un VRAI dispatch (install éditable via `make deploy`), `count_pilot_transcripts_for_task > 0` côté mika. Le ticket ne ferme pas sur le seul merge : il ferme quand un dispatch réel produit un transcript ingéré.

## Contrat de vérification

```bash
uv run pytest          # AC1, AC2, AC3 — inclut le détecteur anti-vacuité
uv run ruff check
uv run mypy src
```

AC4 est **post-merge** et hors PR par construction : il exige `make deploy` (install éditable) puis un dispatch réel, et l'ingestion mika (PR#2240) mergée. Il est tracé sur le ticket, pas sur la PR.

## Definition of Done

- U1–U4 livrés ; `uv run pytest`, `ruff`, `mypy` verts.
- Le détecteur AC3 est démontré rouge-avant/vert-après (la mesure, pas l'affirmation).
- PR ouverte, renvoyant vers la PR sœur mika#2240.
- **La PR référence `#165` sans le fermer** — `Refs #165`, jamais `Closes #165`. Un `Closes` fermerait au merge le ticket que l'AC4 exige de garder ouvert : l'AC4 se mesure APRÈS le merge (install éditable + dispatch réel). Écart délibéré au défaut de `.claude/commands/senara.md` step 8, motivé par l'AC4 du ticket lui-même et par la règle « dormeurs visibles » (CLAUDE.md).
- Le ticket #165 reste **ouvert** jusqu'à AC4 : condition de réveil = « un dispatch réel après `make deploy` produit `count_pilot_transcripts_for_task > 0` ». Dormeur visible, pas pierre tombale.

## Hors périmètre
- L'ingestion + le WARN mika (PR mika#2240 — la moitié-mika, merge séparé).
- Le dispatch cross-repo mono-repo (mika#2108).
- Le request_body brut (couche HTTP, hors v1 — voir caveat).
- Les chemins interactifs `shell.py` / `ipython/session.py` : hors dispatch, hors périmètre du transcript.

## Frères
- mika#2040 (moitié-mika, reste ouvert) / PR mika#2240 (ingestion+WARN).
- mika#2108 (cross-repo sandbox — pourquoi #2040 était indispatchable en mono-repo).
