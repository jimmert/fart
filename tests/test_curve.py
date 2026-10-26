import pytest

from fart.chain import curve as cv


@pytest.fixture
def params(settings):
    return cv.CurveParams.from_config(settings.config("pumpfun"))


def test_initial_price_and_mcap(params):
    s = cv.CurveState.initial(params)
    # 30 SOL / 1.073e9 tokens
    assert s.price_sol(params) == pytest.approx(30 / 1.073e9)
    assert s.mcap_sol(params) == pytest.approx(30 / 1.073 , rel=1e-9)  # ~27.96 SOL
    assert cv.graduation_progress(s, params) == 0.0
    assert cv.sol_to_graduate(s, params) == pytest.approx(85.0)


def test_buy_quote_matches_constant_product(params):
    s = cv.CurveState.initial(params)
    q = cv.buy_quote(s, params, 1_000_000_000)  # 1 SOL incl. fee
    assert q.sol_to_curve + q.fee == 1_000_000_000
    assert q.fee == pytest.approx(q.sol_to_curve * 0.01, abs=2)
    k = s.k
    expected = s.virtual_token - (k // (s.virtual_sol + q.sol_to_curve) + 1)
    assert q.tokens_out == expected
    assert q.new_state.k >= k  # rounding is in the curve's favour
    assert q.price_impact > 0 and q.slippage_bps > 0
    assert q.fill_price > q.spot_price
    assert not q.partial


def test_buy_then_sell_round_trip_loses_only_fees(params):
    s = cv.CurveState.initial(params)
    q = cv.buy_quote(s, params, 2_000_000_000)
    sq = cv.sell_quote(q.new_state, params, q.tokens_out)
    # gross proceeds equal what went into the curve (within integer rounding)
    assert abs(sq.sol_out_gross - q.sol_to_curve) <= 2
    total_fees = q.fee + sq.fee
    assert 2_000_000_000 - sq.sol_out_net == pytest.approx(total_fees, abs=2)
    assert 0.019 < total_fees / 2e9 < 0.021  # ~2% round trip
    # curve back to (almost) initial
    assert abs(sq.new_state.virtual_sol - s.virtual_sol) <= 2
    assert sq.new_state.virtual_token == s.virtual_token


def test_graduation_progress_reaches_one_when_real_tokens_sold_out(params):
    s = cv.CurveState.initial(params)
    # buy everything: a huge order is capped at the remaining real tokens
    q = cv.buy_quote(s, params, 10_000 * 10**9)
    assert q.partial is True
    assert q.tokens_out == params.initial_real_token
    real_sol = q.new_state.real_sol / 1e9
    # ~85 SOL needed to clear the curve with these parameters
    assert 84.0 < real_sol < 86.5
    assert cv.graduation_progress(q.new_state, params) == pytest.approx(min(1.0, real_sol / 85.0), abs=0.02)
    assert q.new_state.real_token == 0


def test_graduation_progress_from_virtual_when_real_unknown(params):
    s = cv.CurveState(virtual_sol=72_500_000_000, virtual_token=444_000_000_000_000)
    assert cv.graduation_progress(s, params) == pytest.approx(42.5 / 85.0)


def test_price_impact_grows_with_size(params):
    s = cv.CurveState.initial(params)
    small = cv.buy_quote(s, params, 100_000_000)
    big = cv.buy_quote(s, params, 5_000_000_000)
    assert big.price_impact > small.price_impact
    assert big.slippage_bps > small.slippage_bps


def test_sol_for_tokens_inverts_buy(params):
    s = cv.CurveState.initial(params)
    q = cv.buy_quote(s, params, 3_000_000_000)
    need = cv.sol_for_tokens(s, params, q.tokens_out)
    assert abs(need - q.sol_to_curve) <= 2


def test_from_pumpportal_floats(params):
    s = cv.CurveState.from_sol_floats(31.5, 1_022_000_000.0, params)
    assert s.virtual_sol == 31_500_000_000
    assert s.virtual_token == 1_022_000_000_000_000
    assert s.mcap_sol(params) == pytest.approx(31.5 / 1.022, rel=1e-9)


def test_dev_buy_fraction(params):
    assert cv.dev_buy_fraction_of_curve(None, params) is None
    assert cv.dev_buy_fraction_of_curve(42.5, params) == pytest.approx(0.5)


def test_invalid_inputs(params):
    s = cv.CurveState.initial(params)
    with pytest.raises(ValueError):
        cv.buy_quote(s, params, 0)
    with pytest.raises(ValueError):
        cv.sell_quote(s, params, 0)
