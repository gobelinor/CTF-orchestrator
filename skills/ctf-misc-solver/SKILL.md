---
name: ctf-misc-solver
description: Solveur CTF Misc. Charge la methodology core et applique les quick wins et pivots pour challenges hybrides, protocoles custom, puzzles, automates, pipelines d'encodage.
---

# CTF Misc Solver

Applique la Resolution Loop définie dans `ctf-core-methodology`. Ce skill couvre tout ce qui ne rentre pas dans une catégorie canonique.

## Quick Wins
- Encodages empilés, compression, sérialisation, framing JSON/binary
- Protocoles texte / menu interactif / états simples
- Erreurs d'index, limites, timestamps, checksums, seeds, génération déterministe
- Puzzles qui se réduisent à une recherche bornée ou un automate fini

## High-Value Pivots
- **Parser d'abord, optimiser ensuite.** Comprendre le contrat I/O exact.
- Transcripts courts pour comprendre un protocole inconnu.
- Conserver un mode local / mock pour itérer.
- Si une sous-discipline devient dominante (crypto, web, pwn), pivoter explicitement vers le solveur correspondant en signalant `recommended_action=reassess_category`.
- Au step Research: Graphiti + web ciblé sur le nom exact du challenge.

## Tools spécifiques
Python stdlib, `pwntools`, `requests`, `jq`, `awk`, `sed`, `xxd`, `nc`.
