"""A cancellable, time-limited bridge to the local Ollama server."""

import asyncio
from contextlib import suppress

import httpx
import ollama

from config import MODEL_TIMEOUT_SECONDS, OLLAMA_HOST
from core.cancellation import check_cancelled


async def _request_chat(cancel_event=None, **kwargs):
    check_cancelled(cancel_event)
    timeout = httpx.Timeout(MODEL_TIMEOUT_SECONDS, connect=5)
    async with ollama.AsyncClient(
        host=OLLAMA_HOST, timeout=timeout, trust_env=False,
    ) as client:
        task = asyncio.create_task(client.chat(**kwargs))
        try:
            async with asyncio.timeout(MODEL_TIMEOUT_SECONDS):
                while not task.done():
                    check_cancelled(cancel_event)
                    await asyncio.wait({task}, timeout=0.05)
                check_cancelled(cancel_event)
                return task.result()
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            elif not task.cancelled():
                # Retrieve simultaneous request failures even if Stop won.
                task.exception()


def request_chat(cancel_event=None, **kwargs):
    """Run from a worker thread; Stop cancels even a stalled HTTP request."""
    try:
        return asyncio.run(_request_chat(cancel_event=cancel_event, **kwargs))
    except (TimeoutError, httpx.TimeoutException) as error:
        raise RuntimeError(
            "The local model took too long. Try again or choose a smaller "
            "CAPRI_MODEL in your environment."
        ) from error
    except ollama.ResponseError as error:
        if error.status_code == 404:
            model = kwargs.get("model", "the configured model")
            raise RuntimeError(
                f"Model '{model}' is unavailable. Run: ollama pull {model}"
            ) from error
        raise
