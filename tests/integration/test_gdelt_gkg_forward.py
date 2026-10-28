"""Bounded GKG candidate ingestion without real network or active database."""

import asyncio
import hashlib
import io
import os
import zipfile
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from ai_futures.config import Settings
from ai_futures.db import Database
from ai_futures.gdelt_gkg_forward import (
    MAX_ARCHIVE,
    MAX_MANIFEST,
    GkgForwardPoller,
    _Failure,
    parse_archive,
    parse_manifest,
)
from ai_futures.gdelt_gkg_models import GkgReleaseRevision, GkgScan, GkgSlotObservation
from ai_futures.gdelt_gkg_pilot import latest_scan_at
from sqlalchemy import func, select, text
from sqlalchemy.exc import DatabaseError

NOW = datetime(2026, 10, 18, 20, 0, tzinfo=UTC)
BATCH = "20261018194500"


def row(batch=BATCH, *, theme="ECON_BITCOIN", tone="-0.5,1,2,3,4,5,6"):
    columns = ["x"] * 27
    columns[1] = batch
    columns[7] = theme
    columns[15] = tone
    columns[4] = "https://publisher.example/private"
    return "\t".join(columns)


def zipped(lines, batch=BATCH, *, filename=None):
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(filename or f"{batch}.gkg.csv", "\n".join(lines) + "\n")
    return target.getvalue()


def manifest(archive, batch=BATCH, *, md5=None, size=None, url=None):
    digest = md5 or hashlib.md5(archive, usedforsecurity=False).hexdigest()
    name = url or f"http://data.gdeltproject.org/gdeltv2/{batch}.gkg.csv.zip"
    return f"{size or len(archive)} {digest} {name}\n".encode()


@pytest.fixture
async def gkg_db(tmp_path):
    settings = Settings(
        _env_file=None,
        app_env="test",
        gkg_forward_enabled=True,
        backend_auth_token="x" * 48,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'gkg.db'}",
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


def test_manifest_and_archive_strict_validation():
    body = zipped([row(theme="ECON_BITCOIN_EXTRA;ECON_BITCOIN"), row(theme="ECON_BITCOIN_EXTRA")])
    entry = parse_manifest(manifest(body), NOW)
    assert entry.url == f"https://data.gdeltproject.org/gdeltv2/{BATCH}.gkg.csv.zip"
    assert (parse_archive(body, BATCH).total_rows, parse_archive(body, BATCH).btc_rows) == (2, 1)
    assert parse_archive(body, BATCH).mean_tone == "-0.5"
    assert parse_archive(zipped([row(theme="OTHER")]), BATCH).mean_tone is None
    for bad in (
        manifest(body, url="https://evil.example/gdeltv2/20261018194500.gkg.csv.zip"),
        manifest(body, url="https://data.gdeltproject.org/evil/20261018194500.gkg.csv.zip"),
        manifest(body) + manifest(body),
        b"not a manifest",
        manifest(body, size=MAX_ARCHIVE + 1),
        manifest(body, batch="20261018200100"),
    ):
        with pytest.raises(_Failure):
            parse_manifest(bad, NOW)
    for bad in (
        zipped([row() + "\textra"]),
        zipped([row(batch="20261018193000")]),
        zipped([row(tone="NaN,1,2,3,4,5,6")]),
        zipped([row(tone="1,2,3,4,5,6")]),
        zipped([row()], filename="wrong.csv"),
        zipped([row()] * 400_000),  # compressed small; declared inflated size exceeds 30 MiB
    ):
        with pytest.raises(_Failure):
            parse_archive(bad, BATCH)


