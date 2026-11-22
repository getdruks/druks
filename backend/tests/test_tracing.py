import threading
from collections.abc import AsyncIterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from operator import attrgetter

import httpx
import pytest
import requests
from conftest import capture_spans, reset_tracing
from druks import tracing
from druks.api.routes import router as health_router
from druks.database import create_async_engine_from_url
from druks.durable.live import EventStream
from druks.settings import get_settings
from druks.testing import TEST_DATABASE_URL, asgi_client, make_settings
from druks.tracing import init_tracing
from fastapi import FastAPI, Request
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from sqlalchemy import text

COLLECTOR = "https://collector.example.com/v1/traces"
TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


@pytest.fixture
def collector(monkeypatch):
    """The exports that reach the operator's collector."""
    posts = []

    def request(self, method, url, headers=None, data=None, timeout=None, allow_redirects=True):
        posts.append({"method": method, "url": url, "headers": headers, "body": data})
        response = requests.Response()
        response.status_code = 200
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    return posts


@pytest.fixture
def fresh_process():
    """Leave tracing after the test as a new process finds it."""
    yield
    reset_tracing()


@pytest.fixture
def spans():
    yield capture_spans(get_settings())
    reset_tracing()


@pytest.fixture
def outside_service():
    """A service on this host, so a call leaves through the transport of httpx. It keeps
    the headers of each call that it takes."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.server.calls.append(self.headers)
            self.rfile.read(int(self.headers["Content-Length"]))
            body = b"secret-response"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.calls = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def test_traces_are_off_until_traces_endpoint_is_set(monkeypatch, tmp_path):
    # Only druks.toml turns traces on.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.internal:4318")

    init_tracing(make_settings(tmp_path))

    assert not tracing.is_tracing()
    assert not trace.get_tracer(__name__).start_span("probe").is_recording()


def test_traces_leave_as_protobuf_over_http_for_the_endpoint_that_is_set(
    fresh_process, collector, monkeypatch, tmp_path
):
    # Druks sends protobuf whatever the protocol says.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/json")
    settings = make_settings(
        tmp_path,
        traces={"endpoint": COLLECTOR, "headers": "x-api-key=collector-key"},
        urls={"webhook_host": "hooks.example.com"},
    )

    init_tracing(settings)
    trace.get_tracer(__name__).start_span("probe").end()
    tracing._provider.force_flush()

    [post] = collector
    assert (post["method"], post["url"]) == ("POST", COLLECTOR)
    assert post["headers"]["content-type"] == "application/x-protobuf"
    assert post["headers"]["x-api-key"] == "collector-key"
    [resource_spans] = ExportTraceServiceRequest.FromString(post["body"]).resource_spans
    resource = {item.key: item.value.string_value for item in resource_spans.resource.attributes}
    assert resource == {"service.name": "druks", "druks.host": "hooks.example.com"}
    [scope_spans] = resource_spans.scope_spans
    assert [span.name for span in scope_spans.spans] == ["probe"]


@pytest.fixture
async def probe(spans, outside_service):
    """An app that an installation that sends traces serves: it reads a note, and it
    writes one through the database and an outside service, then fails."""
    # database.py traces the statements of the engine.
    engine = create_async_engine_from_url(TEST_DATABASE_URL)
    probe = FastAPI()
    probe.include_router(health_router)

    @probe.get("/notes/{note}")
    async def get_note(note: str) -> None:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT :note"), {"note": note})

    @probe.get("/notes/{note}/stream")
    async def stream_note(note: str) -> EventStream:
        async def poll() -> AsyncIterator[str]:
            await get_note(note)
            yield f"data: {note}\n\n"

        return EventStream(poll())

    @probe.post("/notes/{note}")
    async def update_note(note: str, request: Request) -> None:
        await get_note(note)
        async with httpx.AsyncClient() as client:
            await client.post(
                f"http://user:secret-password@127.0.0.1:{outside_service.server_port}"
                "/v1/secret-path?token=secret-query",
                headers={"Authorization": "Bearer secret-bearer"},
                content=await request.body(),
            )
        raise RuntimeError("the service answered password=secret-error")

    tracing.instrument_app(probe)
    yield probe
    FastAPIInstrumentor.uninstrument_app(probe)
    await engine.dispose()


async def test_a_span_leaves_with_identifiers_route_templates_and_exception_types_only(
    spans, outside_service, probe
):
    async with asgi_client(probe) as client:
        with pytest.raises(RuntimeError):
            await client.post(
                "/notes/secret-note?token=secret-query",
                headers={
                    "Authorization": "Bearer secret-bearer",
                    "Cookie": "session=secret-cookie",
                    # A caller that traces its request, with a state and a baggage.
                    "traceparent": TRACEPARENT,
                    "tracestate": "vendor=secret-state",
                    "baggage": "user.email=secret-baggage",
                },
                content=b"secret-body",
            )

    exported = spans()
    assert sorted(span.name for span in exported) == ["POST", "POST /notes/{note}", "SELECT"]
    call, request, statement = sorted(exported, key=attrgetter("name"))
    assert call.attributes["server.address"] == "127.0.0.1"
    assert statement.attributes["db.statement"] == "SELECT %(note)s"
    assert [dict(event.attributes) for event in request.events] == [
        {"exception.type": "RuntimeError"}
    ]
    # The collector gets every name, resource, attribute, event, link, and status here.
    assert b"secret-" not in encode_spans(exported).SerializeToString()
    # The service that Druks calls gets the trace context, and nothing that the caller set.
    [received] = outside_service.calls
    assert {name.lower() for name in received} & {"traceparent", "tracestate", "baggage"} == {
        "traceparent"
    }


async def test_a_read_leaves_a_trace_unless_its_caller_samples_none(spans, probe):
    async with asgi_client(probe) as client:
        await client.get("/health")
        await client.get("/notes/7")
        # The dashboard marks a read that it repeats.
        await client.get("/notes/7", headers={"traceparent": f"{TRACEPARENT[:-2]}00"})

    assert sorted(span.name for span in spans()) == ["GET /notes/{note}", "SELECT"]


async def test_a_stream_sends_the_span_of_its_request_and_none_of_its_polls(spans, probe):
    async with asgi_client(probe) as client:
        response = await client.get("/notes/7/stream")

    assert response.text == "data: 7\n\n"
    assert [span.name for span in spans()] == ["GET /notes/{note}/stream"]
