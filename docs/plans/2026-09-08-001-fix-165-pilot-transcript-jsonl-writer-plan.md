---
issue: 165
type: fix
title: "claude-pilot écrit le transcript JSONL par message SDK dans ANTHROPIC_LOG_FILE (écrivain manquant de mika#2040)"
branch: fix/165/pilot-transcript-jsonl-writer
status: groomed
---

> **Grooming (orchestrateur, 2026-09-08) :** groomé par la main. Le gate architectural — l'AC0 spike-first exigé par mika-arch et ratifié par mika-prime (bearing #2040 « Voie A découpée ») — est **satisfait** : le spike de faisabilité a rendu **HOOK FIABLE (OUI)** (voir le commentaire du ticket #165, verdict daté 2026-09-08). L'incertitude « point de hook SDK introuvable/instable » est réfutée par le code. Ce plan implémente l'écrivain que le spike a débloqué. Pas de re-passe arch nécessaire : le spike EST la mesure que arch demandait.

# Plan — claude-pilot#165 : l'écrivain de transcript JSONL

## Contexte (la moitié manquante de mika#2040)

Le hook de transcript pilote (mika#1705) a un **producteur** (mika `skills/executor.rs` injecte `ANTHROPIC_LOG_FILE=~/.mika/data/pilot-transcripts/<task-id>.jsonl`, dossier monté en écriture dans le sandbox) et une **ingestion** (mika PR#2240 : reader `pilot_transcript.rs`, validation `schema_version:v1`, quarantaine, détecteur WARN `pilot_transcript_empty_after_dispatch`). **L'écrivain manque** : `grep ANTHROPIC_LOG_FILE` dans claude-pilot = 0. C'est la moitié-claude-pilot du split Prime (l'autre moitié, l'ingestion mika, se merge via mika#2240).

## Verdict du spike (AC0, satisfait — gate)

- **Point de hook unique et complet** : `src/claude_pilot/agent.py:419` — `async for message in _merge_stream(client, guardrail_watcher):` (flux du pilote headless). Traite déjà `AssistantMessage` (`:525`) et `ResultMessage` (`:559`). Tous les appels LLM y passent ; `can_use_tool` est un canal de permission séparé sans contenu LLM ; `shell.py:159`/`ipython/session.py` sont interactifs (hors dispatch).
- **Sérialisation triviale** : messages SDK `claude-agent-sdk` = `@dataclass` → `dataclasses.asdict()`.
- **Pas d'instabilité** : écrire sur le `AssistantMessage`/`ResultMessage` **complet**, PAS sur les `StreamEvent` partiels (`:359 include_partial_messages=True`) → 1 ligne/message, zéro double-comptage.

## Implémentation

À chaque `AssistantMessage` et `ResultMessage` consommé dans la boucle `_merge_stream` (agent.py), **si `os.environ.get("ANTHROPIC_LOG_FILE")` est défini**, append 1 ligne JSONL à ce fichier. No-op silencieux si la var est absente (le producteur ne l'injecte que sous dispatch mika).

**Mapping schéma v1** (source de vérité = reader mika PR#2240 `crates/mika-agent/src/task_engine/pilot_transcript.rs`, seul `schema_version` requis, corps string-ou-objet, tokens `as_i64`) :
```json
{"schema_version":"v1",
 "timestamp":"<datetime.now(timezone.utc).isoformat() + Z>",
 "provider":"anthropic",
 "model": <message.model si présent>,
 "response_body": <dataclasses.asdict(message) — accepté en objet par le reader>,
 "tokens_in": <message.usage["input_tokens"] si usage>,
 "tokens_out": <message.usage["output_tokens"] si usage>}
```
Écriture append-only, une ligne par message, `json.dumps` + `\n`, flush. Ouvrir en mode append à chaque écriture (le fichier peut être créé entre-temps ; pas de handle long-vécu à travers un crash).

**Caveat (documenté, non-bloquant)** : `ANTHROPIC_LOG_FILE` évoque le log HTTP brut ; le `request_body` sortant brut **n'est pas exposé** au point de consommation SDK → `request_body`/`latency_ms` restent `null` (le reader mika les accepte nullable). Réponse + usage + model = fiables et reproductibles. Un `request_body` réel serait une couche HTTP différente, hors v1.

## Fire-Disposition / détecteur (AC3)

Le défaut d'origine était **silencieux** (var posée, dossier monté, zéro fichier = indistinguable de « pas de session »). Détecteur = **test e2e anti-vacuité, gate CI bloquant** : une session pilote courte avec `ANTHROPIC_LOG_FILE` défini → le fichier `<task-id>.jsonl` attendu existe → **≥1 ligne JSONL valide** (`schema_version:"v1"` parse). Rouge aujourd'hui (aucun écrivain), vert après. Le détecteur côté mika (WARN `pilot_transcript_empty_after_dispatch`, mika#2240) est le miroir côté ingestion.

## Acceptance Criteria

- **AC0 (spike — SATISFAIT)** — Faisabilité du hook confirmée (verdict HOOK FIABLE, commentaire #165).
- **AC1** — Quand `ANTHROPIC_LOG_FILE` est défini, claude-pilot écrit ≥1 ligne JSONL par message SDK (`AssistantMessage` + `ResultMessage`), avec `schema_version:"v1"`. No-op si la var est absente.
- **AC2** — Le JSONL correspond au schéma ingéré par mika (reader PR#2240) ; `schema_version` présent.
- **AC3 (détecteur)** — Test e2e claude-pilot : session courte → fichier attendu → anti-vacuité (≥1 ligne valide). Gate CI. Rouge avant, vert après.
- **AC4 (intégration cross-repo)** — Après un VRAI dispatch (install éditable via `make deploy`), `count_pilot_transcripts_for_task > 0` côté mika. Le ticket ne ferme pas sur le seul merge : il ferme quand un dispatch réel produit un transcript ingéré.

## Hors périmètre
- L'ingestion + le WARN mika (PR mika#2240 — la moitié-mika, merge séparé).
- Le dispatch cross-repo mono-repo (mika#2108).
- Le request_body brut (couche HTTP, hors v1 — voir caveat).

## Frères
- mika#2040 (moitié-mika, reste ouvert) / PR mika#2240 (ingestion+WARN).
- mika#2108 (cross-repo sandbox — pourquoi #2040 était indispatchable en mono-repo).
