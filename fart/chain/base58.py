"""Base58 (Bitcoin alphabet) encode/decode, stdlib only. Solana pubkeys are 32 bytes."""
from __future__ import annotations

ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(ALPHABET)}


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = bytearray()
    while n > 0:
        n, r = divmod(n, 58)
        out.append(ALPHABET[r])
    pad = 0
    for b in data:
        if b == 0:
            pad += 1
        else:
            break
    return (ALPHABET[0:1] * pad + bytes(reversed(out))).decode("ascii")


def b58decode(s: str) -> bytes:
    if not s:
        return b""
    n = 0
    for ch in s.encode("ascii"):
        try:
            n = n * 58 + _INDEX[ch]
        except KeyError:
            raise ValueError(f"invalid base58 character {chr(ch)!r}") from None
    pad = 0
    for ch in s:
        if ch == "1":
            pad += 1
        else:
            break
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * pad + body


def is_pubkey(s: str) -> bool:
    try:
        return len(b58decode(s)) == 32
    except (ValueError, UnicodeEncodeError):
        return False