async def test_default_off_first_seen_repeat_revision_and_immutable(gkg_db):
    database, settings = gkg_db
    first_body = zipped([row(), row(theme="OTHER")])
    revised_body = zipped([row(tone="-1,1,2,3,4,5,6"), row(theme="OTHER")])
    state = {"body": first_body}
    calls = []

    def respond(request):
        calls.append(str(request.url))
        assert request.method == "GET" and request.url.host == "data.gdeltproject.org"
        if request.url.path.endswith("lastupdate.txt"):
            return httpx.Response(200, content=manifest(state["body"]))
        assert request.url.path == f"/gdeltv2/{BATCH}.gkg.csv.zip"
        return httpx.Response(200, content=state["body"])

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        poller = GkgForwardPoller(database, settings, client=client)
        with pytest.raises(PermissionError):
            await GkgForwardPoller(
                database, settings.model_copy(update={"gkg_forward_enabled": False}), client=client
            ).poll_once(observed_at=NOW)
        assert not calls
        first = await poller.poll_once(observed_at=NOW)
        assert (first.status, first.revision, first.archive_bytes) == ("ok", 1, len(first_body))
        assert await latest_scan_at(database) == NOW
        assert (
            await poller.poll_once(observed_at=NOW + timedelta(minutes=1))
        ).status == "throttled"
        assert len(calls) == 2
        repeated = await poller.poll_once(observed_at=NOW + timedelta(minutes=16))
        assert (repeated.status, repeated.archive_bytes) == ("repeat", 0)
        assert len(calls) == 3  # one manifest, no second ZIP
        state["body"] = revised_body
        revised = await poller.poll_once(observed_at=NOW + timedelta(minutes=32))
        assert (revised.status, revised.revision) == ("ok", 2)
        assert len(calls) == 5
        async with database.sessions() as session:
            revisions = (
                await session.scalars(
                    select(GkgReleaseRevision).order_by(GkgReleaseRevision.revision)
                )
            ).all()
            assert [item.mean_btc_document_tone for item in revisions] == ["-0.5", "-1"]
            assert revisions[0].first_seen_at == revisions[1].first_seen_at == NOW
            assert revisions[1].observed_at == NOW + timedelta(minutes=32)
            assert revisions[0].archive_sha256 != revisions[1].archive_sha256
            asof = (
                await session.scalars(
                    select(GkgReleaseRevision).where(
                        GkgReleaseRevision.observed_at <= NOW + timedelta(minutes=16)
                    )
                )
            ).all()
            assert [item.revision for item in asof] == [1]
            assert await session.scalar(select(func.count()).select_from(GkgScan)) == 3
            assert await session.scalar(select(func.count()).select_from(GkgSlotObservation)) == 6
            with pytest.raises(DatabaseError):
                await session.execute(text("UPDATE gkg_release_revision SET btc_rows=0"))
            await session.rollback()
            with pytest.raises(DatabaseError):
                await session.execute(text("DELETE FROM gkg_scan"))
            await session.rollback()
            with pytest.raises(DatabaseError):
                await session.execute(text("DELETE FROM gkg_slot_observation"))
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    ("response_kind", "expected"),
    [
        ("manifest_429", "manifest_http_429"),
        ("timeout", "transport"),
        ("manifest_schema", "manifest_schema"),
        ("archive_429", "archive_http_429"),
        ("size", "archive_size"),
        ("md5", "archive_md5"),
        ("zip", "archive_schema"),
    ],
)
async def test_failures_are_unknown_and_no_second_archive(gkg_db, response_kind, expected):
    database, settings = gkg_db
    body = zipped([row()])
    calls = []

    def respond(request):
        calls.append(request.url.path)
        is_manifest = request.url.path.endswith("lastupdate.txt")
        if response_kind == "timeout":
            raise httpx.ReadTimeout("mock timeout")
        if response_kind == "manifest_429" and is_manifest:
            return httpx.Response(429)
        if response_kind == "manifest_schema" and is_manifest:
            return httpx.Response(200, content=b"bad")
        if is_manifest:
            if response_kind == "size":
                return httpx.Response(200, content=manifest(body, size=len(body) + 1))
            if response_kind == "md5":
                return httpx.Response(200, content=manifest(body, md5="0" * 32))
            if response_kind == "zip":
                return httpx.Response(200, content=manifest(b"x" * len(body)))
            return httpx.Response(200, content=manifest(body))
        if response_kind == "archive_429":
            return httpx.Response(429)
        if response_kind == "zip":
            return httpx.Response(200, content=b"x" * len(body))
        return httpx.Response(200, content=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        result = await GkgForwardPoller(database, settings, client=client).poll_once(
            observed_at=NOW
        )
        assert (result.status, result.error_code) == ("error", expected)
        assert len(calls) == (
            1 if response_kind in {"manifest_429", "timeout", "manifest_schema"} else 2
        )
        async with database.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(GkgReleaseRevision)) == 0
            slots = (await session.scalars(select(GkgSlotObservation))).all()
            assert len(slots) == 1 and slots[0].status == "error"
    finally:
        await client.aclose()


async def test_missing_slot_is_unknown_and_zero_is_valid(gkg_db):
    database, settings = gkg_db
    batches = ["20261018194500", "20261018201500"]
    index = 0
    calls = []

    def respond(request):
        calls.append(request.url.path)
        body = zipped([row(batch=batches[index], theme="OTHER")], batch=batches[index])
        if request.url.path.endswith("lastupdate.txt"):
            return httpx.Response(200, content=manifest(body, batch=batches[index]))
        return httpx.Response(200, content=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        poller = GkgForwardPoller(database, settings, client=client)
        assert (await poller.poll_once(observed_at=NOW)).status == "ok"
        index = 1
        assert (await poller.poll_once(observed_at=NOW + timedelta(minutes=31))).status == "ok"
        assert len(calls) == 4
        async with database.sessions() as session:
            slots = (
                await session.scalars(
                    select(GkgSlotObservation).order_by(GkgSlotObservation.slot_at)
                )
            ).all()
            assert [(slot.slot_at.strftime("%H:%M"), slot.status) for slot in slots] == [
                ("19:45", "ok"),
                ("20:00", "unknown"),
                ("20:15", "ok"),
            ]
            releases = (await session.scalars(select(GkgReleaseRevision))).all()
            assert all(
                item.btc_rows == 0 and item.mean_btc_document_tone is None for item in releases
            )
    finally:
        await client.aclose()


async def test_pilot_archive_budget_stops_before_zip_and_journals_error(gkg_db):
    database, settings = gkg_db
    body = zipped([row(theme=f"THEME_{index}") for index in range(2000)])
    assert len(body) > MAX_MANIFEST
    paths = []

    def respond(request):
        paths.append(request.url.path)
        assert request.url.path.endswith("lastupdate.txt")
        return httpx.Response(200, content=manifest(body))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await GkgForwardPoller(database, settings, client=client).poll_once(
            observed_at=NOW, download_budget_bytes=MAX_MANIFEST
        )
    assert (result.status, result.error_code, result.archive_bytes) == (
        "error",
        "download_budget",
        0,
    )
    assert len(paths) == 1
    async with database.sessions() as session:
        scan = await session.scalar(select(GkgScan))
        assert scan.error_code == "download_budget" and scan.archive_bytes == 0
        assert await session.scalar(select(func.count()).select_from(GkgReleaseRevision)) == 0
