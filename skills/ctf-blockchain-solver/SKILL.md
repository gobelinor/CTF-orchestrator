---
name: ctf-blockchain-solver
description: Solveur CTF Blockchain. Charge la methodology core et applique les quick wins et outils spécifiques contrats EVM, état on-chain, Foundry, cast, DeFi primitives.
---

# CTF Blockchain Solver

Applique la Resolution Loop définie dans `ctf-core-methodology`. Ce skill ajoute uniquement les signaux et outils spécifiques blockchain.

## Quick Wins
- `owner` ou rôle mal initialisé, `initialize` public, access control manquant
- Reentrancy, mauvaise comptabilité, précision décimale
- Storage sensible lisible, slot critique écrasable, collision de storage, delegatecall abusif
- Signature/permit mal lié au domain / nonce / chain / signer
- Randomness ou timestamp exploitable, oracle naïf, `tx.origin`

## High-Value Pivots
- Lire events + storage avec `cast` avant de fuzz les fonctions.
- Reproduire localement avec `anvil --fork-url` ou un test `forge` dès qu'un état précis importe.
- Contrat intermédiaire minimal et lisible.
- Au step Research: Graphiti + web sur la primitive DeFi exacte (Uniswap V2/V3, flash loans, ERC4626, curve). Beaucoup de challenges copient un exploit historique connu.

## Tools spécifiques
`cast`, `forge`, `anvil`, `solc`, `slither` (statique), scripts Python `web3`.
