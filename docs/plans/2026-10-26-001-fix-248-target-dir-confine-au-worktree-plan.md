---
title: "fix(permissions): le répertoire de build de cargo est confiné au worktree (cpp#248)"
type: fix
status: active
date: 2026-10-26
issue: senara-solutions/claude-pilot#248
---

# Le répertoire de build de cargo est confiné au worktree

## Pourquoi

`--target-dir` et `CARGO_TARGET_DIR=` désignent l'endroit où cargo **écrit** tout son build. Aucun
des deux ne passe par le veto de destination qui garde `cp`, `mv`, `mkdir` et les redirections.

Rejeu sur `origin/main` ec63e2d (handler complet, politique groupée, cwd = worktree) : les 22 formes
de `.pilot-scratch/rejeu-main.txt` sont toutes admises, sauf `env …` et `export …` (déjà refusées
par d'autres règles). Le drapeau passe par tier1 (`is_safe_build_command`, `AUTO`) ; la variable,
par la règle `bash-cargo` (liste fermée de cpp#242) puis le veto chain-safe, qui ne lit pas la
valeur.

Usage réel mesuré dans `/var/log/claude-pilot` (6 405 fichiers) : `CARGO_TARGET_DIR=/tmp/mika-check`
(22), `/tmp/mika-build` (18), `/home/samidarko/.cargo-targets/mika-991` (10), et
`/data/workspace/mika-platform/mika/target` (6) — le `target/` du **checkout de déploiement**.

## Décisions

1. **Un seul juge, avec le cwd.** `permissions._cargo_target_dir_escapes(command, cwd)` lit chaque
   valeur et la refuse si elle n'est pas un chemin littéral (expansion, glob, accolade, `~`, vide),
   si elle contient un composant `..`, si elle sort du worktree (`is_within_project`, résolu,
   sensible aux liens symboliques) ou si elle tombe sur le plan de contrôle
   (`_is_control_plane_path` : `.git/`, `.claude/`, `.github/workflows/`, …). Sans cwd, tout
   chemin absolu est refusé et le relatif est jugé lexicalement. Un chemin absolu qui se résout
   **dans** le worktree reste admis (cpp#176, même doctrine que cp/mv).
2. **Le relevé des valeurs vit dans tier1** (`cargo_target_dir_values`, mots après retrait des
   guillemets par `_lex_shell_words`) : tout mot `CARGO_TARGET_DIR=…` de la commande, et
   `--target-dir V` / `--target-dir=V` après un mot `cargo`. Un segment illisible qui mentionne
   `cargo` ou `CARGO_TARGET_DIR` est refusé.
3. **Deux points d'appel.** tier1 `is_safe_build_command` n'admet plus un cargo qui porte un
   répertoire de build : il n'a pas de cwd, donc il laisse décider la couche politique.
   `_bash_allow_is_chain_safe` prend un `cwd` optionnel et oppose le veto ; le seul appel de
   production (le handler) le passe.
4. **Létalité : survivable.** Le ticket la renvoie au groom (point 3), qui n'a pas eu lieu. Le refus
   passe par le veto chain-safe, dont la létalité reste celle de `_denial_is_terminal` : non
   terminal pour ces formes. Les 40 usages `/tmp/mika-*` mesurés deviennent un refus auquel le
   pilote peut s'adapter, pas une mort de session. Rendre ce refus terminal (alignement sur le veto
   de destination) reste une décision à prendre, sans rien changer au refus lui-même.
5. **Croisement cpp#242 déclaré, non tranché.** Le corpus d'admission de cpp#242 dans
   `tests/test_tier1_cargo_config.py` épingle `CARGO_INCREMENTAL=0 CARGO_TARGET_DIR=/x cargo test
   --no-run` comme admis. C'est exactement la forme que #248 refuse. Le test passe dans la liste
   des refus ; la liste fermée des **noms** de cpp#242 n'est pas touchée et rien n'est élargi.

## Hors périmètre

- cpp#242 (préfixes d'affectation refusés à tort) : aucun changement de la règle `bash-cargo`.
- `cd /ailleurs && cargo build` (le `target/` par défaut suit le répertoire courant) : autre axe.

## Tests

`tests/test_cargo_target_dir.py` : les formes du ticket refusées de bout en bout, une par une et
pour chacune des trois écritures (`--target-dir X`, `--target-dir=X`, `CARGO_TARGET_DIR=X`) ;
`.pilot-scratch/t`, `target-alt` et `<worktree>/target-alt` admis ; un lien symbolique sortant
refusé ; la létalité non terminale épinglée ; tier1 ne l'admet plus seul.
