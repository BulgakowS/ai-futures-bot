import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from ai_futures.mexc_api_maker_gate import (
    API_MAKER_SCOPE,
    AccountFeeEvidence,
    MakerGateError,
    MexcApiMakerConfirmation,
    assess_api_maker_zero,
    parse_public_api_contract,
)
from ai_futures.mexc_private_readonly import parse_account_fee
from ai_futures.mexc_zero_fee_catalog import parse_public_zero_fee_catalog

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
SYMBOL = "LINK_USDT"
ACCOUNT = "account_fixture_01"
AUTO = object()


def public_row(**changes):
    return {
        "symbol": SYMBOL,
        "baseCoin": "LINK",
        "quoteCoin": "USDT",
        "settleCoin": "USDT",
        "state": 0,
        "futureType": 1,
        "apiAllowed": True,
        "contractSize": "0.1",
        "priceUnit": "0.001",
        "volUnit": "1",
        "minVol": "1",
        "maxVol": "10000",
        "makerFeeRate": "0",
        "takerFeeRate": "0",
        "isZeroFeeRate": True,
        "isZeroFeeSymbol": True,
        "feeRateType": "TEMP",
    } | changes


def zone_catalog(rows=None, observed_at=NOW):
    content = json.dumps({"success": True, "code": 0, "data": rows or [public_row()]}).encode()
    return parse_public_zero_fee_catalog(content, observed_at)


def fee_evidence(maker="0", taker="0.0004", observed_at=None):
    body = {
        "success": True,
        "code": 0,
        "data": {
            "symbol": SYMBOL,
            "feeRateMode": "NORMAL",
            "realMakerFee": maker,
            "realTakerFee": taker,
        },
    }
    fee = parse_account_fee(json.dumps(body).encode(), SYMBOL, observed_at or NOW)
    return AccountFeeEvidence(ACCOUNT, fee, "a" * 64)


def confirmation(**changes):
    return replace(
        MexcApiMakerConfirmation(
            issuer="MEXC",
            channel="mexc_support_written",
            reference="mock-ticket-1234",
            artifact_sha256="b" * 64,
            account_ref=ACCOUNT,
            symbol=SYMBOL,
            scope=API_MAKER_SCOPE,
            maker_rate=Decimal("0"),
            issued_at=NOW - timedelta(hours=1),
            expires_at=NOW + timedelta(days=1),
            reviewed_at=NOW - timedelta(minutes=10),
            reviewer_ref="fixture-reviewer",
        ),
        **changes,
    )


def assess(public=None, account_fee=None, proof=None, catalog=AUTO, **changes):
    current_public = public or parse_public_api_contract(public_row(), SYMBOL, NOW)
    return assess_api_maker_zero(
        symbol=SYMBOL,
        account_ref=ACCOUNT,
        zone_catalog=zone_catalog() if catalog is AUTO else catalog,
        public=current_public,
        account_fee=account_fee if account_fee is not None else fee_evidence(),
        confirmation=proof,
        now=NOW,
        **changes,
    )


def test_maker_only_zero_with_nonzero_account_taker():
    result = assess(proof=confirmation())
    assert result.conditions_met is True
    assert result.reason == "CONDITIONS_MET_REVIEW_ONLY"
    assert result.taker_rate == Decimal("0.0004")
    assert result.live_ready is False


def test_missing_confirmation_fails_even_when_both_public_flags_zero():
    public = parse_public_api_contract(
        public_row(makerFeeRate="0", takerFeeRate="0", isZeroFeeRate=True, isZeroFeeSymbol=True),
        SYMBOL,
        NOW,
    )
    result = assess(public=public)
    assert result.conditions_met is False
    assert result.reason == "MEXC_API_CONFIRMATION_MISSING"


def test_missing_stale_wrong_symbol_and_non_candidate_zone_deny():
    assert assess(catalog=None, proof=confirmation()).reason == "PUBLIC_ZONE_MISSING"
    stale = zone_catalog(observed_at=NOW - timedelta(minutes=6))
    assert assess(catalog=stale, proof=confirmation()).reason == "PUBLIC_ZONE_STALE_OR_INVALID"
    future = zone_catalog(observed_at=NOW + timedelta(seconds=1))
    assert assess(catalog=future, proof=confirmation()).reason == "PUBLIC_ZONE_STALE_OR_INVALID"
    other = zone_catalog(rows=[public_row(symbol="ARB_USDT", baseCoin="ARB")])
    assert assess(catalog=other, proof=confirmation()).reason == (
        "PUBLIC_ZONE_SYMBOL_NOT_CANDIDATE"
    )
    noncandidate = zone_catalog(rows=[public_row(isZeroFeeSymbol=False)])
    assert assess(catalog=noncandidate, proof=confirmation()).reason == (
        "PUBLIC_ZONE_SYMBOL_NOT_CANDIDATE"
    )
    unknown = zone_catalog(rows=[public_row(isZeroFeeSymbol=None)])
    assert unknown.unknown_rows == 1
    assert assess(catalog=unknown, proof=confirmation()).reason == (
        "PUBLIC_ZONE_SYMBOL_NOT_CANDIDATE"
    )


