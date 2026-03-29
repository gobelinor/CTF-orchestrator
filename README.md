# CTF Orchestrator

Orchestrateur agentique pour challenges CTF.

Attention cet outil à de fortes chances de détruire vos tokens et l'environnement qui l'héberge, ainsi que d'infliger à l'utilisateur une dette cognitive conséquente et / ou un syndrome de l'imposteur remarquable. 

Le projet prend un challenge normalise, prepare un workspace dedie, route vers un skill CTF adapte, lance un worker (`mock`, `codex`, `claude`), conserve la memoire locale de reprise et produit un `writeup.md` quand un challenge est resolu.

## Ce que fait le projet

- normalise un challenge a partir d'un JSON, d'un texte ou d'une page distante
- cree un workspace isole sous `.challenges/<slug>-<hash>/`
- route vers des skills CTF specialises
- supporte `crypto`, `reverse`, `web`, `pwn`, `forensics`, `osint`, `stego`, `misc`, `mobile`, `blockchain`, `cloud`, `hardware`, `jail`
- rejoue un challenge avec memoire locale au lieu de repartir de zero
- peut superviser une board complete avec filtres et parallelisme borne
- peut publier le suivi dans Discord

## Prerequis

- Python `3.11+`
- `codex` installe et authentifie si tu veux utiliser le backend `codex`
- `claude` installe et authentifie si tu veux utiliser le backend `claude`

Installation:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

## Demarrage Rapide

Creer un challenge minimal:

```bash
cat > challenge.json <<'EOF'
{
  "title": "Demo Challenge",
  "category": "web",
  "description": "A small demo challenge.",
  "target_host": "demo.ctf.local:8080"
}
EOF
```

Run local sans LLM:

```bash
ctf-orchestrator \
  --challenge-file challenge.json \
  --backend-sequence mock \
  --max-attempts 2
```

Run avec un backend reel:

```bash
WORKER_TIMEOUT_SECONDS=300 \
WORKER_PERMISSION_MODE=default \
ctf-orchestrator \
  --challenge-file challenge.json \
  --backend-sequence codex
```

Run avec alternance de workers:

```bash
ctf-orchestrator \
  --challenge-file challenge.json \
  --backend-sequence claude,codex \
  --max-attempts 6
```

## Commandes Utiles

Importer un challenge depuis un texte colle:

```bash
pbpaste | ctf-import - --stdout > challenge.json
```

Importer une page protegee par cookie de session:

```bash
ctf-import \
  "https://ctf.example.com/challenges/noise-cheap" \
  --session-cookie "abc123" \
  --output challenge.json \
  --review
```

Importer un challenge CTFd et demarrer l'instance avant export:

```bash
ctf-import \
  "https://ctf.example.com/challenges" \
  --session-cookie "abc123" \
  --challenge "Glitch The Wired" \
  --start-instance \
  --output challenge.json
```

Lister plusieurs challenges detectes sur une meme source:

```bash
ctf-import --input-file board.txt --list
ctf-import --input-file board.txt --challenge "Noise Cheap" --stdout
```

Lancer une campagne sur une board:

```bash
ctf-supervisor \
  --source-url "https://ctf.example.com/challenges" \
  --session-cookie "abc123" \
  --category crypto \
  --category web \
  --max-difficulty medium \
  --max-parallel-challenges 3 \
  --max-instance-challenges 1 \
  --backend-sequence claude,codex
```

Le superviseur n'envoie pas automatiquement les flags au CTF.

## Format D'entree

Le projet accepte notamment les champs suivants:

- `title`, `name`, `challenge_name`
- `description`, `scenario`, `challenge_scenario`, `challenge_text`
- `category`, `category_hint`
- `files`, `artifacts`, `artifact_paths`
- `target_host` ou `ip` + `port`

Tous les autres champs sont conserves dans `challenge_metadata`.

Exemple minimal:

```json
{
  "title": "Forbidden Fruit",
  "category": "crypto",
  "description": "AES-GCM misuse challenge.",
  "target_host": "aes.cryptohack.org:443",
  "files": [
    "https://aes.cryptohack.org/forbidden_fruit/"
  ],
  "operator_hint": "Exploit AES-GCM nonce reuse. Avoid brute force."
}
```

## Workspace

Chaque challenge vit dans un dossier dedie:

```text
.challenges/<slug>-<hash>/
```

Fichiers utiles:

- `challenge.json`: manifeste normalise
- `artifacts/`: fichiers copies ou telecharges
- `.runs/attempt-history.json`: historique des tentatives
- `.runs/working-memory.json`: memoire de reprise
- `writeup.md`: writeup genere apres resolution
- `.discord-thread.json`: liaison Discord locale si activee

La reprise relit automatiquement l'historique et la memoire locale avant un nouveau run.

## Configuration

Variables utiles:

- `WORKER_TIMEOUT_SECONDS`: timeout global des workers. Defaut `1800`
- `WORKER_PERMISSION_MODE`: mode provider-agnostic (`default`, `on-request`, `plan`, `danger-full-access`, etc.)
- `WORKER_STREAM_EVENTS`: active ou desactive le streaming des commandes workers
- `CODEX_MODEL`, `CODEX_EXTRA_ARGS`
- `CLAUDE_MODEL`, `CLAUDE_EXTRA_ARGS`

Exemple:

```bash
WORKER_TIMEOUT_SECONDS=300 \
WORKER_PERMISSION_MODE=plan \
ctf-orchestrator \
  --challenge-file challenge.json \
  --backend-sequence codex
```

## Discord

Si `DISCORD_BOT_TOKEN` et `DISCORD_PARENT_CHANNEL_ID` sont definis, le projet peut publier le suivi dans Discord.

Exemple:

```bash
export DISCORD_BOT_TOKEN=...
export DISCORD_PARENT_CHANNEL_ID=123456789012345678

ctf-orchestrator \
  --challenge-file challenge.json \
  --backend-sequence claude,codex
```

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## Limites Actuelles

- pas d'UI dediee
- pas de persistance distante
- pas d'auto-submit de flags
- l'import reste surtout optimise pour texte brut, HTML simple et CTFd
