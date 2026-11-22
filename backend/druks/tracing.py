"""Traces over OTLP. One provider takes the spans of DBOS, FastAPI, SQLAlchemy, httpx,
and Druks. Each span crosses one privacy boundary on its way to the collector."""

from collections.abc import Sequence
from datetime import datetime
from urllib.parse import urlsplit

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy.engine import EngineTracer
from opentelemetry.metrics import get_meter
from opentelemetry.propagate import set_global_textmap
from opentelemetry.sdk import trace as sdk_trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import Decision, Sampler, SamplingResult
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.util.re import parse_env_headers
from opentelemetry.util.types import Attributes
from sqlalchemy.ext.asyncio import AsyncEngine

from druks.settings import Settings

# What a span may carry out of the process: identifiers, route templates, and the facts
# that Druks names.
_ATTRIBUTES = frozenset(
    {
        "druks.agent",
        "druks.app",
        "druks.cost_usd",
        "druks.failure_code",
        "druks.gate",
        "druks.harness",
        "druks.model",
        "druks.run_id",
        "druks.subject_id",
        "druks.subject_type",
        "druks.usage.cache_creation_tokens",
        "druks.usage.cached_input_tokens",
        "druks.usage.input_tokens",
        "druks.usage.output_tokens",
        "druks.usage.reasoning_tokens",
        "druks.usage.total_tokens",
        "dbos.application.version",
        "dbos.executor.id",
        "dbos.operation.type",
        "dbos.operation.workflow_id",
        "dbos.queue.name",
        "http.method",
        "http.route",
        "http.status_code",
        "net.peer.name",
        "net.peer.port",
        "db.name",
        "db.operation",
        "db.statement",
        "db.system",
    }
)

_provider: sdk_trace.TracerProvider | None = None


def init_tracing(settings: Settings) -> None:
    """Send traces to ``traces.endpoint`` with ``traces.headers``. Without an endpoint,
    Druks makes no provider and records no span."""
    global _provider
    if settings.traces.endpoint:
        # The host of the webhooks tells one installation's traces from another's.
        resource = {"service.name": "druks"}
        if host := urlsplit(settings.urls.webhook_base).hostname:
            resource["druks.host"] = host
        headers = parse_env_headers(settings.traces.headers.get_secret_value(), liberal=True)
        exporter = OTLPSpanExporter(endpoint=settings.traces.endpoint, headers=headers)
        # The provider sends the last batch when the process exits.
        _provider = sdk_trace.TracerProvider(sampler=_Sampler(), resource=Resource(resource))
        _provider.add_span_processor(BatchSpanProcessor(_PrivacyBoundary(exporter)))
        # DBOS makes its spans on the global provider.
        trace.set_tracer_provider(_provider)
        # The baggage of a caller can hold user data, so Druks reads none and sends none.
        set_global_textmap(TraceContextTextMapPropagator())
        HTTPXClientInstrumentor().instrument(tracer_provider=_provider)


def is_tracing() -> bool:
    return bool(_provider)


def instrument_app(app: FastAPI) -> None:
    """Trace each request that ``app`` serves, when Druks sends traces."""
    if _provider:
        # A streamed response sends one message for each event.
        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=_provider,
            excluded_urls="/health$",
            exclude_spans=["receive", "send"],
        )


def instrument_engine(engine: AsyncEngine) -> None:
    """Trace each statement that ``engine`` runs, without its parameters, when Druks
    sends traces."""
    if _provider:
        # SQLAlchemyInstrumentor instruments only the first engine of a process.
        EngineTracer(
            _provider.get_tracer(__name__),
            engine.sync_engine,
            get_meter(__name__).create_up_down_counter("db.client.connections.usage"),
        )


def record_gate_wait(run_id: str, gate: str, parked_at: datetime, *, is_answered: bool) -> None:
    """Trace a run's wait at ``gate``, from the park that the run recorded to now. A
    wait that ends without an answer, at a timeout or a cancel, ends in error."""
    span = trace.get_tracer(__name__).start_span(
        f"gate_wait {gate}",
        attributes={"druks.gate": gate, "druks.run_id": run_id},
        start_time=int(parked_at.timestamp() * 1_000_000_000),
    )
    if not is_answered:
        span.set_status(trace.StatusCode.ERROR)
    span.end()


class _Sampler(Sampler):
    """Drop a request whose caller sampled none of its trace, with every span under it:
    the dashboard marks each read that it repeats this way. Keep every other span. A
    run keeps its trace under any caller. A span takes no trace state from its caller."""

    def should_sample(
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: trace.SpanKind | None = None,
        attributes: Attributes = None,
        links: Sequence[trace.Link] | None = None,
        trace_state: trace.TraceState | None = None,
    ) -> SamplingResult:
        parent = trace.get_current_span(parent_context).get_span_context()
        is_unsampled = parent.is_valid and not parent.trace_flags.sampled
        # DBOS gives a run the context of its start as a remote parent.
        if is_unsampled and (kind is trace.SpanKind.SERVER or not parent.is_remote):
            return SamplingResult(Decision.DROP)
        return SamplingResult(Decision.RECORD_AND_SAMPLE, attributes)

    def get_description(self) -> str:
        return "DruksSampler"


class _PrivacyBoundary(SpanExporter):
    """Export only what the operator lets leave the process: identifiers, route
    templates, the facts that Druks names, and the type of each exception. No header,
    body, query value, URL, or error text leaves."""

    def __init__(self, exporter: SpanExporter) -> None:
        self._exporter = exporter

    def export(self, spans: Sequence[sdk_trace.ReadableSpan]) -> SpanExportResult:
        redacted = []
        for span in spans:
            attributes = {
                key: value for key, value in span.attributes.items() if key in _ATTRIBUTES
            }
            # A URL leaves as its host alone, which names the service that a call reached.
            if host := urlsplit(str(span.attributes.get("http.url", ""))).hostname:
                attributes["server.address"] = host
            redacted.append(
                sdk_trace.ReadableSpan(
                    name=span.name,
                    context=span.context,
                    parent=span.parent,
                    resource=span.resource,
                    attributes=attributes,
                    # The text of an exception can quote a request or a response.
                    events=[
                        sdk_trace.Event(
                            "exception",
                            {"exception.type": event.attributes["exception.type"]},
                            event.timestamp,
                        )
                        for event in span.events
                        if event.name == "exception"
                    ],
                    kind=span.kind,
                    # An instrumentation describes an error with that text too.
                    status=trace.Status(span.status.status_code),
                    start_time=span.start_time,
                    end_time=span.end_time,
                    instrumentation_scope=span.instrumentation_scope,
                )
            )
        return self._exporter.export(redacted)

    def shutdown(self) -> None:
        self._exporter.shutdown()
