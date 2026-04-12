# CTF Orchestrator

Orchestrateur multi-agents. Prompt-driven. LangGraph + subprocess claude/codex.

> Outil dangereux. Brûle tokens, dépense budget, gonfle dette cognitive. Vous voilà prévenus.

## Ce que ça fait

- Import challenge depuis URL / fichier / stdin → agent prompt-driven.
- Route vers skill spécialisé. Lance worker `mock` / `codex` / `claude`.
- Fait évoluer working memory v2 (hypothèses, branches, stagnation).
- Décide: `retry`, `reframe`, `switch_backend`, `stop`, `writeup`, `memory_persist`, `reassess_category`.
- Summarizer cheap compresse sorties workers longues.
- Trajectoires parallèles best-of-N (first-flag-wins).
- Runtime unifié: budget, cache, log, mock, model routing par rôle.
- Writeup agent. Memory agent → Graphiti MCP.
- Campagne board: queue + capacités bornées + supervisor agent inter-challenge.

## Méthodologie des solveurs

Tous les solveurs suivent la même Resolution Loop centralisée dans `ctf-core-methodology`:

1. **Recon** — énoncé, artefacts, prior attempts, handoff files
2. **Surface mapping** — cible exacte nommée
3. **Hypothèse** — primitive / vulnérabilité / misconfig ciblée
4. **Quick wins** — tests à coût faible qui confirment ou invalident rapidement
5. **Research** — Graphiti MCP d'abord, puis web (arXiv / writeups / GitHub) si le pattern est inhabituel
6. **Exploit progressif** — scripter, itérer, réduire
7. **Validation** — flag contre format attendu, output structurée remplie honnêtement

Les skills spécialisés (`ctf-*-solver`) sont intentionnellement courts (~25 lignes). Ils listent seulement les quick wins, pivots et outils propres à la catégorie. La méthodologie, la policy, les guardrails, les structured result fields vivent une seule fois dans `ctf-core-methodology` et sont chargés avant chaque skill.

Pour les challenges complexes, des références détaillées sont chargées à la demande (`skills/ctf-ai-llm-solver/reference/attacks.md` pour la taxonomie des attaques IA/LLM, par exemple) — zéro coût en contexte tant qu'elles ne sont pas lues.

## Skill AI/LLM

`ctf-ai-llm-solver` couvre les challenges qui ciblent des systèmes ML/LLM:

- Prompt injection (direct, indirect via RAG/document, system prompt leak, context extraction)
- Jailbreak on-manifold (persona, DAN, fiction, encodage) et off-manifold (GCG, suffixes adversariaux)
- Adversarial examples (texte via token embeddings, image FGSM, audio Whisper)
- Model extraction / inversion, embedding inversion
- RAG poisoning, vector DB manipulation
- Audit fichiers modèles (`.pt`, `.pth`, `.onnx`, `.safetensors`, pickle danger)
- Alignment bypass, abliteration (open weights)
- Agent / tool use exploitation

Le skill interroge systématiquement Graphiti MCP (`ecole2600_securite_ia`, `ctf_writeups`) au début de chaque challenge pour retrouver des techniques connues, des writeups de challenges similaires, et des patterns de plateformes custom.

Catégories routées vers ce skill: `ai`, `llm`, `ai_llm`, `ml`, `prompt injection`, `jailbreak`, `adversarial`, `securite_ia`, `ia`, `genai`.

## Flag extraction

Priorité:

1. **Agent field** `WorkerResult.flag` — primaire. Le solveur déclare lui-même le flag dans sa sortie JSON.
2. **Regex permissive fallback** — si flag null, scanner `summary` + `raw_output` avec `[A-Za-z0-9_\-]{2,24}\{...\}`. Catches `HTB{}`, `THM{}`, `picoCTF{}`, `EPT{}`, `ABCD{}`, `flag{}`, `CTF{}`, et tout préfixe word-like court.

Pas de env var à gérer. Agent reste source of truth.

## Agents

