```
   ___  ____  ____     __  ____   ___  _  _  ____  ____  ____  ____   __  ____  __  ____
  / __)(_  _)(  __)   /  \(  _ \ / __)/ )( \(  __)/ ___)(_  _)(  _ \ / _\(_  _)/  \(  _ \
 ( (__   )(   ) _)   (  O ))   /( (__ ) __ ( ) _) \___ \  )(   )   //    \ )( (  O ))   /
  \___) (__) (__)     \__/(__\_) \___)\_)(_/(____)(____/ (__) (__\_)\_/\_/(__) \__/(__\_)
```

Multi-agent CTF solver. Prompt-driven. LangGraph + claude/codex subprocesses.

> Burns tokens. Spends budget. Inflates cognitive debt. You've been warned.

## What it does

- Import challenges from URL/file via prompt-driven agent.
- Route to specialist skill. Spawn `claude`/`codex` worker.
- Working memory tracks hypotheses, branches, stagnation.
- Decision engine: retry, reframe, switch backend, stop, writeup, persist memory.
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

## Challenge loop

```
   START ─► route ─► run_specialist ─► analyze_attempt
                          ▲                    │
                          │                    ▼
                          │              decide_next
                          │                    │
                          └── retry/switch ────┤
                                               ▼
                          END (stop / writeup / memory)
```

## Install

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e .
```

Needs `claude` or `codex` installed and authed.

## Flags

```
ctf SOURCE [options]

SOURCE                   URL, local file path, or '-' for stdin
--input-file PATH        Read board text from a local file
--session-cookie VALUE   Session cookie or full Cookie header
--cookie-file PATH       File containing the raw Cookie header
--category CAT           Allowed category (repeatable)
--challenge NAME         Challenge title filter (repeatable)
--max-difficulty LEVEL   easy | medium | hard
--max-challenges N       Limit to top N eligible challenges
--max-parallel N         Max parallel runs (default 2)
--backend-sequence LIST  Comma-separated workers, e.g. 'claude,codex' (default mock)
--max-attempts N         Max specialist attempts per challenge (default 4)
--parallel-trajectories N  N isolated agents per challenge, first-flag-wins (default 1)
--skills-root PATH       Skills directory (default skills/)
--workspace PATH         Workspace root (default cwd)
--env-file PATH          .env file (default .env if present)
```

## Usage

```bash
ctf "https://ctf.example.com/challenges" \
  --session-cookie "abc123" \
  --backend-sequence claude,codex \
  --max-attempts 6
```

Filter by category or challenge name:

```bash
ctf "https://ctf.example.com/challenges" \
  --session-cookie "abc123" \
  --category web --category crypto \
  --challenge "Noise Cheap" \
  --backend-sequence claude
```

3 agents race on each challenge (first flag wins):

```bash
ctf "https://ctf.example.com/challenges" \
  --session-cookie "abc123" \
  --backend-sequence claude \
  --parallel-trajectories 3
```

Offline dev (no LLM):

```bash
CTF_AGENTS_MOCK=1 ctf --input-file board.txt --backend-sequence mock
```

## Env vars

| Var | What | Default |
|---|---|---|
| `CLAUDE_MODEL` | Claude model | (worker default) |
| `CODEX_MODEL` | Codex model | (worker default) |
| `CTF_LLM_BUDGET_MAX_CALLS` | Hard stop on LLM calls | unlimited |
| `CTF_AGENTS_MOCK` | Offline dev, no LLM | off |
| `WORKER_PERMISSION_MODE` | `default`, `on-request`, `plan` | `default` |
| `WORKER_TIMEOUT_SECONDS` | Worker timeout | 1800 |
| `DISCORD_BOT_TOKEN` | Discord integration | (optional) |
| `DISCORD_PARENT_CHANNEL_ID` | Discord channel | (optional) |

## Tests

```bash
.venv/bin/python -m unittest discover -s tests
```

## Limits

- No UI. No auto-submit flags.
- Without `claude`/`codex`: use `CTF_AGENTS_MOCK=1`.
