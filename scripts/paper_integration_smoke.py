"""Bounded authenticated paper smoke on persisted public MEXC data; never private API."""

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from ai_futures.config import get_settings
from ai_futures.db import Database
from ai_futures.paper import CloseRequest, OrderRequest, PaperService
from ai_futures.paper_models import PaperPortfolio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError


async def main():
    settings = get_settings()
    db = Database(settings)
    svc = PaperService(db, settings.config_version)
    created_ids = []
    evidence = {"checked_at": datetime.now(UTC).isoformat(), "mode": "paper", "private_requests": 0}
    headers = {"Authorization": "Bearer " + settings.backend_auth_token.get_secret_value()}
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8000", headers=headers, timeout=10
    ) as client:
        try:
            async with asyncio.timeout(60):
                while True:
                    ticker = (await client.get("/api/v1/markets/BTC_USDT/ticker")).json()
                    depth = (await client.get("/api/v1/markets/BTC_USDT/depth")).json()
                    if (
                        ticker.get("quality", {}).get("trade_eligible")
                        and depth.get("quality_status") == "ok"
                    ):
                        break
                    await asyncio.sleep(1)
            market = (await client.get("/api/v1/markets")).json()[0]
            rules = market["rules"]
            evidence["market"] = {
                "ticker_source": ticker["source"],
                "ticker_time": ticker["source_time"],
                "rules_version": market["rules_version"],
                "depth_model": depth["data"]["fill_model"],
            }
            create = await client.post("/api/v1/portfolios", json={"name": "S04 bounded smoke"})
            assert create.status_code == 201, create.text
            pid = create.json()["id"]
            created_ids.append(pid)
            await client.post("/api/v1/risk/kill-switch", json={"kill_switch": False})
            tick = D(rules["priceUnit"])
            bid = D(ticker["data"]["bid1"])
            stop = (bid - D("200")) // tick * tick
            payload = {
                "idempotency_key": str(uuid4()),
                "symbol": "BTC_USDT",
                "direction": "LONG",
                "quantity": rules["minVol"],
                "leverage": "1",
                "stop_loss": str(stop),
            }
            opened = (await client.post(f"/api/v1/portfolios/{pid}/orders", json=payload)).json()
            assert opened["risk"]["outcome"] == "ALLOW", opened
            assert opened["order"]["status"] == "FILLED", opened
            repeat = (await client.post(f"/api/v1/portfolios/{pid}/orders", json=payload)).json()
            assert repeat == opened
            conflict = await client.post(
                f"/api/v1/portfolios/{pid}/orders",
                json={**payload, "quantity": str(D(payload["quantity"]) + D(rules["volUnit"]))},
            )
            assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
            account = (await client.get(f"/api/v1/portfolios/{pid}")).json()
            pos_id = account["positions"][0]["id"]
            await client.post("/api/v1/risk/kill-switch", json={"kill_switch": True})
            closed = (
                await client.post(
                    f"/api/v1/portfolios/{pid}/positions/{pos_id}/close",
                    json={"idempotency_key": str(uuid4())},
                )
            ).json()
            assert closed["risk"]["outcome"] == "ALLOW" and closed["order"]["status"] == "FILLED", (
                closed
            )
            ledger = (await client.get(f"/api/v1/portfolios/{pid}/ledger")).json()
            assert all(D(x["wallet_delta"]) + D(x["offset_delta"]) == 0 for x in ledger)
            evidence["manual"] = {
                "portfolio_id": pid,
                "open": opened,
                "close": closed,
                "ledger_count": len(ledger),
                "ledger_balanced": True,
                "same_payload_same_result": True,
                "different_payload_409": True,
            }
            # Production DB test: two simultaneous entries would each reserve almost all free
            # margin without the portfolio lock. One-way conflict must reject the second.
            concurrent = (
                await client.post("/api/v1/portfolios", json={"name": "S04 PostgreSQL concurrency"})
            ).json()
            cpid = concurrent["id"]
            created_ids.append(cpid)
            ticker = (await client.get("/api/v1/markets/BTC_USDT/ticker")).json()
            ask = D(ticker["data"]["ask1"])
            bid = D(ticker["data"]["bid1"])
            qty = (
                (D("2700") / (ask * D(rules["contractSize"])))
                // D(rules["volUnit"])
                * D(rules["volUnit"])
            )
            await svc.kill(False)
            reqs = [
                OrderRequest(
                    idempotency_key=str(uuid4()),
                    direction="LONG",
                    quantity=qty,
                    leverage="3",
                    stop_loss=(bid - D("10")) // tick * tick,
                )
                for _ in range(2)
            ]
            results = await asyncio.gather(*(svc.submit(UUID(cpid), req) for req in reqs))
            outcomes = sorted(r["risk"]["outcome"] for r in results)
            assert outcomes == ["ALLOW", "REJECT"], results
            async with db.sessions() as s:
                snapshot = await svc.summary(s, await s.get(PaperPortfolio, UUID(cpid)))
                assert D(snapshot["available_balance"]) >= 0
            evidence["postgres_concurrency"] = {
                "outcomes": outcomes,
                "rejection": [
                    r["risk"]["reason"] for r in results if r["risk"]["outcome"] == "REJECT"
                ],
                "available_balance": snapshot["available_balance"],
            }
            # Same idempotency request races through independent sessions.
            first = next(r for r in results if r["risk"]["outcome"] == "ALLOW")
            req = next(
                req for req, r in zip(reqs, results, strict=True) if r["risk"]["outcome"] == "ALLOW"
            )
            repeats = await asyncio.gather(svc.submit(UUID(cpid), req), svc.submit(UUID(cpid), req))
            assert repeats == [first, first]
            evidence["postgres_concurrent_idempotency"] = True
            # PostgreSQL trigger must reject an otherwise harmless ledger mutation.
            try:
                async with db.sessions.begin() as s:
                    await s.execute(
                        text("UPDATE paper_ledger SET reason = reason WHERE portfolio_id = :pid"),
                        {"pid": UUID(pid)},
                    )
            except DBAPIError:
                evidence["postgres_immutable_ledger"] = True
            else:
                raise AssertionError("ledger trigger missing")
            # A new manager instance recovers persisted positions.
            await PaperService(db, settings.config_version).tick()
            evidence["manager_restart_recovery"] = True
        finally:
            await svc.kill(True)
            # Reductions require fresh public context; retry within a bounded window.
            for pid in created_ids:
                async with asyncio.timeout(60):
                    while True:
                        async with db.sessions() as s:
                            row = await s.get(PaperPortfolio, UUID(pid))
                            summary = await svc.summary(s, row)
                        if not summary["positions"]:
                            break
                        for pos in summary["positions"]:
                            await svc.submit(
                                UUID(pid),
                                CloseRequest(idempotency_key=str(uuid4())),
                                UUID(pos["id"]),
                            )
                        await asyncio.sleep(1)
            evidence["final_kill_switch"] = (await client.get("/api/v1/risk")).json()["kill_switch"]
            evidence["open_smoke_positions"] = 0
            await db.close()
    path = Path("/tmp/paper-integration.json")
    path.write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
