---
title: "fix(logger): les écritures du journal quittent le thread de la boucle (cpp#171)"
type: fix
status: active
date: 2026-10-29
issue: senara-solutions/claude-pilot#171
---

# Les écritures du journal quittent le thread de la boucle

## Mesure d'abord (2026-10-29, 200 derniers journaux pilotes, 1,77 M lignes)

- 96,2 % des lignes sont des `[debug] stream event` : dispatch-lib passe `--verbose` sans
  condition (mika#2043).
- Seconde la plus chargée d'un journal : médiane 1 983 lignes, p95 4 398, max 5 633.
- Chaque ligne faisait write + flush sur stderr **et** write + flush sur le fichier, en synchrone
  sur le thread qui exécute aussi `_idle_watchdog`. stderr est lui-même un fichier (redirection
  `.stderr` de dispatch-lib) : décharger `_write_file` seul laisserait la moitié des écritures sur
  la boucle.

## Décisions

1. **Un thread écrivain unique**, nourri par une file bornée. `write_log` et `write_file_log`
   horodatent en synchrone (calcul pur, l'ordre est fixé à l'appel), puis déposent ; le thread
   écrit stderr et fichier dans l'ordre d'arrivée, par lots, un flush par lot.
2. **Opt-in** : `start_background_writer()`, appelé par `cli.main`. Sans lui, comportement
   synchrone identique (tests, usage bibliothèque).
3. **Jamais bloquant** : file pleine (100 000 éléments, sink bloqué) ⇒ la ligne est comptée et
   perdue, puis `[logger] dropped N lines: sink too slow` est écrit dès que le sink repart.
4. **Vidange** : `flush_log(timeout)` (marqueur dans la file). Appelé par `close_file_log`, par
   `atexit`, et en tête des branches fatales du CLI pour que la trace fatale reste la dernière.
5. **Erreurs** : règle inchangée, premier échec d'écriture fichier ⇒ un avertissement, sink coupé ;
   un fichier fermé sous l'écrivain (vidange expirée) coupe le sink sans exception.

## Coût assumé

Un SIGKILL perd les lignes encore en file (quelques millisecondes), c'est-à-dire les toutes
dernières lignes d'un pilote qui meurt ainsi. Les écritures directes et rares sur stderr hors
logger (avertissements de `cli.py`, `audit.py`…) peuvent passer quelques millisecondes avant des
lignes encore en file.

## Option non retenue

Réduire le volume à la source (une ligne de résumé par bloc au lieu d'une par
`content_block_delta`, environ dix fois moins de lignes). Plus efficace, mais elle change le
contenu du journal voulu par cpp#125 ; décision à part.

## Tests

`tests/test_logger_background_writer.py` : témoin négatif (sans écrivain, un sink lent bloque
l'appelant) ; sink à 0,5 s par écriture ⇒ 50 `write_log` en < 50 ms et boucle asyncio réactive
(< 50 ms) ; 2 500 lignes dans l'ordre jusqu'au fichier, ANSI retiré ; échec fichier ⇒ un seul
avertissement ; file pleine ⇒ appelant non bloqué, compte rapporté ; `flush_log` signale un sink
qui ne se vide pas.
