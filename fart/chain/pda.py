"""Program-derived addresses (PDAs), stdlib only.

`find_program_address(seeds, program_id)` is Solana's algorithm: for bump 255 down to 0 take
sha256(seeds... || bump || program_id || "ProgramDerivedAddress") and return the first hash that is
NOT a valid Ed25519 point. The on-curve test is the decompression check of RFC 8032 / curve25519-dalek:
with y the 255-bit little-endian value, x^2 = (y^2 - 1) / (d*y^2 + 1) must be a quadratic residue
mod p = 2^255 - 19 (u = 0 counts as valid, v = 0 with u != 0 as invalid; dalek does not reject
x = 0 with the sign bit set).

Verified offline against pump.fun's own PDAs: Global = find(["global"]) and mint authority =
find(["mint-authority"]) reproduce the well-known addresses (tests/test_pda.py). The bonding curve
is find(["bonding-curve", mint]) under the pump.fun program. The pump-amm pool derivation is a
best guess (docs/UNVERIFIED.md) and is only exposed for `fart chain pda`, never used blindly.
"""
from __future__ import annotations

import hashlib
import struct

from .base58 import b58decode, b58encode

P = 2**255 - 19
# Edwards d = -121665/121666 mod p
D = (-121665 * pow(121666, -1, P)) % P
PDA_MARKER = b"ProgramDerivedAddress"
MAX_SEED_LEN = 32
MAX_SEEDS = 16


def is_on_curve(point: bytes) -> bool:
    """True when the 32 bytes decompress to a valid Ed25519 point (so they CANNOT be a PDA)."""
    if len(point) != 32:
        return False
    y = int.from_bytes(point, "little") & ((1 << 255) - 1)
    y %= P
    y2 = y * y % P
    u = (y2 - 1) % P
    v = (D * y2 + 1) % P
    if v == 0:
        return u == 0
    if u == 0:
        return True
    x2 = u * pow(v, P - 2, P) % P
    return pow(x2, (P - 1) // 2, P) == 1  # Euler's criterion: x2 is a square


def _pubkey_bytes(key: str | bytes) -> bytes:
    b = b58decode(key) if isinstance(key, str) else bytes(key)
    if len(b) != 32:
        raise ValueError("pubkey must be 32 bytes")
    return b


def create_program_address(seeds: list[bytes], program_id: str | bytes) -> bytes:
    """Raises ValueError when the derived hash lies on the curve (then try another bump)."""
    if len(seeds) > MAX_SEEDS:
        raise ValueError("too many seeds")
    h = hashlib.sha256()
    for s in seeds:
        if len(s) > MAX_SEED_LEN:
            raise ValueError("seed too long")
        h.update(s)
    h.update(_pubkey_bytes(program_id))
    h.update(PDA_MARKER)
    digest = h.digest()
    if is_on_curve(digest):
        raise ValueError("invalid seeds: address lies on the curve")
    return digest


def find_program_address(seeds: list[bytes], program_id: str | bytes) -> tuple[str, int]:
    """(base58 address, bump) for the first bump from 255 downwards that yields a PDA."""
    for bump in range(255, -1, -1):
        try:
            return b58encode(create_program_address(list(seeds) + [bytes([bump])], program_id)), bump
        except ValueError:
            continue
    raise ValueError("unable to find a viable program address bump seed")


def bonding_curve_address(mint: str, program_id: str) -> str:
    """pump.fun bonding curve PDA: ["bonding-curve", mint]."""
    return find_program_address([b"bonding-curve", _pubkey_bytes(mint)], program_id)[0]


def pump_amm_pool_address(mint: str, pump_program_id: str, amm_program_id: str, wsol_mint: str, index: int = 0) -> str:
    """UNVERIFIED best guess of the pump-amm pool created at graduation:
    creator = pump.fun PDA ["pool-authority", mint]; pool = pump-amm PDA ["pool", index u16 LE, creator, base mint, quote mint].
    Compare with migrations.pool_address on the VPS (`fart chain pda <mint>`)."""
    creator, _ = find_program_address([b"pool-authority", _pubkey_bytes(mint)], pump_program_id)
    seeds = [b"pool", struct.pack("<H", index), _pubkey_bytes(creator), _pubkey_bytes(mint), _pubkey_bytes(wsol_mint)]
    return find_program_address(seeds, amm_program_id)[0]
