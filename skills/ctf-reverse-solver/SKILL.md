---
name: ctf-reverse-solver
description: Solveur CTF Reverse. Charge la methodology core et applique les quick wins, pivots et outils spécifiques à la rétro-ingénierie de binaires, bytecode, VMs, obfuscation.
---

# CTF Reverse Solver

Applique la Resolution Loop définie dans `ctf-core-methodology`. Ce skill ajoute uniquement les signaux et outils spécifiques reverse.

## Quick Wins
- Flag en clair, segments data utiles, comparaisons naïves, transformations XOR/ROL/ADD/SUB
- Lookup tables, CRC/checksum, obfuscation légère, bytecode ou VM minimaliste
- Anti-debug triviaux qui se patchent en un byte

## High-Value Pivots
- Passe statique d'abord: `file`, `strings`, `objdump`, `readelf`, `rizin`. Décompilation seulement si la fonction critique ne ressort pas.
- Isoler la routine de check / dérivation / déchiffrement. Ignorer le reste.
- Reconstruire I/O d'une fonction avant de parcourir tout le binaire.
- Symbolic execution sur fonction réduite, jamais sur le programme entier.
- Au step Research: Graphiti → `ctf_writeups` + web pour packer / protector / VM custom déjà documenté (Themida, VMProtect, Pyarmor, bytecodes exotiques).

## Tools spécifiques
`rizin` / `radare2`, Ghidra headless, `angr`, `z3`, `pylingual`/`uncompyle6` pour Python bytecode, `dex2jar`/`jadx` pour APK.
