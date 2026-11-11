"""A cancellable, time-limited bridge to the local Ollama server."""

import asyncio
import atexit
from contextlib import suppress
from concurrent.futures import TimeoutError as FutureTimeout
from concurrent.futures import CancelledError as FutureCancelled
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread

import httpx
import ollama

from config import MODEL_TIMEOUT_SECONDS, OLLAMA_HOST
from core.cancellation import check_cancelled
from core.latency import current_trace, span
from core.inference_priority import priority as inference_priority


class _Transport:
    """One reusable async connection pool; callbacks stay on their caller thread."""
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.client = None
        self._ready = Event()
        self.thread = Thread(target=self._run, name="Capri model transport", daemon=True)
        self.thread.start()
        if not self._ready.wait(2):
            raise RuntimeError("The local model transport did not start")

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        self.loop.run_forever()
        self.loop.close()

    async def request(self, cancel_event, chunks, kwargs, trace):
        if self.client is None:
            self.client = ollama.AsyncClient(host=OLLAMA_HOST, trust_env=False,
                timeout=httpx.Timeout(MODEL_TIMEOUT_SECONDS, connect=1),
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=4, keepalive_expiry=300))
            await self.client.__aenter__()
        return await _request_chat(cancel_event, chunks, self.client, kwargs, trace)

    async def _close(self):
        tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.client is not None:
            await self.client.__aexit__(None, None, None)
            self.client = None

    def close(self):
        if not self.thread.is_alive():
            return
        future = asyncio.run_coroutine_threadsafe(self._close(), self.loop)
        try:
            future.result(timeout=2)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=2)


_transport = None
_transport_lock = Lock()


def shutdown():
    global _transport
    with _transport_lock:
        transport, _transport = _transport, None
    if transport is not None:
        transport.close()


atexit.register(shutdown)


async def _request_chat(cancel_event, chunks, client, kwargs, trace):
    check_cancelled(cancel_event)
    async with asyncio.timeout(MODEL_TIMEOUT_SECONDS):
        delivered_text = False
        async def generate_once():
            nonlocal delivered_text
            if trace:
                trace.mark("request_dispatched")
            if chunks is None:
                return await client.chat(**kwargs)
            text = []
            final = None
            stream = await client.chat(**kwargs, stream=True)
            try:
                async for part in stream:
                    check_cancelled(cancel_event)
                    final = part
                    content = part.message.content or ""
                    if content:
                        if trace:
                            trace.mark("llm_first_token")
                        delivered_text = True
                        text.append(content)
                        acknowledged = asyncio.get_running_loop().create_future()
                        while True:
                            check_cancelled(cancel_event)
                            try:
                                chunks.put_nowait((content, acknowledged))
                                break
                            except Full:
                                await asyncio.sleep(.005)
                        await acknowledged  # Bounded backpressure; other HTTP requests can still run.
                # Keep Ollama's token counts, compute times and load durations.
                result = final or ollama.ChatResponse(message={"role": "assistant"})
                result.message.content = "".join(text)
                return result
            finally:
                await stream.aclose()
        async def generate():
            for attempt in range(3):
                check_cancelled(cancel_event)
                try:
                    return await generate_once()
                except (ConnectionError, httpx.ConnectError, httpx.ConnectTimeout) as error:
                    # A restarting local server can briefly refuse connections.
                    # Never replay a reply once any text reached chat or TTS.
                    if delivered_text:
                        raise RuntimeError(
                            "Ollama's connection was interrupted while replying. "
                            "The partial reply was kept; try again."
                        ) from error
                    if attempt == 2:
                        raise
                    await asyncio.sleep(.2 * (attempt + 1))
        task = asyncio.create_task(generate())
        try:
            return await task
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            elif not task.cancelled():
                # Retrieve simultaneous request failures even if Stop won.
                task.exception()


def request_chat(cancel_event=None, on_text=None, **kwargs):
    """Run from a worker thread; Stop cancels even a stalled HTTP request."""
    global _transport
    background = kwargs.pop("background", False)
    if background:
        with inference_priority.background(cancel_event) as token:
            return request_chat(token, on_text, **kwargs)
    trace = current_trace()
    check_cancelled(cancel_event)
    chunks = Queue(maxsize=32) if on_text is not None else None
    with _transport_lock:
        if _transport is None:
            _transport = _Transport()
        transport = _transport
    future = asyncio.run_coroutine_threadsafe(transport.request(cancel_event, chunks, kwargs, trace), transport.loop)
    try:
        with span("model_request"):
            while True:
                check_cancelled(cancel_event)
                if chunks is not None:
                    try:
                        text, acknowledged = chunks.get(timeout=.01)
                    except Empty:
                        pass
                    else:
                        check_cancelled(cancel_event)
                        on_text(text)
                        def acknowledge(future=acknowledged):
                            if not future.done():
                                future.set_result(None)
                        transport.loop.call_soon_threadsafe(acknowledge)
                        continue
                if future.done():
                    response = future.result()
                    if trace:
                        for name in ("load_duration", "prompt_eval_duration", "eval_duration"):
                            value = getattr(response, name, None)
                            if isinstance(value, int) and value >= 0:
                                trace.collector.record("ollama_" + name, value)
                        count, duration = response.eval_count, response.eval_duration
                        if count and duration:
                            trace.collector.record("ollama_ns_per_token", duration / count)
                            if hasattr(trace.collector, "record_tokens"):
                                trace.collector.record_tokens(count, duration)
                    return response
                if chunks is None:
                    try:
                        future.result(timeout=.01)
                    except FutureTimeout:
                        pass
    except (ConnectionError, httpx.ConnectError, httpx.ConnectTimeout) as error:
        raise RuntimeError(
            f"Ollama isn't reachable at {OLLAMA_HOST}. Open Ollama and try again. "
            "Background screen reading reconnects automatically."
        ) from error
    except FutureCancelled:
        from core.cancellation import OperationCancelled
        raise OperationCancelled() from None
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
    finally:
        if not future.done():
            future.cancel()
            # Cancellation is acknowledged on the transport loop before the
            # caller can start another turn or release a background model slot.
            with suppress(Exception):
                asyncio.run_coroutine_threadsafe(asyncio.sleep(0), transport.loop).result(timeout=.2)
