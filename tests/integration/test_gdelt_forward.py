"""GDELT forward-only aggregate and failure contracts, with no external HTTP."""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.gdelt_forward import GdeltForwardPoller, parse_volume
from ai_futures.gdelt_models import GdeltVolumeRevision, GdeltVolumeScan
from sqlalchemy import func, select, text
from sqlalchemy.exc import DatabaseError

NOW = datetime(2026, 10, 18, 19, 15, tzinfo=UTC)


def payload(now=NOW, values=None):
    values = values or [(now - timedelta(minutes=15 * i), i % 4 / 10) for i in range(96)]
    return {
        "query_details": {"title": "bitcoin", "date_resolution": "15m"},
        "timeline": [
            {
                "series": "Volume Intensity",
                "data": [
                    {"date": stamp.strftime("%Y%m%dT%H%M%SZ"), "value": value}
                    for stamp, value in sorted(values)
                ],
            }
        ],
    }


@pytest.fixture
async def gdelt_db(tmp_path):
    settings = Settings(
        _env_file=None,
        app_env="test",
        gdelt_forward_enabled=True,
        backend_auth_token="x" * 48,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'gdelt.db'}",
    )
    process = await asyncio.create_subprocess_exec(
        ".venv/bin/alembic",
        "upgrade",
        "head",
        env=os.environ
        | {
            "APP_ENV": "test",
            "BACKEND_AUTH_TOKEN": "x" * 48,
            "DATABASE_URL": settings.database_url.get_secret_value(),
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output = await process.communicate()
    assert process.returncode == 0, output
    database = Database(settings)
    yield database, settings
    await database.close()


async def test_default_off_and_strict_owned_fields(gdelt_db):
    database, settings = gdelt_db
    calls = []
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200))
    )
    try:
        with pytest.raises(PermissionError):
            await GdeltForwardPoller(
                database,
                settings.model_copy(update={"gdelt_forward_enabled": False}),
                client=client,
            ).poll_once(observed_at=NOW)
        assert not calls
        with pytest.raises(ValueError):
            parse_volume(json.dumps(payload() | {"articles": []}).encode(), NOW)
        with pytest.raises(ValueError):
            parse_volume(
                json.dumps(
                    payload()
                    | {
                        "query_details": {
                            "title": "bitcoin",
                            "date_resolution": "15m",
                            "url": "https://publisher.example/",
                        }
                    }
                ).encode(),
                NOW,
            )
        with pytest.raises(ValueError):
            parse_volume(json.dumps(payload(values=[(NOW, float("nan"))])).encode(), NOW)
        with pytest.raises(ValueError):
            parse_volume(json.dumps(payload(values=[(NOW + timedelta(hours=1), 1)])).encode(), NOW)
    finally:
        await client.aclose()


async def test_forward_revisions_coverage_restart_and_immutable(gdelt_db):
    database, settings = gdelt_db
    body = payload()
    calls = []

    def respond(request):
        calls.append(request)
        assert request.method == "GET" and request.url.host == "api.gdeltproject.org"
        assert request.url.path == "/api/v2/doc/doc"
        assert dict(request.url.params) == {
            "query": "bitcoin",
            "mode": "timelinevol",
            "format": "json",
            "timespan": "1d",
        }
        return httpx.Response(200, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        poller = GdeltForwardPoller(database, settings, client=client)
        first = await poller.poll_once(observed_at=NOW)
        assert (first.status, first.points, first.inserted, first.revised) == ("ok", 96, 96, 0)
        assert (
            await poller.poll_once(observed_at=NOW + timedelta(minutes=1))
        ).status == "throttled"
        assert len(calls) == 1
        body["timeline"][0]["data"][-1]["value"] = 0.9
        second = await poller.poll_once(observed_at=NOW + timedelta(minutes=16))
        assert (second.status, second.inserted, second.revised) == ("gap", 1, 1)
        third = await poller.poll_once(observed_at=NOW + timedelta(minutes=32))
        assert (third.inserted, third.revised) == (0, 0)
        async with database.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(GdeltVolumeRevision)) == 97
            rows = (
                await session.scalars(
                    select(GdeltVolumeRevision)
                    .where(GdeltVolumeRevision.point_at == NOW)
                    .order_by(GdeltVolumeRevision.revision)
                )
            ).all()
            assert [row.revision for row in rows] == [1, 2]
            assert rows[0].first_seen_at == rows[1].first_seen_at == NOW
            assert rows[1].observed_at == NOW + timedelta(minutes=16)
            scans = (await session.scalars(select(GdeltVolumeScan))).all()
            assert [scan.status for scan in scans] == ["ok", "gap", "gap"]
            assert all(scan.config_version == settings.config_version for scan in scans)
            with pytest.raises(DatabaseError):
                await session.execute(text("UPDATE gdelt_volume_revision SET value='0'"))
            await session.rollback()
            with pytest.raises(DatabaseError):
                await session.execute(text("DELETE FROM gdelt_volume_scan"))
    finally:
        await client.aclose()


async def test_sparse_and_failures_do_not_invent_points(gdelt_db):
    database, settings = gdelt_db
    sparse = payload(values=[(NOW - timedelta(minutes=15), 0), (NOW, 0.2)])
    responses = [
        httpx.Response(200, json=sparse),
        httpx.Response(429),
        httpx.Response(200, json={"articles": []}),
        httpx.Response(200, content=b"x" * 256_001),
    ]
    calls = []

    def respond(request):
        calls.append(request)
        return responses.pop(0)

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        poller = GdeltForwardPoller(database, settings, client=client)
        a = await poller.poll_once(observed_at=NOW)
        b = await poller.poll_once(observed_at=NOW + timedelta(minutes=16))
        c = await poller.poll_once(observed_at=NOW + timedelta(minutes=32))
        d = await poller.poll_once(observed_at=NOW + timedelta(minutes=48))
        assert (a.status, a.missing_buckets, a.inserted) == ("gap", 94, 2)
        assert (b.status, b.error_code, b.inserted) == ("error", "http_429", 0)
        assert (c.status, c.error_code, c.inserted) == ("error", "transport_or_schema", 0)
        assert (d.status, d.error_code, d.inserted) == ("error", "body_limit", 0)
        assert len(calls) == 4
        async with database.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(GdeltVolumeRevision)) == 2
            assert await session.scalar(select(func.count()).select_from(GdeltVolumeScan)) == 4
    finally:
        await client.aclose()
