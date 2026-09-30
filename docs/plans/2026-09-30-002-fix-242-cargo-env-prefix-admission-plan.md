---
ticket: cpp#242 (mika#2105 / mika#2589 ; lié cpp#184)
kind: fix
class: admission (ratifiée MPC/Prime sur délégation Vincent, 2026-09-30 07:12)
status: ready-for-qa
---

# `bash-cargo` admet un préfixe d'affectation à liste fermée + `cargo --version`/`metadata` — Plan

Famille admission (cpp#45 pour le modèle de liste fermée). ADMISSION explicitement **ratifiée** ((a)+(b) ; (c) `make` NON traité). GO gate MPC/Prime reçu.

## Goal Capsule

Le pilote `2a6c2112` (mika#2105 sur mika#2589) a atteint `error_max_turns` (151 tours, 11 min) **sans produire aucun travail** : ses commandes ordinaires de mesure de build ont été refusées une par une, en non-terminal. Cause : le motif `allow` de `bash-cargo` était ancré sur le PREMIER MOT nu (`^cargo\s+…`). La façon standard de passer une variable à un build — un préfixe d'affectation `NAME=value` (`CARGO_INCREMENTAL=0 cargo …`) — faisait tomber la commande dans le refus par défaut ; et `cargo --version` n'était pas dans la liste de sous-commandes. **But** : admettre ces deux formes, **admission seule**, la létalité et le gate tier1 jamais touchés, l'axe egress/proxy (`HTTPS_PROXY=`) inchangé.

## Le fix (ADMISSION SEULE — `policies/permissions.yaml`, règle `bash-cargo`)

- **(a) Préfixe d'affectation à LISTE FERMÉE.** Le motif accepte désormais un préfixe (répétable, chaînable) de la forme `NAME=value ` où `NAME` appartient à une alternation FERMÉE de cinq noms build/log : `CARGO_INCREMENTAL | CARGO_TARGET_DIR | RUST_LOG | RUST_BACKTRACE | CARGO_TERM_COLOR`. Jamais un `\w+=` générique. Le token qui suit le préfixe fermé doit être littéralement `cargo` : un binaire non-`cargo` derrière le préfixe (`… bash -c '…'`) n'est pas admis.
- **(b) Sous-commandes lecture seule.** Ajout de `metadata` et `--version` à l'alternation des sous-commandes admises.
- **(c) `make` : rien.** Hors portée, non touché.

Motif : `^(?:(?:CARGO_INCREMENTAL|CARGO_TARGET_DIR|RUST_LOG|RUST_BACKTRACE|CARGO_TERM_COLOR)=\S*\s+)*cargo\s+(build|test|check|clippy|fmt|run|clean|doc|tree|metadata|--version)\b`

## Fail-safe / axe egress séparé

Refus fermé dans toutes les directions hors liste : tout nom d'affectation HORS des cinq (`PATH=`, `HOME=`, `LD_PRELOAD=`/`LD_*`, `HTTPS_PROXY=`/`HTTP_PROXY=`, `FOO=`, …) laisse la commande en **default-deny** — le `^` ancre le préfixe au début et exige `cargo` juste après la liste fermée, donc aucun glissement. L'axe egress/proxy-disabling (`HTTPS_PROXY= cargo build`) est un axe d'admission SÉPARÉ, non déplacé par ce patch : il reste refusé par le default-deny, exactement comme avant.

## Hors portée

- Létalité : intouchée (`_denial_is_terminal` inchangé — ce patch est admission seule).
- Le gate tier1 `is_tier1_auto_approve` : jamais touché.
- `RUSTFLAGS` : volontairement HORS liste (à trancher séparément côté ticket) — reste refusé.
- (c) cibles `make test-*`/`verify-*` : NON traitées ici.

## Acceptance criteria

- [x] **AC1 — préfixe fermé chaîné admis.** `CARGO_INCREMENTAL=0 CARGO_TARGET_DIR=/x cargo test --no-run`, `RUST_LOG=debug cargo build`, `RUST_BACKTRACE=1 cargo test`, `CARGO_TERM_COLOR=always cargo clippy` → `allow` (bash-cargo).
- [x] **AC2 — sous-commandes lecture seule admises.** `cargo --version`, `cargo metadata --format-version 1` → `allow`.
- [x] **AC3 — noms hors liste refusés.** `PATH=/tmp cargo build`, `HOME=/x cargo test`, `LD_PRELOAD=… cargo build`, `FOO=1 cargo build` → `deny` (défaut).
- [x] **AC4 — axe egress inchangé.** `HTTPS_PROXY= cargo build` (et `HTTP_PROXY=`) → `deny`, refusé par son propre axe, non déplacé.
- [x] **AC5 — préfixe valide UNIQUEMENT devant `cargo`.** `CARGO_INCREMENTAL=0 bash -c 'cargo build'` → `deny` (le token après le préfixe fermé n'est pas `cargo`).
- [x] **AC6 — pas de régression.** `cargo test`/`build`/`run`/`clippy` inchangés `allow` ; `cargo publish` reste `deny`.
- [x] **AC7 — létalité + gate tier1 octet-identiques.** `_denial_is_terminal` et `is_tier1_auto_approve` non touchés ; alternation FERMÉE (jamais `\w+=`).

## Fire-Disposition

- **Feu** : famine du pilote sur commandes de build ordinaires (mika#2105, `error_max_turns` sans travail, mesure 2026-09-29). **Traité** : `bash-cargo` admet un préfixe d'affectation à liste fermée de 5 noms build/log et les sous-commandes lecture seule `--version`/`metadata`.
- **Vérif de sortie** : probe avant/après (4 positifs + 6 négatifs) ; `pytest`/`ruff`/`mypy`/`verify-pipeline` verts. QA MPC sur le code (GO gate ratifié). Redeploy + restart moteur ensuite (hors ce repo).
- **Résidu** : `RUSTFLAGS` et les cibles `make test-*`/`verify-*` (c) restent refusées, à trancher séparément côté ticket — fail-closed intentionnel.

## Preuve / vérif (verbatim)

Probe (source `policy.evaluate` + chain-guard `_bash_allow_is_chain_safe`) : les 4 positifs passent de deny→allow ; les 6 négatifs (dont `HTTPS_PROXY=` et `CARGO_INCREMENTAL=0 bash -c '…'`) restent deny ; régression cargo (test/build/run/clippy, publish) inchangée. `uv run pytest`, `uv run ruff check .`, `uv run mypy src`, `./scripts/verify-pipeline.sh main-updated` (docs+source) — voir le rapport de commit.

## Prédicat de déclenchement

`allow` (règle `bash-cargo`) ssi la commande matche `^(?:(?:<5 noms fermés>)=\S*\s+)*cargo\s+(<sous-commande admise>)\b` — c.-à-d. un préfixe (éventuellement vide, éventuellement chaîné) d'affectations dont CHAQUE nom est dans la liste fermée, suivi littéralement de `cargo` puis d'une sous-commande admise (`build|test|check|clippy|fmt|run|clean|doc|tree|metadata|--version`). Tout le reste tombe au default-deny.

## Références

- Ticket : cpp#242 (samidarko, 2026-09-29, pilote 2a6c2112 / tâche 63348699). Lié : cpp#184, mika#2105 / mika#2589.
- Modèle de liste fermée : cpp#45 (`make verify-bundled-skills`).
