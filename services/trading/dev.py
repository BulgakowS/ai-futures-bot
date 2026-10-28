import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from services.trading.worker import run_worker


@asynccontextmanager
async def lifespan(app):
    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(stop))
    try:
        yield
    finally:
        stop.set()
        await task


app = FastAPI(lifespan=lifespan)
