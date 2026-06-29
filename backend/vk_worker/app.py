import os

from fastapi import FastAPI

from .worker import run as run_worker

app = FastAPI(title="VK Worker", version="0.1.0")


@app.get("/health")
async def health() -> dict:
    """Simple healthcheck endpoint."""
    return {"status": "ok", "token_id": os.getenv("WORKER_TOKEN_ID", "")}


@app.on_event("startup")
async def _startup() -> None:
    # Run background worker loop in a separate task; the process is expected
    # to be dedicated to this worker (one token_id).
    import asyncio

    asyncio.create_task(_background_worker())


async def _background_worker() -> None:
    # Use the same function as CLI entrypoint.
    from .worker import _main  # type: ignore

    await _main()


if __name__ == "__main__":
    # Allow running as: python backend/vk_worker/app.py
    import uvicorn

    uvicorn.run(
        "vk_worker.app:app",
        host="0.0.0.0",
        port=int(os.getenv("WORKER_PORT", "8080")),
        reload=False,
    )

