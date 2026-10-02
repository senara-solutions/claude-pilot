---
issue: claude-pilot#262
title: "[policy:deny] n'affiche que le début d'un script multi-ligne et ne nomme pas le segment refusé — observabilité"
type: fix
scope_repo: claude-pilot
priority: p2-observability
date: 2026-10-02
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# [policy:deny] : une ligne physique nommant le segment refusé + un hash — Plan

## Goal Capsule

**Objectif.** Un défaut d'OBSERVABILITÉ, pas de politique. Pour un script
multi-ligne, la ligne `[policy:deny]` n'affichait que le DÉBUT de la commande
(`str(command)[:200]`, retours à la ligne BRUTS conservés) et ne nommait jamais
le segment refusé. Conséquences mesurées (cpp#256) : un `grep '[policy:deny]'`
ne rendait que la 1re ligne physique ; le `rule_id` et le suffixe de létalité
`(terminal)/(non-terminal)` (cpp#151) atterrissaient sur une ligne ultérieure
SANS tag, ou disparaissaient à la troncature. Les « 15 refus de `cd <WT> && …` »
de 57ad9d76 étaient en réalité des scripts `cd <WT>⏎…` dont la ligne fautive
était ailleurs (`make`, `cargo run`, `env`, `sed -i`). Prime, 2026-10-01 : « On a
failli signer une frontière de sécurité sur un log trompeur. »

**Ce que ça fait.** La ligne `[policy:deny]` devient UNE ligne physique
(retours à la ligne échappés en `⏎`), NOMME le segment fautif (`segment="…"
cause=…`) et porte un hash court du commande complète (`sha256:<12 hex>`) pour
la recoller au transcript. **Aucun changement d'admission ni de létalité** : la
matrice des gates (`is_tier1_auto_approve` / `is_tier3_dangerous` /
`is_tier3_dangerous_for_lethality` / `_denial_is_terminal` / décision) est
byte-identique. Seule la CHAÎNE de journal change.

## Cause établie à la source (HEAD de la branche)

- `permissions.py::_summarize_input` : `_scrub_secrets(str(command)[:200])` —
  troncature 200 car., `\n` bruts conservés.
- `ui.py::log_policy_deny` imprimait `[policy:deny] Bash: {detail}{[rule_id]}
  {suffixe}` d'un bloc. Les lignes 2..n du `detail` partaient sans tag → le
  `grep` ne rendait que la 1re.
- La décision est pourtant prise SEGMENT PAR SEGMENT
  (`_bash_allow_is_chain_safe`, tier3 par segment, `_destination_veto_reason`
  par segment) — mais aucun champ ne nommait le segment.

## Conception

Chemin diagnostique SÉPARÉ, consommé UNIQUEMENT par la ligne de journal :

1. `permissions._diagnose_refused_bash(policy, command, cwd)` re-parcourt les
   segments (`_split_compound_command`) et renvoie le PREMIER fautif + sa cause
   via `_segment_refusal_cause`, qui réplique l'ordre de la décision :
   `dest-veto:…` (frontière de confinement) → `tier3` → (tier1-safe ou
   allow-propre = non-fautif) → `default-deny` (aucune règle) / `chain-unsafe`
   (règle de refus explicite). Tout est en LECTURE SEULE.
2. `permissions._command_hash` = `sha256(full_command)[:12]`.
3. `permissions._bash_deny_display_detail` (AC3) : si le segment fautif tombe
   HORS de la fenêtre 200, l'afficher en priorité au lieu du début du script.
4. `permissions._bash_deny_log_fields` emballe `(display, kwargs)` pour les 4
   sites de refus Bash ; enveloppé dans un `try/except` qui renvoie
   `(detail, {})` — le diagnostic ne peut JAMAIS planter ni altérer le refus.
5. `ui.log_policy_deny` / `log_policy_deny_with_notify` : `_one_physical_line`
   échappe `\n`→`⏎` ; `_deny_diag_fields` ajoute `segment=/cause=/sha256:` en
   APPEND. Préfixe `[policy:deny] <tool>: ` et suffixe de létalité INCHANGÉS.

## Fire-Disposition

- **Feu** : un log de refus qui ne montrait que la ligne 1 d'un script
  multi-ligne et misattribuait le refus — a failli faire signer cpp#256.
  **Traité** : une ligne physique, segment nommé + cause, hash récupérable.
  **Décision NON touchée** — preuve byte-identique de la matrice des gates
  (test `test_262_diagnostic_does_not_change_the_gate_matrix`).
- **AC5 (invariant dur)** : le diagnostic est pur (aucun gate appelé ne mute) ;
  les 4 sites ne changent QUE les arguments de `log_policy_deny` ; les retours
  `PermissionResultDeny`/`interrupt` sont inchangés. Les tests cpp#151 (qui
  épinglent `interrupt` + suffixe) passent sans modification.
- **AC6 (consommateurs)** : in-repo — aucun parseur du format interne de
  `[policy:deny]` trouvé (seul `scripts/measure-idle-lethality.sh` grep des
  tags `[guardrail]`/`[done]`, pas le corps du deny). Cross-repo —
  `dispatch-lib.sh` (mika#1097) relit `[policy:deny]` depuis le `.stderr` et le
  Signal S ancre `^dispatch-lib: ` : le préfixe `[policy:deny] Bash: <une
  ligne>` et la forme UNE-ligne sont PRÉSERVÉS, les champs s'AJOUTENT.
  **MPC doit vérifier/mettre à jour `dispatch-lib.sh` hors-repo.**
- **Résidu** : le `cause=dest-veto: …` reprend verbatim la raison du veto
  (potentiellement longue mais bornée). La cause par segment est best-effort
  (un diagnostic), jamais une re-décision.

## Preuve / vérif (verbatim)

`uv run pytest` → **1736 passed** ; `uv run ruff check .` → clean ;
`uv run mypy src` → clean ; `./scripts/verify-pipeline.sh main` → passed
(docs + source).

Ligne rendue (script dont la ligne 3 est fautive) :

```
[policy:deny] Bash: cd /wt⏎echo building⏎make build⏎cargo run segment="make build" cause=default-deny (1 of 2) sha256:17acb74c5326 [bash-cd] (non-terminal)
```

## Références

- Solution : `docs/solutions/tooling-decisions/a-deny-log-that-shows-only-line-one-misattributes-the-refusal.md`.
- Lié : cpp#256 (dormant — la prémisse venait de ce log), cpp#259 (même
  famille : un signal qui mentait, le compteur de tours), cpp#151 (suffixe de
  létalité, préservé), mika#1097 (dispatch-lib relit le `.stderr`).
