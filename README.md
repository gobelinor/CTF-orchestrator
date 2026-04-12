# CTF Orchestrator

Multi-agent CTF solver. Prompt-driven. LangGraph + claude/codex subprocesses.

> Burns tokens. Spends budget. Inflates cognitive debt. You've been warned.

## What it does

- Import challenges from URL/file/stdin via prompt-driven agent.
- Route to specialist skill. Spawn `claude`/`codex`/`mock` worker.
- Working memory tracks hypotheses, branches, stagnation.
- Decision engine: retry, reframe, switch backend, stop, writeup, persist memory.
- Parallel trajectories best-of-N (first-flag-wins).
- Campaign mode: queue + bounded concurrency + supervisor agent between challenges.
- Post-solve: writeup agent + memory agent (Graphiti MCP).

## Architecture

```
                        ┌──────────────────────────────────────┐
                        │           skills/ (prompts)           │
                        │                                       │
                        │  ctf-import-agent    ctf-*-solver     │
                        │  ctf-supervisor-agent                 │
                        │  ctf-core-methodology                 │
                        │  ctf-writeup-agent   ctf-memory-agent │
                        └──────────────────▲───────────────────┘
                                           │ SKILL.md injected in prompt
                                           │
┌──────────────────┐   ┌───────────────────┴───────────────────┐
│  agent_runtime   │◄──┤  invoke_agent()                       │
│                  │   │                                       │
│ budget tracker   │   │  writeups / memory / import /         │
│ semaphore        │   │  supervisor / graph (summarizer)      │
│ cache + mock     │   └───────────────────┬───────────────────┘
│ model routing    │                       │ subprocess
│ schema validate  │                       ▼
└──────────────────┘   ┌───────────────────────────────────────┐
                       │  claude -p --json-schema               │
                       │  codex exec --output-schema            │
                       └───────────────────┬───────────────────┘
                                           │ MCP
                                           ▼
                       ┌───────────────────────────────────────┐
                       │  Graphiti MCP (ctf_writeups)           │
                       │  read: all agents / write: memory only │
                       └───────────────────────────────────────┘
```

Python minimal. Prompts do heavy lifting. Skills loaded from `skills/*/SKILL.md` at runtime.

## Challenge loop

```
┌──────────────────────┐
│ normalize + workspace │
│ load resume context   │
└──────────┬───────────┘
           ▼
   START ─► route ─► run_specialist ─► analyze_attempt
                          ▲                    │
                          │                    ▼
                          │              decide_next
                          │                    │
                          └── retry/switch ────┤
                                               ▼
                          END (stop / writeup / memory)
```

`decide_next` picks one of 8 decisions based on flag presence, worker signals, stagnation detection, budget. Never retries same approach without positive signal.

## Install

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e .
```

Needs `claude` or `codex` installed and authed. Mock backend for offline dev.

## Usage

Unified CLI: `ctf {run|import|campaign}`. Old commands (`ctf-orchestrator`, `ctf-import`, `ctf-supervisor`) still work.

### Campaign (board of challenges)

```bash
ctf campaign \
  "https://ctf.example.com/challenges" \
  --session-cookie "abc123" \
  --category web --category crypto \
  --max-parallel-challenges 3 \
  --max-attempts 6 \
  --backend-sequence claude,codex \
  --start-instance-when-needed
```

Import agent reads page. Queue filters + prioritizes. Challenges solved in parallel. Supervisor agent picks next action between challenges. Solved → writeup + memory persist.

### Single challenge

```bash
ctf run --challenge-file challenge.json --backend-sequence claude,codex
```

### Import

```bash
ctf import "https://ctf.example.com/challenges/target" \
  --session-cookie "abc123" --start-instance --output challenge.json
```

### Offline dev

```bash
CTF_AGENTS_MOCK=1 ctf run --challenge-file challenge.json --backend-sequence mock
```

## Workspace

```
.challenges/<slug>-<hash>/
├── challenge.json
├── writeup.md
├── artifacts/
└── .runs/
    ├── attempt-history.json
    ├── working-memory.json
    ├── llm-calls.jsonl
    └── cache/<role>/<sha>.json
```

Auto-resume: rereads history + working memory on next run.

## Key env vars

| Var | What |
|---|---|
| `CLAUDE_MODEL` / `CODEX_MODEL` | Default model. Per-role: `CLAUDE_MODEL_SOLVER`, `_WRITEUP`, etc. |
| `CTF_LLM_BUDGET_MAX_CALLS` | Hard stop on total LLM calls |
| `CTF_LLM_MAX_CONCURRENCY` | Semaphore (default 3) |
| `CTF_AGENTS_MOCK=1` | Offline dev, no LLM calls |
| `WORKER_PERMISSION_MODE` | `default`, `on-request`, `plan`, `danger-full-access` |
| `WORKER_TIMEOUT_SECONDS` | Default 1800 |

## Tests

```bash
.venv/bin/python -m unittest discover -s tests
```

## Limits

- No UI. No auto-submit flags. No remote persistence beyond Graphiti.
- Without `claude`/`codex`, import and campaign agents need `CTF_AGENTS_MOCK=1`.
