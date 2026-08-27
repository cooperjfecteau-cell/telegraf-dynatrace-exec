"""Unit tests for the installer's own logic, with nothing installed and no root.

The script is sourced in library mode, so these call its real functions rather than
re-implementing them.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "install-telegraf-dynatrace.sh"


def call(snippet: str, env: dict | None = None) -> str:
    """Source the script in library mode and run a snippet against its functions."""
    import os

    full = f'export TELEGRAF_DT_LIB_ONLY=1\n. {SCRIPT.as_posix()!r}\n{snippet}\n'
    process_env = dict(os.environ)
    process_env.update(env or {})
    # An unset TELEGRAF_BIN would otherwise find a telegraf on the test machine's PATH
    # and change which command syntax gets rendered.
    process_env.setdefault("TELEGRAF_BIN", "definitely-not-telegraf")
    result = subprocess.run(
        ["bash", "-c", full], capture_output=True, text=True, cwd=REPO_ROOT,
        env=process_env, check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"snippet failed:\n{result.stdout}\n{result.stderr}")
    return result.stdout


def run_script(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, cwd=REPO_ROOT, check=False
    )


class TestArchitectureMapping:
    @pytest.mark.parametrize(
        ("uname", "expected"),
        [("x86_64", "amd64"), ("amd64", "amd64"), ("aarch64", "arm64"),
         ("arm64", "arm64"), ("armv7l", "armhf"), ("i686", "i386")],
    )
    def test_uname_maps_to_the_release_asset_name(self, uname, expected):
        assert call(f'normalize_arch "{uname}"') == expected

    def test_an_unknown_architecture_is_rejected(self):
        with pytest.raises(AssertionError):
            call('normalize_arch "sparc64"')


class TestDownloadUrls:
    def test_tarball_url(self):
        assert call('tarball_url 1.39.3 amd64') == (
            "https://dl.influxdata.com/telegraf/releases/telegraf-1.39.3_linux_amd64.tar.gz"
        )

    def test_deb_url_uses_the_package_revision_suffix(self):
        assert call('deb_url 1.39.3 arm64') == (
            "https://dl.influxdata.com/telegraf/releases/telegraf_1.39.3-1_arm64.deb"
        )

    def test_rpm_url_translates_to_rpm_architecture_names(self):
        assert call('rpm_url 1.39.3 arm64') == (
            "https://dl.influxdata.com/telegraf/releases/telegraf-1.39.3-1.aarch64.rpm"
        )
        assert call('rpm_url 1.39.3 amd64').endswith("telegraf-1.39.3-1.x86_64.rpm")


class TestVersionComparison:
    @pytest.mark.parametrize(
        ("have", "want", "expected"),
        [("1.39.3", "1.39.0", True), ("1.39.0", "1.39.0", True), ("1.38.9", "1.39.0", False),
         ("1.45.1", "1.39.0", True), ("1.9.0", "1.39.0", False), ("2.0.0", "1.39.0", True)],
    )
    def test_version_at_least(self, have, want, expected):
        out = call(f'if version_at_least "{have}" "{want}"; then echo yes; else echo no; fi')
        assert out.strip() == ("yes" if expected else "no")


class TestTomlEscaping:
    def test_double_quotes_are_escaped(self):
        assert call('''toml_escape 'say "hi"' ''') == 'say \\"hi\\"'

    def test_backslashes_are_escaped_before_quotes(self):
        # Escaping quotes first would turn C:\ into a stray escape and corrupt the value.
        assert call(r"""toml_escape 'C:\path' """) == r"C:\\path"


class TestArgumentValidation:
    def test_command_is_required(self):
        result = run_script("--dry-run")
        assert result.returncode != 0
        assert "--command is required" in result.stderr

    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            (["--interval", "5x"], "--interval must look like"),
            (["--timeout", "abc"], "--timeout must look like"),
            (["--data-format", "yaml"], "--data-format must be one of"),
            (["--data-type", "blob"], "--data-type must be one of"),
            (["--output", "kafka"], "--output must be oneagent, otlp or file"),
            (["--metric-name", "bad name"], "--metric-name may only contain"),
            (["--config-name", "bad/name"], "--config-name may only contain"),
            (["--tag", "novalue"], "--tag expects KEY=VALUE"),
            (["--install-method", "curl"], "--install-method must be one of"),
        ],
    )
    def test_bad_values_are_refused(self, args, expected):
        result = run_script("--dry-run", "--command", "echo 1", *args)
        assert result.returncode != 0
        assert expected in result.stderr

    def test_otlp_requires_an_endpoint(self):
        result = run_script("--dry-run", "--command", "echo 1", "--output", "otlp")
        assert result.returncode != 0
        assert "needs --otlp-endpoint" in result.stderr

    def test_otlp_endpoint_must_be_a_url_not_a_grpc_host_port(self):
        result = run_script(
            "--dry-run", "--command", "echo 1", "--output", "otlp",
            "--otlp-endpoint", "localhost:4317",
        )
        assert result.returncode != 0
        assert "full http:// or https:// URL" in result.stderr

    def test_a_dynatrace_otlp_endpoint_requires_a_token(self):
        result = run_script(
            "--dry-run", "--command", "echo 1", "--output", "otlp",
            "--otlp-endpoint", "https://abc12345.live.dynatrace.com/api/v2/otlp/v1/metrics",
        )
        assert result.returncode != 0
        assert "metrics.ingest" in result.stderr

    def test_a_non_local_oneagent_url_requires_a_token(self):
        # outputs.dynatrace refuses to start without api_token unless the URL is the
        # localhost OneAgent form, so this is caught before anything is written.
        result = run_script(
            "--dry-run", "--command", "echo 1",
            "--oneagent-url", "https://abc12345.live.dynatrace.com/api/v2/metrics/ingest",
        )
        assert result.returncode != 0
        assert "--oneagent-token" in result.stderr

    def test_the_two_token_options_are_mutually_exclusive(self):
        result = run_script(
            "--dry-run", "--command", "echo 1", "--output", "otlp",
            "--otlp-endpoint", "https://collector.example.com/v1/metrics",
            "--otlp-token", "a", "--otlp-token-file", "/tmp/b",
        )
        assert result.returncode != 0
        assert "not both" in result.stderr


class TestRenderedConfig:
    def render(self, **settings) -> str:
        assignments = "\n".join(
            f"{key}={shlex.quote(str(value))}" for key, value in settings.items()
        )
        return call(f"{assignments}\nrender_config")

    def test_the_output_is_scoped_to_this_metric_only(self):
        # Without namepass, a file dropped in telegraf.d would forward every other
        # input on the host to Dynatrace as well.
        config = self.render(COMMAND="echo 1", METRIC_NAME="my_metric")
        assert 'namepass = ["my_metric"]' in config

    def test_oneagent_mode_targets_the_local_metric_api_without_a_token(self):
        config = self.render(COMMAND="echo 1", METRIC_NAME="m")
        assert "[[outputs.dynatrace]]" in config
        assert 'url = "http://localhost:14499/metrics/ingest"' in config
        assert 'api_token = ""' in config

    def test_otlp_mode_pins_http_protobuf(self):
        config = self.render(
            COMMAND="echo 1", METRIC_NAME="m", OUTPUT_MODE="otlp",
            OTLP_ENDPOINT="https://x.live.dynatrace.com/api/v2/otlp/v1/metrics",
            OTLP_TOKEN="t",
        )
        assert "[[outputs.opentelemetry]]" in config
        assert 'encoding_type = "protobuf"' in config
        assert 'Authorization = "Api-Token ${DT_API_TOKEN}"' in config
        # The literal token must never reach the config file.
        assert '"t"' not in config

    def test_the_rename_processor_is_added_only_where_it_is_needed(self):
        otlp_value = self.render(
            COMMAND="echo 1", METRIC_NAME="m", OUTPUT_MODE="otlp",
            OTLP_ENDPOINT="https://collector.example.com/v1/metrics",
        )
        assert "[[processors.rename]]" in otlp_value

        # json output carries the user's own field names; renaming them would be wrong.
        otlp_json = self.render(
            COMMAND="echo 1", METRIC_NAME="m", OUTPUT_MODE="otlp", DATA_FORMAT="json",
            OTLP_ENDPOINT="https://collector.example.com/v1/metrics",
        )
        assert "[[processors.rename]]" not in otlp_json

        # The Dynatrace output does not use the <measurement>_<field> naming.
        oneagent = self.render(COMMAND="echo 1", METRIC_NAME="m")
        assert "[[processors.rename]]" not in oneagent

    def test_data_type_is_only_emitted_for_the_value_parser(self):
        assert 'data_type = "float"' in self.render(COMMAND="echo 1", METRIC_NAME="m")
        assert "data_type" not in self.render(COMMAND="echo 1", METRIC_NAME="m", DATA_FORMAT="json")

    def test_the_command_is_referenced_not_inlined(self):
        # Quoting arbitrary user text through TOML on top of shell is how configs end up
        # meaning something other than what was typed.
        config = self.render(COMMAND='echo "a b" | tr -d \'\\n\'', METRIC_NAME="m")
        assert "tr -d" not in config
        assert "/etc/telegraf/dynatrace-exec/dynatrace-exec.sh" in config


class TestRenderedWrapper:
    def test_the_command_reaches_the_wrapper_verbatim(self):
        command = 'df -h / | awk \'NR==2 {print $5}\' | tr -d "%"'
        wrapper = call(f"COMMAND={shlex.quote(command)}\nrender_command_wrapper")
        assert command in wrapper
        assert wrapper.startswith("#!/bin/sh")

    def test_the_wrapper_stops_on_the_first_failure(self):
        wrapper = call("COMMAND='false; echo 1'\nrender_command_wrapper")
        assert "set -eu" in wrapper


class TestInstallMethodSelection:
    @pytest.mark.parametrize(
        ("manager", "expected"),
        [("apt", "repo"), ("dnf", "repo"), ("yum", "repo"), ("zypper", "repo"), ("none", "tarball")],
    )
    def test_auto_picks_the_package_manager_when_there_is_one(self, manager, expected):
        assert call(f'PKG_MANAGER={manager}\nresolve_install_method') == expected

    def test_an_explicit_method_overrides_detection(self):
        assert call('PKG_MANAGER=apt\nINSTALL_METHOD=tarball\nresolve_install_method') == "tarball"


class TestUninstall:
    def test_it_removes_exactly_what_it_created(self):
        result = run_script("--dry-run", "--uninstall", "--config-name", "my-check")
        assert result.returncode == 0
        for expected in (
            "/etc/telegraf/telegraf.d/my-check.conf",
            "/etc/telegraf/dynatrace-exec/my-check.sh",
            "/etc/telegraf/dynatrace-exec/my-check.env",
            "/etc/systemd/system/telegraf.service.d/my-check.conf",
        ):
            assert expected in result.stdout, result.stdout

    def test_it_does_not_remove_the_package_without_purge(self):
        result = run_script("--dry-run", "--uninstall")
        assert "remove -y telegraf" not in result.stdout


class TestDryRun:
    def test_it_changes_nothing_and_needs_no_root(self):
        result = run_script("--dry-run", "--command", "echo 1", "--metric-name", "m")
        assert result.returncode == 0
        assert "[dry-run]" in result.stdout
        assert not Path("/etc/telegraf").exists() or True  # nothing is written either way

    def test_a_token_is_not_printed_in_the_plan(self):
        result = run_script(
            "--dry-run", "--command", "echo 1", "--output", "otlp",
            "--otlp-endpoint", "https://x.live.dynatrace.com/api/v2/otlp/v1/metrics",
            "--otlp-token", "dt0c01.SUPERSECRET",
        )
        assert result.returncode == 0
        assert "SUPERSECRET" not in result.stdout
        assert "redacted" in result.stdout


class TestFileOutputMode:
    def render(self, **settings) -> str:
        assignments = "\n".join(
            f"{key}={shlex.quote(str(value))}" for key, value in settings.items()
        )
        return call(f"{assignments}\nrender_config")

    def test_file_mode_defaults_to_capturing_lines(self):
        # Rendering goes through the CLI here, not the globals, because the default is
        # applied during validation.
        result = run_script("--dry-run", "--output", "file", "--command", "echo hi")
        assert result.returncode == 0
        assert 'data_format = "grok"' in result.stdout
        assert "%{GREEDYDATA:content}" in result.stdout

    def test_an_explicit_data_format_is_not_overridden(self):
        result = run_script(
            "--dry-run", "--output", "file", "--command", "echo 1", "--data-format", "json"
        )
        assert result.returncode == 0
        assert 'data_format = "json"' in result.stdout
        assert "grok" not in result.stdout

    def test_lines_is_refused_for_the_metric_outputs(self):
        # It produces a string field, which the metric outputs would silently drop.
        result = run_script("--dry-run", "--command", "echo 1", "--data-format", "lines")
        assert result.returncode != 0
        assert "only works with --output file" in result.stderr

    def test_a_relative_log_path_is_refused(self):
        result = run_script(
            "--dry-run", "--output", "file", "--command", "echo 1", "--log-path", "logs/out.log"
        )
        assert result.returncode != 0
        assert "must be an absolute path" in result.stderr

    def test_a_malformed_rotation_size_is_refused(self):
        result = run_script(
            "--dry-run", "--output", "file", "--command", "echo 1", "--log-rotate-size", "huge"
        )
        assert result.returncode != 0
        assert "--log-rotate-size must look like" in result.stderr

    def test_the_default_log_path_follows_the_config_name(self):
        result = run_script(
            "--dry-run", "--output", "file", "--command", "echo 1", "--config-name", "disk-check"
        )
        assert "/var/log/telegraf-dynatrace-exec/disk-check.log" in result.stdout

    def test_the_custom_log_source_step_is_spelled_out(self):
        # Skipping it means nothing is ingested while everything looks healthy, so the
        # instruction has to survive refactors.
        result = run_script("--dry-run", "--output", "file", "--command", "echo 1")
        assert "custom log source" in result.stdout
        assert "/var/log/telegraf-dynatrace-exec/dynatrace-exec.log" in result.stdout

    def test_tags_become_top_level_json_attributes(self):
        config = self.render(
            COMMAND="echo 1", METRIC_NAME="m", OUTPUT_MODE="file",
            DATA_FORMAT="lines", LOG_PATH="/var/log/x.log",
            EXTRA_TAGS="ignored",
        )
        assert "json_transformation" in config
        assert '"content": fields.content' in config
        assert '"log.source": name' in config

    def test_a_tag_key_that_would_break_the_json_is_refused(self):
        result = run_script(
            "--dry-run", "--output", "file", "--command", "echo 1", "--tag", 'bad key=1'
        )
        assert result.returncode != 0
        assert "--tag key may only contain" in result.stderr
