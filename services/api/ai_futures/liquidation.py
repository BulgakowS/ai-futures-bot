"""Conservative paper-only isolated-margin estimate from public contract tiers."""

from decimal import Decimal, InvalidOperation

D = Decimal


def risk_tiers(rules):
    """Return ordered contract-volume caps, or None for unsupported public terms."""
    try:
        if rules.get("riskLimitMode") != "CUSTOM" or rules.get("riskLimitType") != "BY_VOLUME":
            return None
        rows = rules["riskLimitCustom"]
        if not isinstance(rows, list) or not rows:
            return None
        tiers = []
        for expected, row in enumerate(rows, 1):
            if not isinstance(row, dict) or type(row.get("level")) is not int:
                return None
            level = row["level"]
            cap, mmr, imr, leverage = (D(str(row[key])) for key in
                                       ("maxVol", "mmr", "imr", "maxLeverage"))
            if (level != expected or not all(x.is_finite() for x in
                                             (cap, mmr, imr, leverage))
                    or cap <= 0 or not 0 < mmr <= imr < 1 or leverage <= 0
                    or imr * leverage > 1
                    or (tiers and (cap <= tiers[-1]["max_vol"]
                                   or mmr < tiers[-1]["mmr"]
                                   or imr < tiers[-1]["imr"]
                                   or leverage > tiers[-1]["max_leverage"]))):
                return None
            tiers.append({"level": level, "max_vol": cap, "mmr": mmr,
                          "imr": imr, "max_leverage": leverage})
        liquidation_fee = D(str(rules["liquidationFeeRate"]))
        taker_fee = D(str(rules["takerFeeRate"]))
        multiplier = D(str(rules["contractSize"]))
        if (not all(x.is_finite() for x in (liquidation_fee, taker_fee, multiplier))
                or min(liquidation_fee, taker_fee) < 0 or multiplier <= 0
                or liquidation_fee + taker_fee + tiers[-1]["mmr"] >= 1):
            return None
        return tiers
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None


def tier_for_quantity(rules, quantity):
    tiers = risk_tiers(rules)
    if tiers is None or quantity <= 0:
        return None
    return next((tier for tier in tiers if quantity <= tier["max_vol"]), None)


def estimate(position, rules, mark):
    """Solve the paper maintenance equality; posted cash flows enter only once."""
    tier = tier_for_quantity(rules, position.quantity)
    if tier is None or mark <= 0 or position.multiplier != D(str(rules["contractSize"])):
        return None
    rate = tier["mmr"] + D(str(rules["liquidationFeeRate"])) + D(str(rules["takerFeeRate"]))
    units = position.quantity * position.multiplier
    isolated_cash = position.margin + position.funding
    if position.direction == "LONG":
        boundary = (position.average_entry * units - isolated_cash) / (units * (1 - rate))
        pnl = (mark - position.average_entry) * units
    else:
        boundary = (position.average_entry * units + isolated_cash) / (units * (1 + rate))
        pnl = (position.average_entry - mark) * units
    return {
        "price": max(D(0), boundary),
        "triggered": isolated_cash + pnl <= mark * units * rate,
        "tier": tier,
        "maintenance_rate": rate,
        "isolated_cash": isolated_cash,
    }
