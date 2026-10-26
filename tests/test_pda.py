"""PDA derivation with the stdlib Ed25519 on-curve check, verified against pump.fun's own PDAs."""
import hashlib

import pytest

from fart.chain.base58 import b58decode, b58encode
from fart.chain.pda import bonding_curve_address, create_program_address, find_program_address, is_on_curve, pump_amm_pool_address

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def test_known_pump_fun_pdas():
    assert find_program_address([b"global"], PUMP) == ("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf", 255)
    assert find_program_address([b"mint-authority"], PUMP) == ("TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM", 255)
    assert find_program_address([b"__event_authority"], PUMP) == ("Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1", 255)


def test_on_curve_semantics():
    assert is_on_curve(b58decode(PUMP))  # a real keypair public key lies on the curve
    assert not is_on_curve(b58decode("TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM"))  # a PDA never does
    assert is_on_curve(b"\x01" + b"\x00" * 31)  # y = 1: the identity point
    assert not is_on_curve(b"")
    assert not is_on_curve(b"\x00" * 31)
    # roughly half of all 32-byte strings decompress to a point
    n = sum(is_on_curve(hashlib.sha256(bytes([i])).digest()) for i in range(256))
    assert 90 < n < 170


def test_bonding_curve_pda_deterministic_and_off_curve():
    mint = b58encode(hashlib.sha256(b"mint-1").digest())
    a = bonding_curve_address(mint, PUMP)
    assert a == bonding_curve_address(mint, PUMP)
    assert len(b58decode(a)) == 32 and not is_on_curve(b58decode(a))
    assert a != bonding_curve_address(b58encode(hashlib.sha256(b"mint-2").digest()), PUMP)
    # some bump makes the hash land on the curve: create_program_address refuses it
    saw_refusal = False
    for bump in range(255, -1, -1):
        try:
            create_program_address([b"bonding-curve", b58decode(mint), bytes([bump])], PUMP)
        except ValueError:
            saw_refusal = True
            break
    assert saw_refusal


def test_seed_limits():
    with pytest.raises(ValueError):
        create_program_address([b"x" * 33], PUMP)
    with pytest.raises(ValueError):
        create_program_address([b"x"] * 17, PUMP)


def test_pool_pda_shape(settings):
    pf = settings.config("pumpfun")
    mint = b58encode(hashlib.sha256(b"mint-1").digest())
    pool = pump_amm_pool_address(mint, pf["program_id"], pf["pump_amm_program_id"], pf["wsol_mint"])
    assert len(b58decode(pool)) == 32 and not is_on_curve(b58decode(pool))
