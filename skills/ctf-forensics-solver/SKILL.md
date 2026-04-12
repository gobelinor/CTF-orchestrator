---
name: ctf-forensics-solver
description: Solveur CTF Forensics. Charge la methodology core et applique les quick wins, pivots et outils spécifiques à l'analyse d'artefacts (disque, mémoire, pcap, logs, documents).
---

# CTF Forensics Solver

Applique la Resolution Loop définie dans `ctf-core-methodology`. Ce skill ajoute uniquement les signaux et outils spécifiques forensics.

## Quick Wins
- Archives ou couches imbriquées, fichiers supprimés, metadata parlantes
- Historique shell / browser / clipboard, macros, artefacts d'upload
- Pcaps avec HTTP/DNS/FTP/SMTP/TLS non chiffré ou objets exportables
- RAM: processus suspects, strings, creds, sockets, cmdline
- Logs: tokens, traces d'accès, chemins, événements anormaux

## High-Value Pivots
- **Disque**: monter et parcourir l'arborescence avant de lancer les carvers.
- **RAM**: lister processus/réseau/files avant les plugins lourds Volatility.
- **Pcap**: filtrer par protocole/hôte/objet/session avant de regarder 10 000 paquets.
- **Logs**: isoler la fenêtre temporelle la plus dense, `jq`/`awk`/`rg`.
- Au step Research: Graphiti → `ctf_writeups` + web sur le format exact (EVTX, $MFT, memory profile version, format propriétaire).

## Tools spécifiques
`binwalk`, `foremost`, `exiftool`, `tshark`, `tcpflow`, `capinfos`, `volatility3`, `plaso`/`log2timeline` si timeline nécessaire.