def test_public_contract_must_be_from_same_observation_as_zone():
    older = parse_public_api_contract(public_row(), SYMBOL, NOW - timedelta(seconds=1))
    assert assess(public=older, proof=confirmation()).reason == "PUBLIC_CONTRACT_UNSUITABLE"


@pytest.mark.parametrize(
    "changes",
    [
        {"issuer": "trader"},
        {"channel": "telegram"},
        {"account_ref": "different_account"},
        {"symbol": "ARB_USDT"},
        {"scope": "web_trades"},
        {"maker_rate": Decimal("0.0001")},
        {"artifact_sha256": ""},
        {"reference": ""},
        {"reviewer_ref": ""},
        {"expires_at": NOW},
        {"issued_at": NOW + timedelta(minutes=1)},
        {"reviewed_at": NOW + timedelta(minutes=1)},
    ],
)
def test_confirmation_mismatch_expiry_or_incomplete_provenance_fails(changes):
    result = assess(proof=confirmation(**changes))
    assert result.conditions_met is False
    assert result.reason == "MEXC_API_CONFIRMATION_INVALID"


def test_rate_change_and_stale_fee_fail():
    assert assess(account_fee=fee_evidence("0.0001"), proof=confirmation()).reason == (
        "MAKER_RATE_NOT_ZERO"
    )
    stale = fee_evidence(observed_at=NOW - timedelta(minutes=6))
    assert assess(account_fee=stale, proof=confirmation()).reason == "ACCOUNT_FEE_INVALID"
    malformed_binding = replace(fee_evidence(), account_binding_sha256="")
    assert assess(account_fee=malformed_binding, proof=confirmation()).reason == (
        "ACCOUNT_FEE_INVALID"
    )
    unsupported_mode = replace(fee_evidence().fee, fee_rate_mode="TIERED")
    assert (
        assess(
            account_fee=replace(fee_evidence(), fee=unsupported_mode), proof=confirmation()
        ).reason
        == "ACCOUNT_FEE_INVALID"
    )


def test_confirmation_must_precede_current_account_fee_snapshot():
    later = confirmation(
        issued_at=NOW - timedelta(minutes=1), reviewed_at=NOW - timedelta(minutes=1)
    )
    earlier_fee = fee_evidence(observed_at=NOW - timedelta(minutes=2))
    assert assess(account_fee=earlier_fee, proof=later).reason == "MEXC_API_CONFIRMATION_INVALID"


def test_public_contract_state_api_and_freshness_fail():
    for row in (public_row(state=1), public_row(apiAllowed=False), public_row(futureType=2)):
        public = parse_public_api_contract(row, SYMBOL, NOW)
        assert assess(public=public, proof=confirmation()).reason == "PUBLIC_CONTRACT_UNSUITABLE"
    old = parse_public_api_contract(public_row(), SYMBOL, NOW - timedelta(minutes=6))
    assert assess(public=old, proof=confirmation()).reason == "PUBLIC_CONTRACT_UNSUITABLE"


@pytest.mark.parametrize(
    "changes",
    [
        {"symbol": "ARB_USDT"},
        {"baseCoin": "ARB"},
        {"quoteCoin": "USD"},
        {"settleCoin": "USD"},
        {"apiAllowed": "true"},
        {"contractSize": "0"},
        {"priceUnit": "NaN"},
        {"minVol": "10001"},
    ],
)
def test_public_contract_parser_rejects_malformed_rules(changes):
    with pytest.raises(MakerGateError):
        parse_public_api_contract(public_row(**changes), SYMBOL, NOW)


def test_wrong_fee_account_binding_and_no_boolean_proof_fail():
    account_fee = replace(fee_evidence(), account_ref="different_account")
    assert assess(account_fee=account_fee, proof=confirmation()).reason == "ACCOUNT_FEE_MISSING"
    assert assess(proof=True).reason == "MEXC_API_CONFIRMATION_MISSING"
