---
name: ctf-jail-solver
description: Solveur CTF Jail/Sandbox. Charge la methodology core et applique les quick wins et techniques spécifiques pyjail, shell jail, filtres AST, seccomp, template injection.
---

# CTF Jail Solver

Applique la Resolution Loop définie dans `ctf-core-methodology`. Ce skill ajoute uniquement les techniques spécifiques aux environnements restreints.

## Quick Wins
- Récupération d'objets via MRO, `__subclasses__`, `globals`, closures, exceptions, format strings
- Imports indirects, accès à `open`, `os`, `sys`, loaders ou modules déjà chargés
- Bypass blacklist par concaténation, encodage, aliasing, objets existants
- Shell jail: env vars, expansion, redirections, interprètes accessibles
- Seccomp: lire la politique effective et exploiter les syscalls permis

## High-Value Pivots
- **Harness local** qui teste les payloads et capture erreurs avant tout envoi distant.
- Classer la contrainte (syntactic parser vs runtime filter vs seccomp vs template engine) avant de générer des payloads.
- Primitive claire d'abord: obtenir un nom / handle / fd / import / lecture. Pas de chaîne de 15 gadgets.
- Au step Research: Graphiti + web pour le moteur exact (PyJail AST visitor custom, Jinja2 SSTI, Handlebars, bash `set -r`, restricted Ruby, etc.).

## Tools spécifiques
Python local pour harness + payload gen, `strace`, `seccomp-tools`, `pwntools` si interactif.
