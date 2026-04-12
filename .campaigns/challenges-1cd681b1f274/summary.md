# Campaign Summary

**Campaign:** Campaign challenges
**Source:** `https://ctf.espilon.net/challenges`

## Counts

- `needs_human`: 2
- `skipped`: 13
- `solved`: 2

## Challenges

- `needs_human` Nurse Call: First pass using ctf-web-solver: likely auth bypass or input tampering path.
- `needs_human` The Wired: First pass using ctf-forensics-solver: likely auth bypass or input tampering path.
- `skipped` Accela Signal: filtered out
- `skipped` CAN Bus Implant: filtered out
- `skipped` Cyberia Grid: filtered out
- `skipped` Glitch The Wired: filtered out
- `skipped` LAIN_Br34kC0r3 V2: filtered out
- `skipped` Let's All Hate UART: filtered out
- `skipped` Observe The Wired: filtered out
- `skipped` Operating Room: filtered out
- `skipped` Patient Portal: filtered out
- `skipped` Schumann Resonance: filtered out
- `skipped` Serial Experimental 00: filtered out
- `skipped` Signal Tap Lain: filtered out
- `skipped` Wired Airwave 013: filtered out
- `solved` Hello-ESP: Recovered the flag statically from the ESP32 app image and left a minimal reproducer at /Users/tj/Documents/CTF-Destroyer/.challenges/hello-esp-f56c26c4/solve.py. The firmware contains an embedded XOR key (`LAIN`) and ciphertext; decoding yields the flag.
- `solved` Wired SPI Exfil: Reused the existing minimal solver in the workspace, connected to the live SPI probe on espilon.net:37984, read the hidden flash region at 0x030000, XOR-decoded the recovered blob with key WIRED_SPI, and confirmed the flag.
