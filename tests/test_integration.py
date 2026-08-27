"""End-to-end tests: the config this script generates, run by a real Telegraf binary.

These do not assert on the text of the generated config. They render it with the script,
hand it to Telegraf, and check what arrives at the other end — which is the only thing
that proves the integration works.

Set TELEGRAF_BIN to the telegraf executable. tests/conftest.py will download one into a
cache directory if it is not set.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from datetime import datetime
from pathlib import Path

import pytest

from tests.receivers import OneAgentMetricsReceiver, OtlpMetricsReceiver

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "install-telegraf-dynatrace.sh"

# The OneAgent metric API's real port. outputs.dynatrace only waives the api_token
# requirement for the localhost form of this URL, so the stub binds the real port and the
# tests use the installer's untouched default rather than a lookalike.
ONEAGENT_PORT = 14499


def render(tmp_path: Path, shell_bin: str, **settings) -> tuple[Path, Path]:
    """Render the wrapper and config with the script itself, into tmp_path.

    The script is sourced in library mode so the tests exercise the same rendering code
    the installer runs, rather than a copy of it that could drift.
    """
    managed_dir = tmp_path.as_posix()
    assignments = "\n".join(
        f'{key}={_quote(value)}' for key, value in settings.items() if key != "EXTRA_TAGS"
    )
    tags = settings.get("EXTRA_TAGS", [])
    if tags:
        assignments += "\nEXTRA_TAGS=(" + " ".join(_quote(tag) for tag in tags) + ")"

    script = textwrap.dedent(
        f"""
        export TELEGRAF_DT_LIB_ONLY=1
        export MANAGED_DIR={_quote(managed_dir)}
        export TELEGRAF_CONF_DIR={_quote(managed_dir)}
        export SHELL_BIN={_quote(shell_bin)}
        # shellcheck disable=SC1090
        . {_quote(SCRIPT.as_posix())}
        {assignments}
        render_command_wrapper > {_quote(managed_dir)}/wrapper.out
        render_config          > {_quote(managed_dir)}/config.out
        """
    )
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, cwd=REPO_ROOT, check=False
    )
    if result.returncode != 0:
        raise AssertionError(f"rendering failed:\n{result.stdout}\n{result.stderr}")

    config_name = settings.get("CONFIG_NAME", "dynatrace-exec")
    wrapper = tmp_path / f"{config_name}.sh"
    wrapper.write_text((tmp_path / "wrapper.out").read_text(), newline="\n")
    config = tmp_path / f"{config_name}.conf"
    config.write_text((tmp_path / "config.out").read_text(), newline="\n")
    return wrapper, config


def _quote(value) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"


def run_telegraf_once(telegraf_bin: str, config: Path, env_extra: dict | None = None) -> str:
    """Gather once and write to the outputs, the way the installed service would."""
    import os

    env = dict(os.environ)
    env.update(env_extra or {})
    result = subprocess.run(
        [telegraf_bin, "--once", "--config", str(config)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    output = result.stdout + result.stderr
    if result.returncode != 0:
        raise AssertionError(f"telegraf --once failed ({result.returncode}):\n{output}")
    return output


class TestOneAgentOutput:
    def test_command_output_reaches_the_oneagent_metric_endpoint(self, tmp_path, telegraf_bin, shell_bin):
        with OneAgentMetricsReceiver(port=ONEAGENT_PORT) as receiver:
            _, config = render(
                tmp_path,
                shell_bin,
                COMMAND="echo 42.5",
                METRIC_NAME="unit_test_metric",
                OUTPUT_MODE="oneagent",
                METRIC_PREFIX="telegraf",
                INTERVAL="1s",
            )
            run_telegraf_once(telegraf_bin, config)

            points = receiver.metrics()

        assert points, f"nothing arrived. Raw lines: {receiver.lines}"
        assert any(point.value == pytest.approx(42.5) for point in points), receiver.lines
        # The Dynatrace output builds the key as <prefix>.<measurement>.<field>, so the
        # value parser's field arrives as a trailing ".value". Pinned exactly, because
        # this is the string someone has to type into a DQL query.
        assert [point.name for point in points] == ["telegraf.unit_test_metric.value"]

    def test_the_default_oneagent_path_is_used(self, tmp_path, telegraf_bin, shell_bin):
        with OneAgentMetricsReceiver(port=ONEAGENT_PORT) as receiver:
            _, config = render(
                tmp_path,
                shell_bin,
                COMMAND="echo 1",
                METRIC_NAME="unit_test_metric",
                OUTPUT_MODE="oneagent",
                INTERVAL="1s",
            )
            run_telegraf_once(telegraf_bin, config)

        assert receiver.requests
        assert receiver.requests[0].path == "/metrics/ingest"

    def test_extra_tags_become_dimensions(self, tmp_path, telegraf_bin, shell_bin):
        with OneAgentMetricsReceiver(port=ONEAGENT_PORT) as receiver:
            _, config = render(
                tmp_path,
                shell_bin,
                COMMAND="echo 7",
                METRIC_NAME="unit_test_metric",
                OUTPUT_MODE="oneagent",
                INTERVAL="1s",
                EXTRA_TAGS=["environment=test", "team=platform"],
            )
            run_telegraf_once(telegraf_bin, config)
            points = receiver.metrics()

        assert points, receiver.lines
        attributes = points[0].attributes
        assert attributes.get("environment") == "test", receiver.lines
        assert attributes.get("team") == "platform", receiver.lines

    def test_multiline_command_with_a_pipe_runs_through_the_shell(self, tmp_path, telegraf_bin, shell_bin):
        # A pipe only works because the command goes into a wrapper script; Telegraf's exec
        # input does not run its command through a shell.
        with OneAgentMetricsReceiver(port=ONEAGENT_PORT) as receiver:
            _, config = render(
                tmp_path,
                shell_bin,
                COMMAND="echo '1 2 3' | tr ' ' '\\n' | wc -l",
                METRIC_NAME="unit_test_metric",
                OUTPUT_MODE="oneagent",
                INTERVAL="1s",
            )
            run_telegraf_once(telegraf_bin, config)
            points = receiver.metrics()

        assert points, receiver.lines
        assert points[0].value == pytest.approx(3.0), receiver.lines


class TestOtlpOutput:
    def test_command_output_arrives_as_valid_otlp_protobuf(self, tmp_path, telegraf_bin, shell_bin):
        with OtlpMetricsReceiver() as receiver:
            _, config = render(
                tmp_path,
                shell_bin,
                COMMAND="echo 3.5",
                METRIC_NAME="unit_test_otlp",
                OUTPUT_MODE="otlp",
                OTLP_ENDPOINT=f"{receiver.url}/api/v2/otlp/v1/metrics",
                OTLP_TOKEN="dt0c01.TESTTOKEN",
                INTERVAL="1s",
            )
            run_telegraf_once(telegraf_bin, config, {"DT_API_TOKEN": "dt0c01.TESTTOKEN"})
            points = receiver.metrics()

        assert receiver.export_requests, "no OTLP export request arrived"
        assert points, "the export request decoded but carried no data points"
        # Exactly this name, not "unit_test_otlp_value": the generated rename processor
        # is what collapses Telegraf's <measurement>_<field> naming.
        assert [point.name for point in points] == ["unit_test_otlp"]
        assert any(point.value == pytest.approx(3.5) for point in points)

    def test_the_api_token_is_sent_as_a_dynatrace_authorization_header(
        self, tmp_path, telegraf_bin, shell_bin
    ):
        with OtlpMetricsReceiver() as receiver:
            _, config = render(
                tmp_path,
                shell_bin,
                COMMAND="echo 1",
                METRIC_NAME="unit_test_otlp",
                OUTPUT_MODE="otlp",
                OTLP_ENDPOINT=f"{receiver.url}/api/v2/otlp/v1/metrics",
                OTLP_TOKEN="dt0c01.TESTTOKEN",
                INTERVAL="1s",
            )
            run_telegraf_once(telegraf_bin, config, {"DT_API_TOKEN": "dt0c01.TESTTOKEN"})

        assert receiver.requests
        headers = receiver.requests[0].headers
        assert headers.get("authorization") == "Api-Token dt0c01.TESTTOKEN", headers

    def test_the_endpoint_path_is_used_verbatim(self, tmp_path, telegraf_bin, shell_bin):
        # Dynatrace expects the full signal path. If Telegraf appended its own suffix the
        # requests would 404 against a real tenant, so pin the behaviour here.
        with OtlpMetricsReceiver() as receiver:
            _, config = render(
                tmp_path,
                shell_bin,
                COMMAND="echo 1",
                METRIC_NAME="unit_test_otlp",
                OUTPUT_MODE="otlp",
                OTLP_ENDPOINT=f"{receiver.url}/api/v2/otlp/v1/metrics",
                OTLP_TOKEN="dt0c01.TESTTOKEN",
                INTERVAL="1s",
            )
            run_telegraf_once(telegraf_bin, config, {"DT_API_TOKEN": "dt0c01.TESTTOKEN"})

        assert receiver.requests[0].path == "/api/v2/otlp/v1/metrics"

    def test_the_payload_is_protobuf_and_gzipped(self, tmp_path, telegraf_bin, shell_bin):
        with OtlpMetricsReceiver() as receiver:
            _, config = render(
                tmp_path,
                shell_bin,
                COMMAND="echo 1",
                METRIC_NAME="unit_test_otlp",
                OUTPUT_MODE="otlp",
                OTLP_ENDPOINT=f"{receiver.url}/api/v2/otlp/v1/metrics",
                OTLP_TOKEN="dt0c01.TESTTOKEN",
                INTERVAL="1s",
            )
            run_telegraf_once(telegraf_bin, config, {"DT_API_TOKEN": "dt0c01.TESTTOKEN"})

        headers = receiver.requests[0].headers
        assert "x-protobuf" in headers.get("content-type", ""), headers
        assert headers.get("content-encoding") == "gzip", headers


def test_the_generated_config_is_accepted_by_telegraf(tmp_path, telegraf_bin, shell_bin):
    _, config = render(
        tmp_path,
        shell_bin,
        COMMAND="echo 1",
        METRIC_NAME="unit_test_metric",
        OUTPUT_MODE="oneagent",
        INTERVAL="1s",
    )
    result = subprocess.run(
        [telegraf_bin, "--test", "--config", str(config)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "unit_test_metric" in result.stdout, result.stdout + result.stderr


class TestFileOutput:
    """File mode: the command's text on disk, shaped for OneAgent to pick up."""

    def render_and_run(self, tmp_path, shell_bin, telegraf_bin, command, **overrides):
        log_path = tmp_path / "capture.log"
        settings = {
            "COMMAND": command,
            "METRIC_NAME": "check_output",
            "OUTPUT_MODE": "file",
            "DATA_FORMAT": "lines",
            "LOG_PATH": log_path.as_posix(),
            "INTERVAL": "1s",
        }
        settings.update(overrides)
        _, config = render(tmp_path, shell_bin, **settings)
        run_telegraf_once(telegraf_bin, config)
        if not log_path.exists():
            raise AssertionError(f"telegraf wrote no log file at {log_path}")
        return [
            json.loads(line)
            for line in log_path.read_text().splitlines()
            if line.strip()
        ]

    def test_each_output_line_becomes_its_own_log_record(self, tmp_path, telegraf_bin, shell_bin):
        # The value parser hands over the whole stdout as one string; grok is what makes
        # this one record per line, which is the difference between a readable log stream
        # and a single blob with embedded newlines.
        records = self.render_and_run(
            tmp_path, shell_bin, telegraf_bin,
            "printf '%s\n' 'nginx: worker failed' 'disk /var at 91 pct' 'backup ok'",
        )

        assert [record["content"] for record in records] == [
            "nginx: worker failed",
            "disk /var at 91 pct",
            "backup ok",
        ]

    def test_records_use_the_field_names_dynatrace_reads(self, tmp_path, telegraf_bin, shell_bin):
        records = self.render_and_run(
            tmp_path, shell_bin, telegraf_bin, "echo hello", EXTRA_TAGS=["env=prod"]
        )
        record = records[0]

        # Top level, not nested under "fields": Dynatrace maps content to the log message
        # and timestamp to the log timestamp only if they are at the root.
        assert record["content"] == "hello"
        assert record["log.source"] == "check_output"
        assert record["env"] == "prod"
        assert "host.name" in record
        assert "fields" not in record

    def test_the_timestamp_is_parseable_iso_8601(self, tmp_path, telegraf_bin, shell_bin):
        records = self.render_and_run(tmp_path, shell_bin, telegraf_bin, "echo hello")

        stamp = records[0]["timestamp"]
        parsed = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%f%z")
        assert parsed.year >= 2024, stamp

    def test_rotation_is_configured_so_the_disk_cannot_fill(self, tmp_path, telegraf_bin, shell_bin):
        _, config = render(
            tmp_path, shell_bin,
            COMMAND="echo hello", METRIC_NAME="check_output", OUTPUT_MODE="file",
            DATA_FORMAT="lines", LOG_PATH=(tmp_path / "capture.log").as_posix(),
            LOG_ROTATE_SIZE="5MB", LOG_KEEP=3,
        )
        text = config.read_text()
        assert 'rotation_max_size = "5MB"' in text
        assert "rotation_max_archives = 3" in text
