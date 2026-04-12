---
name: ctf-osint-solver
description: Solveur CTF OSINT. Charge la methodology core et applique les quick wins, pivots et garde-fous spécifiques aux enquêtes en sources ouvertes (social, géolocalisation, archives, métadonnées).
---

# CTF OSINT Solver

Applique la Resolution Loop définie dans `ctf-core-methodology`. Ce skill ajoute uniquement les pivots et règles spécifiques OSINT.

## Quick Wins
- Recherche exacte sur chaînes rares / citations / handles
- Réutilisation handle, avatar, photo, bannière, bio sur d'autres plateformes
- EXIF, archives web (Wayback, archive.today), historique DNS/WHOIS, fichiers publics exposés
- Reverse image, géolocalisation par enseignes, routes, reliefs, ombres, fuseaux horaires

## High-Value Pivots
- Partir des indices fournis, jamais de l'internet entier.
- Exiger deux signaux indépendants compatibles avant de conclure.
- Croiser image + texte + date + lieu au lieu d'un seul axe.
- Au step Research: Graphiti → `ctf_writeups` + outils publics (Sherlock, Maigret, GeoGuessr-style sources).

## Règles spécifiques OSINT
- **Passif uniquement.** Pas d'interaction intrusive, pas de création de compte, pas de scraping agressif, pas de harcèlement.
- Pas de dox hors sujet. Tout pivot doit être justifié par l'énoncé.
- Valider le **format exact** de la réponse demandée avant de conclure.

## Tools spécifiques
`exiftool`, moteurs de recherche avancés, reverse image search, archives web. `rg` pour parser dumps textuels.
