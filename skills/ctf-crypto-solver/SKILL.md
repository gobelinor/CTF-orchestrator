---
name: ctf-crypto-solver
description: Solveur CTF Crypto. Charge la methodology core et applique les quick wins, pivots et outils spécifiques aux challenges cryptographiques. Couvre classical ciphers, RSA, AES modes, hash/MAC, RNG, lattice, protocoles.
---

# CTF Crypto Solver

Applique la Resolution Loop définie dans `ctf-core-methodology`. Ce skill ajoute uniquement les signaux et outils spécifiques crypto.

## Quick Wins
- Encodages empilés (hex/base64/int/bytes) + endianness
- XOR simple, répété, Vigenère, substitution, OTP réutilisé
- RSA: petit `e`, Fermat, Wiener, common modulus, CRT fault, oracles
- AES modes: ECB, IV/nonce reuse, CTR/GCM misuse, padding oracle
- Hash/MAC: length extension, confusion hash/MAC, comparaison tronquée
- RNG: seed faible, LCG, MT19937, nonce prévisible

## High-Value Pivots
- Écrire des checks qui invalident une famille d'attaques avant de lancer un solveur lourd.
- Exploiter tailles, répétitions, collisions, sorties partielles, erreurs de parsing.
- Extraire une relation algébrique sur les inconnues avant Z3 / lattice.
- Au step Research: Graphiti → `ctf_writeups`, puis cryptohack / ctftime / arXiv pour la primitive exacte. Beaucoup d'attaques non-triviales ont un writeup de référence à un google près.
- SageMath uniquement quand la réduction mathématique le justifie.

## Tools spécifiques
`pycryptodome`, `hashlib`, `gmpy2`, `sympy`, `sage`, `pwntools`, `requests` pour oracles distants.
