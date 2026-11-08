"""Vision requests reserve explicit context for images, metadata, and the answer."""

import json
import re
from threading import Lock

import ollama

import config
from core.cancellation import check_cancelled
from core.model_client import request_chat
from core.vision_diagnostics import logger

_context_cache = {}
_cache_lock = Lock()


def _overflow_prompt_tokens(error):
    if error.status_code != 400:
        return False, None
    payload = error.error
    for _ in range(3):
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (ValueError, TypeError):
                break
        elif isinstance(payload, dict) and "error" in payload:
            payload = payload["error"]
        else:
            break
    message = payload.get("message", "") if isinstance(payload, dict) else str(payload)
    match = re.search(r"request\s*\((\d+)\s+tokens\)\s+exceeds the available context size\s*\((\d+)\s+tokens\)", message, re.I)
    explicit = isinstance(payload, dict) and payload.get("type") == "exceed_context_size_error"
    if not (explicit or match):
        return False, None
    reported = payload.get("n_prompt_tokens") if isinstance(payload, dict) else None
    if type(reported) is int and reported > 0:
        return True, reported
    return True, int(match[1]) if match else None


def request_vision(cancel_event=None, **kwargs):
    """Retry a confirmed context overflow once, preserving every image and its mapping."""
    options = dict(kwargs.get("options") or {})
    maximum = max(1024, int(config.VISION_MAX_CONTEXT_TOKENS))
    initial = min(maximum, max(1024, int(options.get("num_ctx") or config.VISION_CONTEXT_TOKENS)))
    key = (kwargs.get("model"), initial, maximum)
    with _cache_lock:
        context = _context_cache.get(key, initial)
    output = max(0, int(options.get("num_predict") or 0))
    for attempt in range(2):
        check_cancelled(cancel_event)
        try:
            response = request_chat(cancel_event=cancel_event, **{**kwargs, "options": {**options, "num_ctx": context}})
            check_cancelled(cancel_event)
            if context > initial:
                with _cache_lock:
                    _context_cache[key] = max(context, _context_cache.get(key, initial))
            return response
        except ollama.ResponseError as error:
            overflow, prompt_tokens = _overflow_prompt_tokens(error)
            if not overflow:
                raise
            check_cancelled(cancel_event)
            required = prompt_tokens + output + 256 if prompt_tokens is not None else context * 2
            larger = ((max(required, context + 1) + 1023) // 1024) * 1024
            if larger > maximum or attempt:
                count = str(prompt_tokens) if prompt_tokens is not None else "more"
                raise RuntimeError(
                    f"The vision request needs {count} input tokens plus room for the answer. "
                    f"Ollama could not fit it within the configured {maximum}-token maximum. "
                    "Increase CAPRI_VISION_MAX_CONTEXT_TOKENS if your hardware allows a larger context."
                ) from error
            logger.warning("Vision context overflow; retrying with %s tokens (previously %s)", larger, context)
            context = larger
