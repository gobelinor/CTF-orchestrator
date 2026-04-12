---
name: ctf-web-solver
description: Solveur CTF Web. Charge la methodology core et applique les quick wins, pivots et outils spécifiques aux challenges web. Pour HTTP/API/cookies/JWT/auth/injections/SSRF/SSTI/upload/logique métier.
---

# CTF Web Solver

Applique la Resolution Loop définie dans `ctf-core-methodology`. Ce skill ajoute uniquement les signaux et outils spécifiques au web.

## Quick Wins
- IDOR, auth bypass, trust côté client, rôle implicite, debug routes, fichiers de backup
- path traversal, file read, upload, template injection, deserialization légère
- JWT/session mal validé, algorithme none, confusion HS/RS, cookies prévisibles
- SSRF, open redirect utile, CSRF logique, race triviale
- SQLi/NoSQLi/SSTI/XPath quand l'entrée et la sink sont proches

## High-Value Pivots
- Lire HTML, JS, réponses d'erreur, metadata avant de fuzz.
- Si le frontend révèle des endpoints, les appeler directement (pas l'UI).
- Pour les APIs, une collection de requêtes minimales reproductibles.
- `ffuf` uniquement si une convention de nommage le justifie.
- Avant d'inventer une technique exotique, Graphiti → `web_pentest` + web → articles / writeups publics sur la stack exacte (framework, version, CVE).

## Tools spécifiques
`curl`, `httpie`, `jq`, `requests`, `ffuf` (scope serré), proxy/Burp optionnel.
