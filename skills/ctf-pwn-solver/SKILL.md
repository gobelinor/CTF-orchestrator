---
name: ctf-pwn-solver
description: Solveur CTF Pwn. Charge la methodology core et applique les quick wins, pivots et outils spécifiques binary exploitation. Couvre stack/heap overflow, format string, UAF, ROP, shellcode, primitives read/write.
---

# CTF Pwn Solver

Applique la Resolution Loop définie dans `ctf-core-methodology`. Ce skill ajoute uniquement les signaux et outils spécifiques pwn.

## Quick Wins
- ret2win, shellcode obvious, format string triviale, GOT overwrite sous protections faibles
- index signé/non signé, taille contrôlée, off-by-one, menu mal borné
- `scanf`/`printf`/`gets`/`read`/`strcpy` mal utilisés, chunks heap simples

## High-Value Pivots
- Triage d'abord: `file`, `checksec`, `ldd`, `strings`, `objdump` — GDB seulement une fois la primitive identifiée.
- Harness local qui reproduit exactement les échanges.
- Fuites (stack/libc/heap/bss) avant la chaîne finale.
- Heap complexe → revenir aux invariants allocator, pas au spray.
- Au step Research: Graphiti → `ctf_writeups` + web pour la version libc exacte, les how2heap correspondants, les one-gadgets publiés.

## Tools spécifiques
`checksec`, `objdump`, `readelf`, `pwntools`, `gdb` / `pwndbg`, `ROPgadget`, `one_gadget`, `libc-database`.