| Agent | Rôle |
|---|---|
| `ctf-import-agent` | Normalise source brute (texte/HTML/JSON/custom) → ImportedChallenge. Drive aussi le launch d'instance si besoin. |
| `ctf-category-router` + `ctf-core-methodology` | Catégorie + méthodo transverse injectée dans chaque solveur. |
| `ctf-*-solver` | 14 spécialisés: web, crypto, pwn, reverse, forensics, stego, misc, mobile, blockchain, cloud, hardware, jail, osint, **ai_llm**. |
| `ctf-supervisor-agent` | Décisions aux points inter-challenge. |
| `ctf-writeup-agent` | Writeup final post-solve. |
| `ctf-memory-agent` | Persiste knowledge réutilisable dans Graphiti MCP (`ctf_writeups`). |

## Schéma 1 — architecture globale

```
                          ┌─────────────────────────────────────┐
                          │          skills/ (prompts)          │
                          │                                     │
                          │  ctf-import-agent                   │
                          │  ctf-supervisor-agent               │
                          │  ctf-category-router                │
                          │  ctf-core-methodology               │
                          │  ctf-{web,crypto,pwn,rev,...}-solver│
                          │  ctf-writeup-agent                  │
                          │  ctf-memory-agent                   │
                          └──────────────────▲──────────────────┘
                                             │ SKILL.md injecté dans prompt
                                             │
┌──────────────────┐   ┌──────────────────────┴──────────────────────┐
│  agent_runtime   │◄──┤  invoke_agent()                             │
│                  │   │                                             │
│ budget tracker   │   │  writeups.py     memory.py                  │
│ semaphore        │   │  import_agent    supervisor_agent           │
│ extract_json     │   │  graph.py (summarizer inline)               │
│ schema validate  │   └────────────────────┬────────────────────────┘
│ cache sha1       │                        │ subprocess claude/codex
│ model routing    │                        ▼
│ mock backend     │   ┌─────────────────────────────────────────────┐
│ llm-calls.jsonl  │   │   claude -p --json-schema (+ --model ROLE)  │
└──────────────────┘   │   codex exec  --output-schema               │
                       └─────────────────────┬───────────────────────┘
                                             │ MCP
                                             ▼
                       ┌─────────────────────────────────────────────┐
                       │   Graphiti MCP (group_id=ctf_writeups)      │
                       │   read: solveurs, import, writeup           │
                       │   write: memory agent only                  │
                       └─────────────────────────────────────────────┘
```

Python minimal. Prompts font le gros du boulot. Skills lus depuis `skills/*/SKILL.md` au runtime, injectés dans le prompt des agents.

## Schéma 2 — boucle challenge (LangGraph)

```
run_challenge(request)
    │
    ▼
┌──────────────────────┐
│ normalize + workspace│  .challenges/<slug>-<hash>/
│ load resume          │  .runs/attempt-history.json + working-memory.json
└──────────┬───────────┘
           ▼
╔══════════════════════════════════════════════════════════════╗
║                    LangGraph state machine                   ║
║                                                               ║
║   START ──► route ──► run_specialist ──► analyze_attempt     ║
║                            ▲                    │            ║
║                            │                    ▼            ║
║                            │              decide_next        ║
║                            │                    │            ║
║                            └── retry/switch ────┤            ║
║                                                 ▼            ║
║                            END (stop / writeup / memory)     ║
╚══════════════════════════════════════════════════════════════╝
           │
           ▼
┌──────────────────────┐
│ writeup_agent        │  → writeup.md
│ memory_agent         │  → Graphiti MCP
└──────────────────────┘
```

Nœuds:

- `route` — catégorie + core_skill + specialist_skill (Python + router skill).
- `run_specialist` — construit WorkerRequest, sélection backend policy-based (solve_rate/block_rate/avg_confidence), worker subprocess, raw_output > 8k → summarizer cheap, append history, build working memory v2.
- `analyze_attempt` — event read-only, flag check, stagnation read.
- `decide_next` — moteur de décision (cf. schéma 3). Met à jour `recommended_next_brief` → injecté dans prochain worker.

