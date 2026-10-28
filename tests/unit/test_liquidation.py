from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from ai_futures.liquidation import estimate, risk_tiers, tier_for_quantity

CAPS = (120000, 310000, 480000, 2800000, 17500000, 19000000)
MMR = ("0.001", "0.004", "0.005", "0.01", "0.02", "0.05")
IMR = ("0.002", "0.005", "0.01", "0.02", "0.05", "0.1")
LEV = (500, 200, 100, 50, 20, 10)


def rules():
    return {
        "riskLimitMode": "CUSTOM", "riskLimitType": "BY_VOLUME",
        "riskLimitCustom": [
            {"level": n, "maxVol": cap, "mmr": MMR[n - 1],
             "imr": IMR[n - 1], "maxLeverage": LEV[n - 1]}
            for n, cap in enumerate(CAPS, 1)
        ],
        "contractSize": "0.0001", "takerFeeRate": "0.0004",
        "liquidationFeeRate": "0.0004",
    }


@pytest.mark.parametrize("index", range(6))
def test_six_contract_volume_boundaries(index):
    data = rules()
    cap = D(CAPS[index])
    tier = tier_for_quantity(data, cap)
    assert tier["level"] == index + 1
    assert tier["mmr"] == D(MMR[index])
    assert cap * D(data["contractSize"]) * D("80000") == cap * D("8")
    following = tier_for_quantity(data, cap + 1)
    assert following is None if index == 5 else following["level"] == index + 2


def position(direction="LONG", funding="0", fees="0"):
    return SimpleNamespace(
        quantity=D("1000"), multiplier=D("0.0001"), average_entry=D("100"),
        margin=D("5"), funding=D(funding), fees=D(fees), direction=direction,
    )


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_isolated_funding_and_posted_entry_fee_once(direction):
    data = rules()
    base = estimate(position(direction), data, D("100"))
    credited = estimate(position(direction, funding="1", fees="0.25"), data, D("100"))
    charged = estimate(position(direction, funding="-1", fees="0.25"), data, D("100"))
    assert credited["price"] == estimate(position(direction, funding="1"), data,
                                          D("100"))["price"]
    if direction == "LONG":
        assert credited["price"] < base["price"] < charged["price"]
    else:
        assert credited["price"] > base["price"] > charged["price"]
    rate = D(MMR[0]) + D("0.0008")
    boundary = base["price"]
    units = D("0.1")
    pnl = (boundary - D("100")) * units * (1 if direction == "LONG" else -1)
    assert abs(base["isolated_cash"] + pnl - boundary * units * rate) < D("1e-25")


@pytest.mark.parametrize("change", [
    {"riskLimitType": "BY_VALUE"}, {"riskLimitMode": "INCREASE"},
    {"riskLimitCustom": []},
])
def test_unsupported_risk_mode_has_no_numeric_estimate(change):
    data = rules() | change
    assert risk_tiers(data) is None
    assert estimate(position(), data, D("100")) is None


def test_missing_conflicting_or_nonfinite_tier_fails_closed():
    for mutation in (
        lambda rows: rows[2].pop("imr"),
        lambda rows: rows[2].update(maxVol=CAPS[1]),
        lambda rows: rows[2].update(level=7),
        lambda rows: rows[2].update(mmr="NaN"),
        lambda rows: rows[2].update(mmr="0.0001"),
        lambda rows: rows[2].update(maxLeverage=500),
    ):
        data = rules()
        mutation(data["riskLimitCustom"])
        assert risk_tiers(data) is None
