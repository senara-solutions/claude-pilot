---
title: "fix(agent): avis unique à 50 tours du plafond (cpp#289)"
type: fix
status: active
date: 2026-10-29
issue: senara-solutions/claude-pilot#289
---

# Avis unique à 50 tours du plafond

## Pourquoi

Le 2026-10-14, trois vols d'implémentation sont morts sur `error_max_turns` (201/200, 201/200,
151/150) avec leur travail commité et sans PR (mika#2693). Le pilote connaît son compte de tours,
la session ne le voit pas.

## Décisions

1. **Canal** : un hook `PostToolUse` et `PostToolUseFailure` (tous outils) qui renvoie
   `hookSpecificOutput.additionalContext`. Le SDK 0.2.152 l'accepte sur les deux événements. Pas
   de second `query()`, pas de message utilisateur. L'événement d'échec compte : un tour dont
   l'outil échoue franchit aussi le seuil.
2. **Compte** : `guardrails.turns`, boucle principale seule depuis cpp#259, même unité que
   `maxTurns`. Un appel venu d'un sous-agent (`agent_id` présent) est ignoré et ne consomme pas
   l'injection.
3. **Seuil** : `TURNS_REMAINING_NOTICE = 50`, injection quand `turns >= maxTurns - 50`, une fois.
   Un plafond de 50 ou moins désarme l'avis. Aucun changement de plafond.
4. **Unicité** : le hook est créé une fois par session ; une reprise après refus réutilise les
   options, donc ne réarme pas l'avis. Après une reprise, le compte du pilote est cumulé : l'avis
   peut venir plus tôt que le compte du CLI, jamais plus tard.
5. **Texte** : « [claude-pilot] N turns left before maxTurns. Commit, push, skip to the end of
   the pipeline (verify, compound, PR). » Taille bornée par test.
6. **Journal** : `log_guardrail("turns_remaining_notice", "injected at turn T/M (N left)")`.

## Tests

`tests/test_turns_remaining_notice.py` prend le hook dans les options que `run_agent` remet au
`ClaudeSDKClient`, avec un vrai `SessionGuardrails` : sous le seuil jamais, au seuil une fois,
plafonds 150 et 200, échec d'outil, un seul avis pour les deux événements, sous-agent ignoré,
plafond désarmé, ligne `[guardrail]`, texte mince.

## Limite

Ces tests appellent le hook enregistré, ils ne prouvent pas que le CLI remet le contexte au
modèle. Une preuve live courte est proposée à part (abonnement Claude, zéro OpenRouter).

## Preuve live (2026-10-29)

`uv run claude-pilot --no-relay --max-turns 53` dans le clone, abonnement Claude seul, prompt de cinq
lectures (un `Read` par tour), puis question au modèle sur tout texte reçu commençant par
`[claude-pilot]`.

```
[2026-10-29T08:01:46.040Z] [guardrail] turns_remaining_notice: injected at turn 3/53 (50 left)
[2026-10-29T08:01:57.645Z] [done] Success | 6 turns | $0.70 | 18s
```

Réponse du modèle : le hook `PostToolUse:Read` après `pyproject.toml` a injecté, verbatim,
« [claude-pilot] 50 turns left before maxTurns. Commit, push, skip to the end of the pipeline
(verify, compound, PR). » Une seule injection sur six tours.

Observation : le modèle ne l'a pas suivi, en lisant le préfixe `[claude-pilot]` comme celui du
protocole relais (CLAUDE.md) et non comme une consigne. Ici le prompt demandait seulement de
rapporter ; en vol réel, la consigne va dans le sens du pipeline. À surveiller sur les premiers vols.
