---
name: ctf-category-router
description: Routage initial de tout challenge CTF vers le couple `ctf-core-methodology` + skill spécialisé adapté (crypto/reverse/web/pwn/forensics/osint/stego/misc/mobile/blockchain/cloud/hardware/jail). Utiliser au démarrage de chaque challenge CTF.
---

# CTF Category Router

## Routing
1. Lire l’énoncé, artefacts, service cible.
2. Assigner une catégorie principale, puis noter une catégorie secondaire éventuelle sans compliquer le routage.
3. Charger toujours `ctf-core-methodology` puis le skill spécialisé de la catégorie principale.
4. En cas d’ambiguïté forte, choisir la catégorie qui maximise la résolution terminal-first la moins coûteuse.

## Mapping rapide
- Crypto: chiffrements, signatures, maths
- Reverse: binaire, bytecode, crackme
- Web: application HTTP/API, auth/session
- Pwn: memory corruption, exploit binaire
- Forensics: preuves disque/ram/pcap/logs
- OSINT: investigation sources ouvertes
- Stego: données cachées médias
- Mobile: APK/IPA, app Android/iOS, instrumentation légère
- Blockchain: smart contracts, EVM, RPC, storage on-chain
- Cloud: IAM, containers, buckets, kube, metadata, CI/CD
- Hardware: firmware, bus, UART/JTAG, RF, SDR, embedded
- Jail: pyjail, shell jail, seccomp, sandbox escape
- Misc: tout le reste ou vrai hybride non dominant

## Graphiti (MCP)
Graphiti est dispo via MCP dans l'environnement (group_id `ctf_writeups`). En cas de doute sur la catégorie, tu peux interroger Graphiti pour voir si un challenge avec ce titre a déjà été résolu et dans quelle catégorie il a été classé. Tu ne persistes rien.
