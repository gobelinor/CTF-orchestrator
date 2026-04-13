# CTF Orchestrator — Audit

Audit objectif du projet. Etat des lieux, forces, faiblesses, axes d'amelioration.

**Readiness: 6/10 hobby CTF, 4/10 competitive, 3/10 production.**

---

## Ce qui marche bien

- **State machine LangGraph** claire et extensible. Flow route → specialist → analyze → decide → loop. Minimal, testable.
- **Decision engine** couvre les scenarios courants: flag → writeup → memory → stop, stagnation overrides, budget checks, worker recommendations.
- **Working memory v2** bonne abstraction: hypothesis tracking, branches, rejection history, backend performance. Tailles bornees.
- **Agent runtime unifie**: budget, cache, mock, model routing, semaphore. Solide.
- **Test coverage** correcte (~85 tests non-langgraph). Decision logic, stagnation, JSON extraction, schema validation, parallel trajectories.
- **Skills architecture** propre: core methodology centralisee, 14 solvers lean, progressive disclosure avec reference files.

## Ce qui ne marche pas

### Critique — a fixer avant utilisation reelle

| # | Probleme | Ou | Impact |
|---|---|---|---|
| 1 | **Boucle infinie RETRY_REFRAMED** — pas de compteur de reframes. Si le LLM reframe sans progres, boucle jusqu'a max_attempts (gaspille tokens) | graph.py `_choose_decision` | Token waste, stagnation non detectee |
| 2 | **Timeout subprocess perd la sortie partielle** — si le LLM a output un flag avant timeout, il est perdu | workers.py invoke | Flag manque, faux negatif |
| 3 | **Reassess category ne previent pas les boucles** — re-route vers la meme categorie car pas de tracking des categories deja essayees | graph.py route node | Boucle de re-categorisation |
| 4 | **Working memory pas flush a chaque attempt** — si crash mid-challenge, progres perdu | graph.py state persistence | Perte de travail |
| 5 | **Echec Graphiti silencieux** — memory agent retourne None sans log ni event | memory.py | Perte de knowledge silencieuse |

### Haut — a fixer avant scaling

| # | Probleme | Ou | Impact |
|---|---|---|---|
| 6 | **Stagnation string similarity trop loose** — Jaccard >= 0.6 sur tokens, faux positifs sur hypotheses differentes ("SQLi on field X" vs "SQLi on field Y") | graph.py `_detect_stagnation_signals` | Switch backend premature |
| 7 | **Pas de prompt caching SDK** — subprocess `claude -p` pas de cache. Chaque appel re-envoie le system prompt complet | agent_runtime.py | Token waste ~30% |
| 8 | **Budget exhaustion pas graceful** — stop brutal mid-campagne, pas de flush partial | agent_runtime.py | Travail perdu |
| 9 | **JSON extraction perd les payloads partiels** — malformed JSON = blocked au lieu d'extraire ce qui est recuperable | agent_runtime.py `extract_json` | Faux negatifs |
| 10 | **Specialist prompts trop courts** — ~16 lignes de guidance reelle par solveur. Pas d'exemples, pas de failure modes | skills/ctf-*-solver | LLM fait du generique |

### Moyen — nice to have

| # | Probleme | Ou |
|---|---|---|
| 11 | Worker `needs_human=true` accepte aveuglément — devrait demander 2 signaux | graph.py |
| 12 | Backend scoring sans recency weighting — vieux echecs penalisent autant que les recents | graph.py |
| 13 | Pas de validation format flag contre le challenge spec | graph.py |
| 14 | Core methodology injectee en texte dans chaque prompt — pas de shared system prompt | skills/ |
| 15 | .runs/ bloat sans cleanup policy — 400+ fichiers apres 100 challenges | graph.py |
| 16 | Pas de progress indicator pendant les runs longs | supervisor.py |
| 17 | `_choose_decision()` 80 lignes de ifs imbriques — devrait etre decompose | graph.py |

### Bas — refactoring

| # | Probleme | Ou |
|---|---|---|
| 18 | Pas de docstrings sur decision logic et memory evolution | graph.py |
| 19 | Stagnation signal names = magic strings, devrait etre enum | graph.py |
| 20 | Misc solver vide — juste "apply core methodology" sans heuristiques | skills/ctf-misc-solver |
| 21 | Pas de routing multi-categorie (web+crypto) | skills.py |
| 22 | Pas de migration working memory v1 → v2 | graph.py |

---

## Features a implementer

### Priorite haute

- **Reframe counter** dans working memory. Limite a 2, puis force SWITCH_BACKEND.
- **Partial JSON recovery** — extraire les champs meme si JSON casse.
- **Category loop prevention** — tracker les categories essayees, exclure du re-routing.
- **State flush apres chaque attempt** — pas seulement en fin de challenge.
- **Log explicite sur echec Graphiti** — event + stderr.

### Priorite moyenne

- **Prompt caching** — migrer vers Anthropic SDK avec `cache_control` sur system prompt. Economie ~30% tokens.
- **Observability** — metriques: success rate par categorie, avg tokens par challenge, backend win rate. Fichier `metrics.jsonl`.
- **Human-in-the-loop** — si 2x needs_human, pause et demande input CLI avant de continuer.
- **Graceful budget shutdown** — flush pending, generate partial writeups, exit proprement.
- **Enrichir specialist prompts** — 2-3 failure modes par categorie, exemples concrets.

### Priorite basse

- Rolling log cleanup (.runs/ older than N runs).
- Multi-category routing (primary + secondary skill).
- Flag format validation contre challenge spec.
- Progress bar / ETA estimation.
- Estimated cost display avant lancement campagne.

---

## Architecture decisions a reconsiderer

1. **subprocess vs SDK** — `claude -p --json-schema` est simple mais empeche le prompt caching, le streaming token-level, et le retry automatique. SDK Anthropic donnerait cache + retry + cost tracking.

2. **Stagnation 8 signaux** — sur-ingenierie. 4 suffiraient: `consecutive_blocked`, `identical_summaries`, `hypothesis_loop`, `command_repetition`. Les autres (slow_drift, confidence_downtrend) ont trop de faux positifs.

3. **Backend performance scoring** — trop simple (static solve_rate). Devrait decay dans le temps et tracker par categorie.

4. **Event system** — Callable simple, pas de queue. Si un handler bloque, le pipeline bloque. Devrait etre async ou fire-and-forget.

---

## Ce qui est sur-ingenierie

- 8+ stagnation signals (4 suffisent)
- Backend performance tracking avec limits mais sans LRU
- Memory schema v2 freeze sans migration path
- 17 constantes MEMORY_*/RESUME_* (hardcode ok, mais les valeurs sont arbitraires — jamais tunees sur des runs reels)

## Ce qui est sous-ingenierie

- Error recovery (timeout, JSON, Graphiti, budget)
- Prompt quality (specialist trop courts)
- Observability (pas de metriques)
- Persistence cross-crash
- Human-in-the-loop

---

*Audit genere le 2026-04-13.*
