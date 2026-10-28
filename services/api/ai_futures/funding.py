"""Public funding history reconciliation; no broker or account API access."""

from datetime import datetime, timedelta

from sqlalchemy import func, select

from ai_futures.mexc import MarketDataError, payload_hash
from ai_futures.models import FundingCoverage, FundingSettlement
from ai_futures.paper_models import PaperFill, PaperPortfolio, PaperPosition
from ai_futures.schemas import utc_now

SOURCE = "mexc_public_funding_history"
PAGE_SIZE = 1000


def settlement_time(row):
    return datetime.fromisoformat(row["settleTime"])


def verified_pages(pages, current, needed_from):
    """Require stable overlapping pages and a cycle-consistent observed sequence."""
    first = pages[0]
    total = first["total_count"]
    found = {}
    previous_times = set()
    for index, page in enumerate(pages):
        if page["total_count"] != total or not page["rows"]:
            raise MarketDataError("Funding page count changed or empty")
        times = [settlement_time(row) for row in page["rows"]]
        if times != sorted(times, reverse=True) or len(times) != len(set(times)):
            raise MarketDataError("Funding page order or duplicate")
        if index and not previous_times.intersection(times):
            raise MarketDataError("Funding page overlap missing")
        for row, when in zip(page["rows"], times, strict=True):
            if when in found and payload_hash(found[when]) != payload_hash(row):
                raise MarketDataError("Conflicting funding settlement")
            found[when] = row
        previous_times = set(times)
    ordered = sorted(found, reverse=True)
    if not ordered or ordered[-1] > needed_from:
        raise MarketDataError("Funding history does not reach held interval")
    predecessor_index = next(index for index, when in enumerate(ordered) if when <= needed_from)
    ordered = ordered[: predecessor_index + 1]
    found = {when: found[when] for when in ordered}
    next_time = datetime.fromisoformat(current["nextSettleTime"])
    if next_time <= utc_now() or ordered[0] > utc_now() or ordered[0] >= next_time:
        raise MarketDataError("Funding current schedule is stale")
    if next_time - ordered[0] != timedelta(hours=current["collectCycle"]):
        raise MarketDataError("Funding latest settlement gap")
    for newer, older in zip(ordered, ordered[1:], strict=False):
        if newer - older != timedelta(hours=found[newer]["collectCycle"]):
            raise MarketDataError("Funding settlement gap")
    return found, ordered[-1], next_time


async def needed_coverage_start(session, symbol):
    return await session.scalar(
        select(func.min(PaperFill.occurred_at))
        .join(PaperPosition, PaperPosition.id == PaperFill.position_id)
        .join(PaperPortfolio, PaperPortfolio.id == PaperPosition.portfolio_id)
        .where(
            PaperPosition.symbol == symbol,
            PaperPosition.epoch == PaperPortfolio.epoch,
            PaperPortfolio.mode == "paper",
        )
    )


async def reconcile_funding(database, adapter, symbol, config_version, current):
    """Persist only a completely verified fetch. A failed fetch invalidates the watermark."""
    async with database.sessions() as session:
        needed = await needed_coverage_start(session, symbol)
    needed = needed or utc_now()
    pages = []
    try:
        for page_num in range(1, PAGE_SIZE + 1):
            size = PAGE_SIZE - page_num + 1
            page = await adapter.funding_history_page(symbol, page_num, size)
            pages.append(page)
            if page_num > 1 and page["total_count"] != pages[0]["total_count"]:
                raise MarketDataError("Funding page shifted")
            if page["rows"] and settlement_time(page["rows"][-1]) <= needed:
                break
        else:
            raise MarketDataError("Funding pagination bound")
        again = await adapter.funding_history_page(symbol, 1, PAGE_SIZE)
        if payload_hash(again) != payload_hash(pages[0]):
            raise MarketDataError("Funding page one shifted")
        found, oldest, until = verified_pages(pages, current, needed)
        digest = payload_hash({when.isoformat(): found[when] for when in sorted(found)})
        async with database.sessions.begin() as session:
            for when, row in found.items():
                existing = await session.get(FundingSettlement, (symbol, when))
                raw_hash = payload_hash(row)
                if existing:
                    if existing.raw_hash != raw_hash:
                        raise MarketDataError("Stored funding settlement changed")
                    continue
                session.add(
                    FundingSettlement(
                        symbol=symbol,
                        settle_time=when,
                        rate=row["fundingRate"],
                        collect_cycle=row["collectCycle"],
                        source=SOURCE,
                        raw=row,
                        raw_hash=raw_hash,
                        observed_at=utc_now(),
                        config_version=config_version,
                    )
                )
            await session.merge(
                FundingCoverage(
                    symbol=symbol,
                    covered_from=oldest,
                    covered_until=until,
                    status="verified",
                    gap_time=None,
                    source=SOURCE,
                    snapshot_hash=digest,
                    observed_at=utc_now(),
                    config_version=config_version,
                )
            )
        return True
    except MarketDataError:
        async with database.sessions.begin() as session:
            await session.merge(
                FundingCoverage(
                    symbol=symbol,
                    covered_from=None,
                    covered_until=None,
                    status="unresolved",
                    gap_time=needed,
                    source=SOURCE,
                    snapshot_hash=None,
                    observed_at=utc_now(),
                    config_version=config_version,
                )
            )
        return False