## Schéma 3 — moteur de décision (`_choose_decision`)

```
latest = WorkerResult

┌─ flag présent ?
│    ├─ writeup pas fait          → REQUEST_WRITEUP
│    ├─ memory pas fait            → REQUEST_MEMORY
│    └─ tout fait                  → STOP (post_solve_complete)
│
├─ worker recommended_action ∈ enum valide
│    ├─ needs_human                → NEEDS_HUMAN
│    ├─ stop                       → STOP
│    ├─ reassess_category          → REASSESS_CATEGORY
│    ├─ retry/switch               → honoré (override si stagnation)
│    └─ budget dépassé             → STOP
│
├─ worker.needs_human              → NEEDS_HUMAN
├─ failure_reason = wrong_category → REASSESS_CATEGORY
├─ failure_reason = needs_human    → NEEDS_HUMAN
│
├─ stagnation reframed-triggers
│     { hypothesis_loop,
│       no_confirmed_hypothesis,
│       slow_hypothesis_drift,
│       confidence_downtrend }
│                                  → RETRY_REFRAMED
│     (si budget dépassé)          → STOP
│
├─ stagnation switch-triggers
│     { three_consecutive_blocked,
│       identical_summaries_recent,
│       command_repetition }
│                                  → SWITCH_BACKEND
│     (si budget dépassé)          → STOP
│
├─ attempts >= max_attempts        → STOP (max_attempts_reached)
│
├─ status = blocked                → SWITCH_BACKEND
├─ status = needs_retry            → RETRY_REFRAMED
│
├─ same hypothesis + conf < 0.5    → RETRY_REFRAMED (identical_repeat_guard)
│
└─ default                         → RETRY_REFRAMED
                                     (jamais RETRY_SAME sans signal)
```

Décision → `_build_attempt_brief(...)` → stocké dans `working_memory.recommended_next_brief` → injecté dans prompt worker suivant. `_select_next_backend_index(...)` score les backends par (-solve_rate, block_rate, -avg_conf).

## Prérequis

- Python `3.11+`
- `claude` ou `codex` installé et authentifié (au moins un).
- MCP Graphiti dans l'environnement (optionnel: absent → memory agent retourne `skipped`).
- Backend `mock` = boucle solveur locale pour tests.

Installation:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

## Exemple campagne complète

```bash
ctf-supervisor \
  "https://ctf.example.com/challenges" \
  --session-cookie "abc123" \
  --category web --category crypto \
  --max-difficulty medium \
  --max-parallel-challenges 3 \
  --max-instance-challenges 1 \
  --max-attempts 6 \
  --backend-sequence claude,codex \
  --start-instance-when-needed
```

Pipeline:

1. `load_source_document` fetch page (cookie).
2. `ctf-import-agent` lit contenu brut, retourne N challenges structurés.
3. Queue Python filtre + priorise.
4. `run_challenge` parallèle (avec `state_lock`), max_instance_challenges = 1.
5. Boucle LangGraph: route → specialist → analyze → decide → loop.
6. Post-challenge: `ctf-supervisor-agent` pick next action.
7. Solved → writeup agent + memory agent → Graphiti.

Le superviseur ne submit jamais de flag automatiquement.

## Un seul challenge

```bash
ctf-orchestrator --challenge-file challenge.json --backend-sequence claude,codex
```

Trajectoires parallèles best-of-N:

```bash
ctf-orchestrator \
  --challenge-file challenge.json \
  --backend-sequence claude,codex \
  --parallel-trajectories 3 \
  --max-attempts 4
```

Mock local complet (pas de LLM):

```bash
CTF_AGENTS_MOCK=1 \
ctf-orchestrator --challenge-file challenge.json --backend-sequence mock
```

## Import via agent

```bash
ctf-import \
  "https://ctf.example.com/challenges/noise-cheap" \
  --session-cookie "abc123" \
  --start-instance \
  --output challenge.json
```

