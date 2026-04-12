---
name: ctf-core-methodology
description: Méthodologie CTF transverse utilisée par tous les solveurs spécialisés. Définit la resolution loop officielle, la resource policy, les règles d'engagement, le format de sortie structurée et l'usage de Graphiti. Chargée avant chaque skill spécialiste.
---

# CTF Core Methodology

## Resolution Loop

1. **Recon** — lire énoncé, artefacts, prior attempts, handoff files. Rien de nouveau avant d'avoir absorbé ce qui existe déjà.
2. **Surface mapping** — nommer la cible exacte: artefact local, binaire, service distant, protocole, identité, format, modèle.
3. **Hypothèse** — une ligne: quelle primitive / vulnérabilité / misconfig tu vas exploiter, et pourquoi c'est plausible.
4. **Quick wins** — tests à coût faible qui invalident ou confirment l'hypothèse en quelques secondes.
5. **Research** — si les quick wins échouent ou si le pattern est inhabituel: chercher le web (articles de recherche, arXiv, writeups publics, GitHub PoC) pour valider ou enrichir l'hypothèse avant d'aller plus loin. Graphiti MCP peut confirmer une technique connue ou signaler un writeup existant — l'interroger avant de faire du web.
6. **Exploit progressif** — scripter, itérer, réduire. Garder la chaîne minimale reproductible.
7. **Validation** — flag validé contre le format attendu, artefacts sauvegardés, output structurée remplie honnêtement.

Si une étape ne progresse pas, pivoter explicitement. Ne pas intensifier une piste qui stagne.

## Resource Policy

- Local et passif avant distant et interactif. Une seule opération coûteuse à la fois.
- Toute recherche bornée avant exécution (temps, taille, profondeur, workers, wordlist).
- Préférer les checks qui invalident vite une hypothèse.
- Réutiliser scripts, logs et sorties existants. Ne pas relancer une exploration similaire.
- Pas de brute force non borné, pas de fuzzing large sans hypothèse, pas de scans infra hors périmètre.

## Tool Bias (défaut)

`file`, `strings`, `xxd`, `jq`, `rg`, `curl`, `httpie`, `requests`, `python`, `pwntools`, `objdump`, `readelf`, `binwalk`, `tshark`, `aws`, `kubectl`, `cast`. Les solveurs spécialisés ajoutent leurs propres outils quand la catégorie l'exige.

## Graphiti (MCP)

Knowledge graph Graphiti disponible via MCP:
- **Read only pour toi.** `search_nodes`, `search_memory_facts` avec `group_ids=["ctf_writeups", "ecole2600_securite_ia", "web_pentest"]` et autres groupes quand pertinent.
- Consulter au moment `Research` de la boucle, ou plus tôt si tu reconnais un nom / pattern connu.
- Ne JAMAIS écrire. La persistance post-solve est gérée par `ctf-memory-agent`.

## Guardrails

- Rester dans le périmètre du challenge. Pas de pentest de la plateforme CTF ni de services tiers hors énoncé.
- Pas de DoS, flood, scan large. Bornes explicites sur tout brute force.
- Ne pas installer de stack lourde ni lancer plusieurs outils gourmands en parallèle sans besoin mesuré.

## Structured Result Fields (obligatoires)

Au-delà de `status`, `summary`, `next_step`, `flag`, `evidence`, `commands`:

- `hypothesis`, `hypothesis_result` (`confirmed`/`rejected`/`inconclusive`/`untested`), `confidence` (0..1), `novelty` (0..1)
- `failure_reason` canonique (`target_unreachable`, `auth_required`, `wrong_category`, `wrong_hypothesis`, `tool_missing`, `time_budget`, `needs_human`, `worker_error`, `unknown`, `none`)
- `recommended_action` (`retry_same_backend`, `retry_same_backend_reframed`, `switch_backend`, `stop`, `request_writeup`, `request_memory_persist`, `reassess_category`, `needs_human`)
- `artifacts_produced`, `network_touched`, `target_reachable`, `needs_human`, `branch_id`

Ces champs pilotent `decide_next_node`. Mentir les casse. Remplir avec honnêteté.

## Minimum Output

- Hypothèse retenue ou invalidée.
- Signal utile observé.
- Commande ou script central.
- Prochain pas le plus rentable si non résolu.
- Flag uniquement si effectivement récupéré et validé contre le format attendu.
