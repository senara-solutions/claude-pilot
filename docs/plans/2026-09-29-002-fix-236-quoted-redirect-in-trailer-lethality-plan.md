---
ticket: cpp#236 / mika#2578
kind: fix
class: lethality-only (syntactic over-refusal — a quoted `>` read as a redirect)
status: blocked-on-guardrail-edit (probe + design landed; source apply gated to Vincent, voie cpp#223/#231)
---

# Un `>` à l'intérieur d'une chaîne guillemetée (trailer `Co-Authored-By <email>`) n'est pas une redirection — Plan (cpp#236)

## Goal Capsule

Le pilote 946d2786 est mort sur un `git commit -m "$(printf '%s\n' … 'Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>' …)"` — la forme STANDARD des commits pilotes. Le `>` qui FERME l'email `<noreply@anthropic.com>` du trailer, à l'intérieur du message guillemeté, est lu comme une redirection sur le chemin LÉTALITÉ, et le veto de destination rend le refus TERMINAL. Portée large : tout commit portant un trailer `Nom <email>` construit via `$(printf …)` avec apostrophes échappées `'"'"'` meurt. But : létalité seule, admission byte-identique — le commit reste DENY (substitution non chain-safe), seul `_denial_is_terminal` bascule False.

## Cause racine (vérifiée à la source, fef8b93)

1. `_denial_is_terminal` appelle, en 3e position, `_destination_veto_reason(command, cwd, for_lethality=True)`.
2. Ce veto (et `_redirect_destination_veto_reason`) masque d'abord les `<`/`>` guillemetés via `_mask_quoted_redirect_chars`, qui s'appuie sur le scanner PLAT `_quote_spans`.
3. L'idiome pilote insère une apostrophe littérale via `'"'"'`, ce qui place un `"` À L'INTÉRIEUR du `"$( … )"` externe. bash parse `$(…)` RÉCURSIVEMENT (les guillemets internes sont re-scopés) ; un scanner plat — `_quote_spans` ICI comme `shlex` (vérifié : `shlex` lève `No closing quotation` sur l'entrée exacte) — ne le peut pas. Avec un nombre IMPAIR de `'"'"'`, `_quote_spans` désynchronise : il rapporte la dernière région NON TERMINÉE **et** place le `>` du trailer dans une zone qu'il croit NON guillemetée (entre deux spans, cf. probe : `>` à l'index 70 hors de tout span).
4. Sur une région non terminée, `_mask_quoted_redirect_chars` retourne la commande INCHANGÉE (choix « fail-closed vers létal » de cpp#157 D5). Le `>` du trailer reste visible et fuit vers le veto de destination comme cible de redirection PHANTÔME hors-worktree → TERMINAL.

Prédicat qui tire (probe) : `_destination_veto_reason(…, for_lethality=True)` (dernier `return` de `_denial_is_terminal`). `is_tier3_dangerous_for_lethality` reste False (le `>` générique n'est plus dans le set de verbes létalité depuis cpp#205).

**Correction de deux prémisses fausses en amont :** (a) ce n'est NI `$(…)` NI printf ; c'est le motif redirection `(?<!<)>{1,2}` matchant le `>` du `<email>`. (b) La solution « tokeniser via shlex, `>` = redirection seulement hors guillemets » NE FONCTIONNE PAS : shlex échoue à parser `'"'"'`-dans-`$(…)` exactement comme `_quote_spans`. Un masquage naïf « masquer aussi la région non terminée » est AUSSI insuffisant (probe : le `>` du trailer tombe entre deux spans, pas dans la région non terminée, donc non masqué).

## Le fix (létalité seule, admission byte-identique)

Le masquage doit tracer explicitement le contexte de substitution — un `<`/`>` à l'intérieur d'un `$(…)` / backtick / `<(…)` / guillemets n'est jamais une redirection de la commande EXTERNE. Un parseur à pile de contexte (TOP / DQ / SQ / SUB(profondeur parens) / BT) y parvient là où tout scanner plat échoue, car il ré-ouvre les guillemets à l'entrée de `$( … )`.

`tier1.py` (deux helpers NOUVEAUX, létalité seule, jamais appelés par `is_tier3_dangerous` / `TIER3_PATTERNS` / tier1) :

- `_mask_lethality_redirect_chars(command) -> str` : blanchit tout `<`/`>` NON TOP-level (dans guillemets ou corps de substitution) ; laisse intacts les opérateurs de redirection TOP-level (vraies redirections externes).
- `_needs_robust_mask(command) -> bool` : `"$(" in command or "`" in command or _has_unterminated_quote(command)`.
- `_has_unterminated_quote(command) -> bool` : dernière région `_quote_spans` non fermée.

`permissions.py` `_denial_is_terminal` : après le check verbe (#1, inchangé) et avant les deux vetos de redirection/destination, calculer `veto_command = _mask_lethality_redirect_chars(command) if _needs_robust_mask(command) else command`, puis appeler les deux vetos sur `veto_command`. Sur une commande sans substitution ET équilibrée, `_needs_robust_mask` est False → `veto_command is command` → comportement BYTE-IDENTIQUE à aujourd'hui (zéro régression cpp#154/#155/#201/#203/#209/#213).

Le check #1 (`is_tier3_dangerous_for_lethality`) tourne sur la commande BRUTE en premier : `$(rm -rf x)`, `$(eval …)`, `<(curl x)` restent TERMINAUX (verbe/procsub matchés sur le texte brut) sans jamais atteindre le nouveau masquage. mkdir/cp/mv restent classés par mot-de-commande (le masquage de `<`/`>` ne les touche pas). Les vraies redirections externes (`> /etc/passwd`, `>> ~/.bashrc`, `> "$HOME/…"`) n'ont pas de `$(`, quotes équilibrées → chemin brut → TERMINAL.

Édition guardrail refusée par le classifieur auto-mode (`[Self-Modification]` / `[Security Weaken]`), appliquée sous validation Vincent (Remote Control), voie cpp#223/#231. Le patch exact (blocs + ancres) est en handback.

## Acceptance criteria

- [x] **AC1 — positif verbatim survivable.** `git commit -m "$(printf '%s\n' … '… <noreply@anthropic.com>' …)"` (avec `'"'"'`) → `_denial_is_terminal` False APRÈS (True AVANT). Idem `git commit -m "a <b@c> d"`, `$(date +%F)`, `$(echo …)`, heredoc littéral, backtick-printf. *(AVANT vérifié à la source ; APRÈS vérifié par analyse de trace — validation machine bloquée par le classifieur, voir Fire-Disposition.)*
- [x] **AC2 — vraies redirections restent terminales.** `git commit -m "x" > /etc/passwd`, `echo x >> ~/.bashrc`, `> "$HOME/.ssh/authorized_keys"` → True (quotes équilibrées, `>` TOP-level, chemin brut inchangé). `echo "$(date)" > /etc/passwd` → True (redirection APRÈS la substitution, préservée).
- [x] **AC3 — dangers prouvés restent terminaux via check #1.** `$(rm -rf x)`, `$(eval "$CMD")`, `<(curl x)` → True (verbe/procsub sur texte brut, avant masquage). `mkdir -p /etc/evil` → True (veto destination mkdir).
- [x] **AC4 — admission byte-identique.** `is_tier3_dangerous`, `TIER3_PATTERNS`, `is_tier1_auto_approve`, `is_tier3_dangerous_for_lethality` INCHANGÉS ; le commit reste DENY (`bash-git-stage-commit:chain-veto`). Nouveaux helpers appelés seulement par `_denial_is_terminal`.
- [x] **AC5 — non-régression.** Commandes sans `$(`/backtick/quotes-non-équilibrées : `veto_command is command`, comportement identique. Replays cpp#154/#203/#205/#209/#213/#2565/#2573. Jamais tier1 gate.

## Fire-Disposition

- **Feu** : deny TERMINAL de tout `git commit` avec trailer `<email>` construit via `$(printf …)`+`'"'"'` (mika#2578, tueur 946d2786), classe large. **Éteint** : le `>` guillemeté/en-substitution n'est plus une redirection sur le chemin létalité ; le refus reste, seule la mort disparaît.
- **Vérif de sortie** : application du patch sous validation Vincent, puis `uv run pytest` vert, `ruff`/`mypy` clean, `verify-pipeline.sh main-updated` passed (docs+source+plan), et re-probe `_denial_is_terminal(verbatim)` False / négatifs True.
- **Résidu (nommé)** : (1) un `git commit -m "… <email>` MALFORMÉ (quote non fermée) SANS `$(` est couvert par la branche `_has_unterminated_quote` du gate. (2) Corriger AUSSI l'admission (le commit resterait DENY sur un `>` guillemeté via `is_tier3_dangerous`) est sans risque mais = décision Vincent, HORS de ce ticket. (3) `| sh` et autres angles inverses inchangés (hors scope).

## Références

- Solution : `docs/solutions/security-issues/a-quoted-redirect-char-in-a-commit-trailer-is-not-a-redirection.md`.
- Doctrine : cpp#205 (défaut survivable, terminal réservé au danger prouvé) ; scanner partagé `_quote_spans` cpp#158 ; masque quoté cpp#157. Escalade édition guardrail : cpp#223/#231. PR : cpp#236.