L'import agent reçoit tout le contenu brut, normalise en JSON structuré. Si `--start-instance` est passé, l'agent est aussi responsable de déclencher le démarrage d'instance et d'extraire l'host:port résultant. Zéro logique hardcodée côté Python.

Lister:

```bash
ctf-import --input-file board.txt --list
ctf-import --input-file board.txt --challenge "Noise Cheap" --stdout
```

## Workspace

```
.challenges/<slug>-<hash>/
├── challenge.json               manifeste normalisé
├── writeup.md                   généré par writeup agent
├── artifacts/                   fichiers copiés / téléchargés
└── .runs/
    ├── attempt-history.json     history tronqué (6 derniers)
    ├── working-memory.json      memory v2
    ├── llm-calls.jsonl          log structuré par appel LLM
    ├── cache/<role>/<sha>.json  cache writeup/memory/import
    ├── claude|codex|mock/       logs par backend (prompt, schema, output)
    ├── writeup/                 prompt + output writeup agent
    ├── memory/result.json       statut persistance Graphiti
    └── summarizer/              (si déclenché)
```

Resume automatique: relit history + memory v2 avant nouveau run. v1 ancienne ignorée.

## Working memory v2

Champs persistés:

`current_focus, confirmed_findings, low_value_paths, key_commands, inline_scripts, handoff_files, active_hypothesis, tested_hypotheses, rejected_hypotheses, promising_leads, stagnation_signals, backend_performance, reachable_targets, useful_artifacts, last_strategy_change_reason, recommended_next_brief, exploration_branches, resume_assessment`

Signaux stagnation:

- `three_consecutive_blocked`
- `identical_summaries_recent`
- `hypothesis_loop` (≤2 hypothèses uniques sur 4)
- `no_confirmed_hypothesis` (4 derniers rejected/inconclusive)
- `slow_hypothesis_drift` (Jaccard ≥ 0.6 entre hypothèses successives)
- `confidence_downtrend` (monotone décroissante + drop ≥ 20%)
- `command_repetition` (même shell cmd ≥ 3×)
- `backend_all_blocked:<backend>`

Toutes les listes bornées: `tested_hypotheses[-12:]`, `exploration_branches[-16:]`, `useful_artifacts[-20:]`, `backend_performance` top-8 par solve rate.

## WorkerResult structuré

`status, summary, next_step, flag, evidence, commands, hypothesis, hypothesis_result, confidence, novelty, failure_reason, recommended_action, artifacts_produced, network_touched, target_reachable, needs_human, branch_id`

## Agent runtime

Tous les appels agents (non-solver) passent par `invoke_agent()`:

1. Cache check (`.runs/cache/<role>/<sha>.json`)
2. Mock path (`CTF_AGENTS_MOCK=1`)
3. Budget hard stop (`CTF_LLM_BUDGET_MAX_CALLS`)
4. Backend select (claude préféré)
5. Semaphore concurrence (`CTF_LLM_MAX_CONCURRENCY`, défaut 3)
6. Model routing par rôle
7. subprocess + JSON schema
8. Extraction robuste + validation
9. Cache write + log `.runs/llm-calls.jsonl`

## Model routing

| Rôle | ENV var | Conseil |
|---|---|---|
| solver | `CLAUDE_MODEL_SOLVER` | sonnet/opus |
| supervisor | `CLAUDE_MODEL_SUPERVISOR` | haiku |
| import | `CLAUDE_MODEL_IMPORT` | haiku |
| writeup | `CLAUDE_MODEL_WRITEUP` | sonnet |
| memory | `CLAUDE_MODEL_MEMORY` | sonnet |
| summarizer | `CLAUDE_MODEL_SUMMARIZER` | haiku |

Fallback: `CLAUDE_MODEL` → modèle worker défaut. Même pattern `CODEX_MODEL_*`.

## Summarizer

Sortie worker > `WORKER_SUMMARIZER_THRESHOLD` (défaut 8000 chars) → compressée par summarizer cheap. Préserve flags, commandes, erreurs. Désactiver: `WORKER_SUMMARIZER_DISABLED=1`.

