"""Langfuse setup: shared callback handler + trace helpers."""
import os
import time
import uuid
from typing import Optional

LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "http://192.168.48.76:3000")
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
LANGFUSE_ENABLED = os.getenv("LANGFUSE_ENABLED", "true").lower() == "true"

LANGFUSE_HANDLER = None
_langfuse_client = None
_active_trace = None

try:
    if LANGFUSE_ENABLED and LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY:
        from langfuse import Langfuse
        from langfuse.callback import CallbackHandler

        _langfuse_client = Langfuse(
            public_key=LANGFUSE_PUBLIC_KEY,
            secret_key=LANGFUSE_SECRET_KEY,
            host=LANGFUSE_HOST,
        )
        LANGFUSE_HANDLER = CallbackHandler(
            public_key=LANGFUSE_PUBLIC_KEY,
            secret_key=LANGFUSE_SECRET_KEY,
            host=LANGFUSE_HOST,
        )
        print("[Langfuse] enabled, host=" + LANGFUSE_HOST, flush=True)
    else:
        print("[Langfuse] disabled (missing keys or LANGFUSE_ENABLED=false)", flush=True)
except ImportError as e:
    print("[Langfuse] import failed: " + str(e), flush=True)
except Exception as e:
    print("[Langfuse] init failed: " + type(e).__name__ + ": " + str(e), flush=True)


def langfuse_client():
    """Return the global Langfuse client, or None if not enabled."""
    return _langfuse_client


def start_cycle_trace(name="inspection_cycle", session_id=None, metadata=None):
    """Start a new trace context. Returns trace handle, or None if disabled."""
    global _active_trace
    if _langfuse_client is None:
        return None
    try:
        trace = _langfuse_client.trace(
            name=name,
            session_id=session_id or str(uuid.uuid4())[:8],
            metadata=metadata or {},
        )
        _active_trace = trace
        return trace
    except Exception as e:
        print("[Langfuse] start_cycle_trace failed: " + str(e), flush=True)
        return None


def end_cycle_trace(trace, output=None, metadata=None):
    """End trace and force flush events to Langfuse server."""
    global _active_trace
    if trace is None or _langfuse_client is None:
        return
    try:
        if output is not None or metadata is not None:
            trace.update(output=output, metadata=metadata)
        _langfuse_client.flush()
    except Exception as e:
        print("[Langfuse] end_cycle_trace failed: " + str(e), flush=True)
    finally:
        _active_trace = None


def trace_span(name, agent, input_data=None, output_data=None,
               metadata=None, duration_ms=None):
    """Record a span attached to the current active trace."""
    if _active_trace is None or _langfuse_client is None:
        return
    try:
        meta = {"agent": agent, "duration_ms": duration_ms}
        if metadata:
            meta.update(metadata)
        _active_trace.span(
            name=name,
            input=input_data or {},
            output=output_data or {},
            metadata=meta,
        )
    except Exception as e:
        print("[Langfuse] trace_span failed: " + str(e), flush=True)


class TraceTimer:
    """Context manager: time a block and record it as a span."""

    def __init__(self, agent, name, input_data=None, metadata=None):
        self.agent = agent
        self.name = name
        self.input_data = input_data or {}
        self.metadata = metadata or {}
        self.output_data = {}
        self.t0 = None

    def __enter__(self):
        self.t0 = time.time()
        return self

    def set_output(self, output):
        self.output_data = output

    def __exit__(self, exc_type, exc_val, exc_tb):
        duration_ms = round((time.time() - self.t0) * 1000, 1)
        meta = dict(self.metadata)
        if exc_type is not None:
            meta["error"] = exc_type.__name__ + ": " + str(exc_val)
        trace_span(
            name=self.name,
            agent=self.agent,
            input_data=self.input_data,
            output_data=self.output_data,
            metadata=meta,
            duration_ms=duration_ms,
        )


def flush_langfuse():
    """Flush all buffered events. Call before main process exits."""
    if _langfuse_client is not None:
        try:
            _langfuse_client.flush()
        except Exception as e:
            print("[Langfuse] flush failed: " + str(e), flush=True)
