"""Stand-in endpoints that record what Telegraf actually puts on the wire.

Neither of these fakes the protocol loosely. The OneAgent stub parses the Dynatrace
metric line protocol and answers with the ingest response shape the plugin expects; the
OTLP receiver decodes the real protobuf with the generated OpenTelemetry classes. If a
config is wrong, these fail rather than quietly accepting bytes.
"""

from __future__ import annotations

import gzip
import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer

from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2


@dataclass
class Request:
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class DataPoint:
    name: str
    value: float
    attributes: dict[str, str] = field(default_factory=dict)
    resource_attributes: dict[str, str] = field(default_factory=dict)


class _Receiver:
    """Shared plumbing: bind an ephemeral port, serve in a thread, record requests."""

    def __init__(self, port: int = 0):
        self.port = port
        self.requests: list[Request] = []
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        handler = self._make_handler()
        self._server = HTTPServer(("127.0.0.1", self.port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def __enter__(self):
        self.url = self.start()
        return self

    def __exit__(self, *_exc):
        self.stop()

    def _make_handler(self):
        raise NotImplementedError

    @staticmethod
    def _read_body(handler: BaseHTTPRequestHandler) -> bytes:
        # Telegraf's OTLP output streams the body with Transfer-Encoding: chunked and no
        # Content-Length, which BaseHTTPRequestHandler does not decode for us. Reading
        # Content-Length alone silently yields an empty body that then parses as a valid
        # but empty protobuf message — a green test over no data at all.
        if handler.headers.get("Transfer-Encoding", "").lower() == "chunked":
            body = _read_chunked(handler.rfile)
        else:
            body = handler.rfile.read(int(handler.headers.get("Content-Length") or 0))

        if handler.headers.get("Content-Encoding", "").lower() == "gzip":
            body = gzip.decompress(body)
        return body


def _read_chunked(stream) -> bytes:
    """Decode an HTTP/1.1 chunked body: <hex length>CRLF <bytes> CRLF, ending at 0."""
    body = bytearray()
    while True:
        line = stream.readline().strip()
        if not line:
            break
        size = int(line.split(b";")[0], 16)
        if size == 0:
            stream.readline()  # the trailing CRLF after the terminating chunk
            break
        body.extend(stream.read(size))
        stream.readline()  # the CRLF that follows each chunk
    return bytes(body)


class OneAgentMetricsReceiver(_Receiver):
    """Stands in for the OneAgent local metric API at /metrics/ingest."""

    def __init__(self, port: int = 0):
        super().__init__(port)
        self.lines: list[str] = []

    def _make_handler(self):
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's naming
                body = receiver._read_body(self)
                receiver.requests.append(
                    Request(self.path, {k.lower(): v for k, v in self.headers.items()}, body)
                )
                text = body.decode("utf-8", errors="replace")
                lines = [line for line in text.splitlines() if line.strip()]
                receiver.lines.extend(lines)

                payload = json.dumps(
                    {"linesOk": len(lines), "linesInvalid": 0, "error": None}
                ).encode()
                self.send_response(202)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                pass

        return Handler

    def metrics(self) -> list[DataPoint]:
        """Parse the Dynatrace metric line protocol: key[,dims] value [timestamp]."""
        points: list[DataPoint] = []
        for line in self.lines:
            head, _, rest = line.partition(" ")
            name, _, dimension_text = head.partition(",")
            value_text = rest.split(" ")[0]
            # Values may be plain, or gauge,<n> / count,delta=<n>.
            if "," in value_text:
                value_text = value_text.split(",")[-1].split("=")[-1]
            try:
                value = float(value_text)
            except ValueError:
                continue
            attributes = {}
            for pair in dimension_text.split(","):
                if "=" in pair:
                    key, _, val = pair.partition("=")
                    attributes[key] = val.strip('"')
            points.append(DataPoint(name=name, value=value, attributes=attributes))
        return points


class OtlpMetricsReceiver(_Receiver):
    """Stands in for an OTLP/HTTP metrics endpoint, decoding the protobuf for real."""

    def __init__(self, port: int = 0):
        super().__init__(port)
        self.export_requests: list[metrics_service_pb2.ExportMetricsServiceRequest] = []

    def _make_handler(self):
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = receiver._read_body(self)
                receiver.requests.append(
                    Request(self.path, {k.lower(): v for k, v in self.headers.items()}, body)
                )

                export = metrics_service_pb2.ExportMetricsServiceRequest()
                # Fails loudly on anything that is not valid OTLP protobuf, which is the
                # whole point of decoding rather than counting bytes.
                export.ParseFromString(body)
                receiver.export_requests.append(export)

                payload = metrics_service_pb2.ExportMetricsServiceResponse().SerializeToString()
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                pass

        return Handler

    def metrics(self) -> list[DataPoint]:
        points: list[DataPoint] = []
        for export in self.export_requests:
            for resource_metrics in export.resource_metrics:
                resource_attributes = _attributes(resource_metrics.resource.attributes)
                for scope_metrics in resource_metrics.scope_metrics:
                    for metric in scope_metrics.metrics:
                        points.extend(_points(metric, resource_attributes))
        return points


def _points(metric, resource_attributes: dict[str, str]) -> list[DataPoint]:
    found: list[DataPoint] = []
    for kind in ("gauge", "sum"):
        if not metric.HasField(kind):
            continue
        for point in getattr(metric, kind).data_points:
            value = point.as_double if point.HasField("as_double") else float(point.as_int)
            found.append(
                DataPoint(
                    name=metric.name,
                    value=value,
                    attributes=_attributes(point.attributes),
                    resource_attributes=resource_attributes,
                )
            )
    return found


def _attributes(key_values) -> dict[str, str]:
    result = {}
    for attribute in key_values:
        value = attribute.value
        if value.HasField("string_value"):
            result[attribute.key] = value.string_value
        elif value.HasField("int_value"):
            result[attribute.key] = str(value.int_value)
        elif value.HasField("double_value"):
            result[attribute.key] = str(value.double_value)
        elif value.HasField("bool_value"):
            result[attribute.key] = str(value.bool_value)
    return result