## Config

**Workers:**
- `WORKER_TIMEOUT_SECONDS` (défaut `1800`)
- `WORKER_PERMISSION_MODE` (`default`, `on-request`, `plan`, `danger-full-access`, ...). Valeur inconnue → `ValueError` explicite.
- `WORKER_STREAM_EVENTS`
- `WORKER_SUMMARIZER_THRESHOLD` / `WORKER_SUMMARIZER_DISABLED`
- `CODEX_MODEL`, `CODEX_EXTRA_ARGS`, `CODEX_TIMEOUT_SECONDS`
- `CLAUDE_MODEL`, `CLAUDE_EXTRA_ARGS`, `CLAUDE_TIMEOUT_SECONDS`

**Agent runtime:**
- `CTF_IMPORT_AGENT_BACKENDS=claude,codex`
- `CTF_LLM_BUDGET_MAX_CALLS`, `CTF_LLM_MAX_CONCURRENCY`
- `CTF_AGENTS_MOCK=1` (dev offline)
- `AGENT_TIMEOUT_<ROLE>` (p.ex. `AGENT_TIMEOUT_WRITEUP=600`), fallback `AGENT_TIMEOUT_DEFAULT`
- `CTF_ROUTE_LLM_FALLBACK=1` — active le classifier LLM cheap quand aucun keyword ne matche

**Workspace:**
- `CTF_RUNS_DIR` (défaut `.runs`) — répertoire de stockage par challenge

**Working memory bounds** (tous optionnels, defaults raisonnables):
- `MEMORY_TESTED_HYPOTHESES_LIMIT` (12)
- `MEMORY_PROMISING_LEADS_LIMIT` (6)
- `MEMORY_REJECTED_HYPOTHESES_LIMIT` (10)
- `MEMORY_USEFUL_ARTIFACTS_LIMIT` (20)
- `MEMORY_EXPLORATION_BRANCHES_LIMIT` (16)
- `MEMORY_BACKEND_PERFORMANCE_LIMIT` (8)
- `MEMORY_KEY_COMMANDS_LIMIT` (8)
- `MEMORY_HANDOFF_FILES_LIMIT` (10)
- `RESUME_HISTORY_DEPTH` (6)
- `RESUME_KEY_COMMANDS_LIMIT` (4)
- `RESUME_EVIDENCE_LIMIT` (3)

## Graphiti via MCP

- Lecture libre par tous les agents (`search_nodes`, `search_memory_facts` avec `group_ids=["ctf_writeups"]`).
- Écriture uniquement par memory agent (`add_memory` dans `ctf_writeups`), post-solve.
- Zéro config côté projet. Tout passe par MCP environment.

## Discord (optionnel)

`DISCORD_BOT_TOKEN` + `DISCORD_PARENT_CHANNEL_ID` → thread par challenge. Events publiés: `route_resolved`, `attempt_completed`, `attempt_analyzed`, `decision_made`, `raw_output_summarized`, `memory_persist_completed`, `supervisor_agent_decision`, `trajectory_solved`, `worker_command_*`.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests
```

153 tests. Couvre dispatch runtime (budget, cache, mock, schema, JSON parse), model routing, stagnation hardening, retry loop guard, trajectoires parallèles, decide_next, working memory evolution, import agent flow, supervisor agent flow, flag regex permissive (HTB/THM/pico), backend registry plugin, skills auto-discovery, HTML extractor code/pre/table preservation, per-role timeouts, enum extensibility, configurable runs path.

## Limites

- Pas d'UI.
- Pas d'auto-submit de flags.
- Pas de persistance distante hors Graphiti.
- Sans `claude`/`codex`, `ctf-import` et `ctf-supervisor` HS — sauf `CTF_AGENTS_MOCK=1`.
- Backend `mock` = boucle solveur locale sur challenge déjà normalisé. Combo `CTF_AGENTS_MOCK=1` + `--backend-sequence mock` pour pipeline complet offline.
