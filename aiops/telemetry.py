"""OpenTelemetry tracing for every investigation, agent step, model call and tool call.

Spans go to a JSON-lines file by default (``var/traces.jsonl``), optionally to the console, and to any OTLP
collector such as Jaeger when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set (for example ``http://localhost:4318``).
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Sequence
from pathlib import Path

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

_configured = False
_lock = threading.Lock()


class JsonLinesSpanExporter(SpanExporter):
    """Appends one compact JSON object per finished span."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            for s in spans:
                ctx = s.get_span_context()
                assert ctx is not None
                fh.write(
                    json.dumps(
                        {
                            "trace_id": f"{ctx.trace_id:032x}",
                            "span_id": f"{ctx.span_id:016x}",
                            "parent_id": f"{s.parent.span_id:016x}" if s.parent else None,
                            "name": s.name,
                            "start_ns": s.start_time,
                            "end_ns": s.end_time,
                            "status": s.status.status_code.name,
                            "attributes": dict(s.attributes or {}),
                        },
                        default=str,
                    )
                    + "\n"
                )
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None


def setup_tracing(path: Path | None = None, console: bool = False) -> None:
    """Install the tracer provider once per process. Safe to call repeatedly."""
    global _configured
    with _lock:
        if _configured:
            return
        provider = TracerProvider(resource=Resource.create({"service.name": "aiops-investigator"}))
        trace_file = path or Path(os.environ.get("AIOPS_TRACE_FILE", "var/traces.jsonl"))
        provider.add_span_processor(SimpleSpanProcessor(JsonLinesSpanExporter(trace_file)))
        if console or os.environ.get("AIOPS_TRACE_CONSOLE") == "1":
            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        _configured = True


def tracer() -> trace.Tracer:
    return trace.get_tracer("aiops")


def shutdown() -> None:
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.force_flush()
