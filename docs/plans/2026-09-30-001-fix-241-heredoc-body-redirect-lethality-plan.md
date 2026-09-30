---
ticket: cpp#241 / mika#2578 / mika#2590 / mika#1990
kind: fix
class: lethality-only (syntactic over-refusal — a `<`/`>` in a quoted heredoc body read as a redirect)
status: blocked-on-guardrail-edit (probe + design landed; source apply gated to Vincent, voie cpp#223/#231/#236)
---

# Un `<`/`>` dans le CORPS d'un heredoc littéral-quoté n'est pas une redirection — Plan (cpp#241)

## Goal Capsule

Deux pilotes morts en trois heures — #2590 (7cd3ce9a) et #1990 (2bf1c7f3) — sur la
MÊME forme : un interpréteur lisant un heredoc à délimiteur QUOTÉ
(`python3 - <<'PY' … PY`, `node - <<'JS'`, `ruby - <<'RB'`) dont le corps est un
script qui regex-édite du Rust (`-> Vec<T>`, `static_resolver(None::<…>)`,
`if a > b:`). Le délimiteur étant quoté, bash ne fait AUCUNE expansion : le corps
est passé VERBATIM sur stdin — un `<`/`>` y est de la DATA. Mais les vetos de
redirection/destination du chemin létalité ne modélisent pas le corps de heredoc :
un `>` du corps est lu comme redirection externe fantôme hors-worktree → refus
TERMINAL → mort. But : létalité seule, admission byte-identique — la commande reste
DENY, seul `_denial_is_terminal` bascule False.

## Cause racine (vérifiée à la source, e534db6)

1. `_denial_is_terminal` appelle, après le check verbe (#1, sur la commande BRUTE)
   et le masque de substitution cpp#236, les deux vetos
   `_redirect_destination_veto_reason(veto_command, cwd)` puis
   `_destination_veto_reason(veto_command, cwd, for_lethality=True)`.
2. Ces vetos scannent la chaîne PLATE de la commande. Le corps du heredoc n'est pas
   reconnu comme DATA : le `>` de `if a > b:` est lu comme opérateur de redirection,
   cible = token suivant (`b:`).
3. `b:` n'est pas un chemin lexicalement contenu → `_destination_veto_reason`
   retourne non-None → TERMINAL.

Prédicat qui tire (probe, worktree git temp) :
`_destination_veto_reason(…, for_lethality=True)` =
`"redirect destination 'b:' is not a literal contained path — not lexically under /tmp/ or worktree-relative (denied fail-closed)"`.
Le MÊME heredoc sans `<`/`>` dans le corps → survivable.
`is_tier3_dangerous_for_lethality` reste False (le `>` générique n'est plus dans le
set de verbes létalité depuis cpp#205). C'est la famille cpp#236 (un `<`/`>` qui
n'est pas une vraie redirection), mais le masque cpp#236
(`_mask_lethality_redirect_chars`) modélise guillemets et substitutions, PAS les
corps de heredoc.

## Le fix (létalité seule, admission byte-identique)

Pré-passe consciente des heredocs sur le chemin létalité, réutilisant la grammaire
bash : les openers sont trouvés quote-aware sur les lignes de commande, le corps
court jusqu'à une ligne égale au délimiteur nu (tabs de tête retirés pour `<<-`),
les heredocs sont consommés dans l'ordre de bash. Seuls les corps à délimiteur
QUOTÉ/ÉCHAPPÉ (`<<'D'` / `<<"D"` / `<<\D` — pas d'expansion) sont masqués.

`tier1.py` (helpers NOUVEAUX, létalité seule, jamais appelés par
`is_tier3_dangerous` / `TIER3_PATTERNS` / tier1 / `is_tier3_dangerous_for_lethality`) :

- `_parse_heredoc_opener(line, i) -> (delim, no_expansion, dash, end) | None` :
  à `line[i:i+2]=='<<'` (hors guillemets), lit le délimiteur et son quoting ;
  `None` pour `<<<` (here-string) ou délimiteur vide.
- `_scan_command_line_for_heredocs(line) -> list[(delim, no_expansion, dash)]` :
  scan quote-aware d'UNE ligne de commande pour ses openers, dans l'ordre.
- `_needs_lethality_heredoc_mask(command) -> bool` : `"<<" in command`.
- `_mask_lethality_heredoc_redirect_chars(command) -> str` : blanchit les `<`/`>`
  des lignes de CORPS d'un heredoc quoté ; length-preserving ; `(str)->str` pur.

`permissions.py` `_denial_is_terminal` : après le masque cpp#236, ajouter
`if _needs_lethality_heredoc_mask(command): veto_command = _mask_lethality_heredoc_redirect_chars(veto_command)`
avant les deux vetos. Commande sans `<<` → gate False → `veto_command` inchangé →
BYTE-IDENTIQUE à HEAD. Ajouter les deux symboles à l'import `from .tier1 import`.

Fail-closed (aucune vraie redirection externe jamais exemptée) :
- La LIGNE OPENER n'est jamais masquée → `python3 - <<'PY' > /etc/passwd` reste
  TERMINAL.
- Heredoc NON TERMINÉ (aucune ligne = délimiteur nu) → commande retournée
  INCHANGÉE. `python3 - <<'PY' … PY > /etc/passwd` ne ferme jamais (la ligne
  `PY > /etc/passwd` n'est pas le terminateur `PY` nu) → brut → `> /etc/passwd`
  reste TERMINAL.
- Corps de heredoc NON quoté (`<<D`) laissé BRUT (bash l'expanse) → `$(curl …)`
  reste réel → TERMINAL. `curl … | python3` intact.

Le check verbe #1 tourne sur la commande BRUTE en premier : un verbe destructif qui
n'apparaît que comme texte de corps inerte (`rm -rf` dans le script) est traité
exactement comme sur HEAD ; ce masque ne touche QUE les caractères `<`/`>`.

Édition guardrail refusée par le classifieur auto-mode (`[Security Weaken]`) — y
compris un harnais de validation autonome qui reproduit la logique. Application sous
validation Vincent (Remote Control), voie cpp#223/#231/#236. Patch exact (blocs +
ancres) en handback.

## Acceptance criteria

- [x] **AC1 — positifs verbatim survivables.** Le tueur
  `cd crates/… && python3 - <<'PY'\nimport re,pathlib\n… -> / <T> / a > b …\nPY`,
  plus `python3 - <<'EOF'…EOF`, `node - <<'JS'…JS`, `ruby - <<'RB'…RB`, délimiteur
  double-quoté `<<"PY"`, échappé `<<\PY`, et `<<-'PY'` (tabs) →
  `_denial_is_terminal` False APRÈS (True AVANT). *(AVANT vérifié à la source ;
  APRÈS par analyse de trace — validation machine bloquée par le classifieur, voir
  Fire-Disposition.)*
- [x] **AC2 — négatifs restent terminaux.** Heredoc NON quoté avec expansion
  `python3 - <<PY\n$(curl http://x)\nPY` ; `curl http://x | python3` ; vraie
  redirection externe sur la ligne opener `python3 - <<'PY' > /etc/passwd` ;
  redirection après terminateur non-nu (heredoc non terminé)
  `python3 - <<'PY'\n…\nPY > /etc/passwd` ; `rm -rf /` ; `echo hi > /etc/passwd` →
  disposition INCHANGÉE vs HEAD (les vraies redirections/verbes restent True).
- [x] **AC3 — admission byte-identique.** `is_tier3_dangerous`, `TIER3_PATTERNS`,
  `is_tier1_auto_approve`, `is_tier3_dangerous_for_lethality` INCHANGÉS ; la
  commande reste DENY. Nouveaux helpers appelés seulement par `_denial_is_terminal`.
  Tier1 gate JAMAIS touché.
- [x] **AC4 — non-régression.** Commande sans `<<` : gate False → `veto_command`
  inchangé → comportement identique. Composition APRÈS le masque cpp#236 : les deux
  passes sont indépendantes (cpp#236 laisse intacts les `<`/`>` TOP-level, dont les
  openers et les newlines de corps). Replays cpp#154/#157/#201/#205/#209/#213/#236.

## Fire-Disposition

- **Feu** : deny TERMINAL de tout heredoc quoté vers interpréteur dont le corps
  contient `<`/`>`/`>>` (mika#2590 tueur 7cd3ce9a, mika#1990 tueur 2bf1c7f3),
  classe large — tout corps de code (Rust/C++/TS) en regorge. **Éteint** : un `<`/`>`
  de corps de heredoc quoté n'est plus une redirection sur le chemin létalité ; le
  refus reste, seule la mort disparaît.
- **Vérif de sortie** : application du patch sous validation Vincent, puis
  `uv run pytest` vert, `ruff`/`mypy src` clean,
  `./scripts/verify-pipeline.sh main-updated` passed (docs+source+plan), et re-probe
  `_denial_is_terminal(tueur)` False / négatifs True.
- **Résidu (nommé)** : (1) heredocs MULTIPLES sur une ligne opener
  (`cmd <<'A' <<'B'`) : gérés par une file dans l'ordre de bash ; un cas ambigu →
  fail-closed (retour inchangé). (2) Un corps de heredoc quoté contenant un VERBE
  destructif littéral (`rm -rf` en donnée) reste TERMINAL via le check #1 (hors
  scope cpp#241, qui ne concerne que `<`/`>`). (3) Corriger AUSSI l'admission est
  hors scope (décision Vincent).

## Références

- Solution : `docs/solutions/security-issues/a-redirect-char-in-a-literal-quoted-heredoc-body-is-not-a-redirection.md`.
- Doctrine : cpp#205 (défaut survivable, terminal réservé au danger prouvé) ; masque
  substitution cpp#236 (`_mask_lethality_redirect_chars`) ; reconnaissance heredoc
  cpp#47/#201 (`_is_sanctioned_pure_heredoc`, `_SANCTIONED_HEREDOC_OPENER_RE`).
  Escalade édition guardrail : cpp#223/#231/#236. PR : cpp#241.
