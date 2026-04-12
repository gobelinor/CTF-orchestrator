---
name: ctf-hardware-rf-solver
description: Solveur CTF Hardware/RF. Charge la methodology core et applique les quick wins et outils spécifiques firmware, bus série, captures RF/IQ, modulations, protocoles radio.
---

# CTF Hardware RF Solver

Applique la Resolution Loop définie dans `ctf-core-methodology`. Ce skill ajoute uniquement les signaux et outils spécifiques hardware/RF.

## Quick Wins
- Firmware: archives embarquées, configs, clés, mots de passe, pages web, scripts update
- UART/console, protocoles texte, commandes en clair, checksums simples
- Captures RF ou audio: ASK/FSK, trames répétées, préambules, IDs fixes
- Secrets ou flags en clair dans dumps, EEPROM, calibration files

## High-Value Pivots
- **Firmware**: `binwalk`, `strings`, extraction FS, recherche credentials et endpoints avant de toucher à la partie exécution.
- **Bus série**: reconstruire les paquets et leur framing avant de spéculer sur la logique applicative.
- **RF**: format, débit, bursts, modulation probable, symboles récurrents avant tout décodage complet.
- Au step Research: Graphiti + web pour protocoles propriétaires connus (LoRa, Zigbee, Z-Wave, PMR446, CAN, Modbus) et outils déjà écrits.

## Tools spécifiques
`binwalk`, `sox`, `rtl_433`, `multimon-ng`, `sigrok-cli`, `pulseview`, scripts Python (`numpy`, `scipy.signal`).
