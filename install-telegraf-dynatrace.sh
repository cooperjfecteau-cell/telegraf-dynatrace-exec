#!/usr/bin/env bash
#
# Install Telegraf on a Linux host, run a command of your choosing on a schedule, and
# send the result to Dynatrace — either through the local OneAgent or over OTLP.
#
# Usage: ./install-telegraf-dynatrace.sh --command '<shell command>' [options]
# See --help for the full list.

set -euo pipefail

readonly SCRIPT_VERSION="0.1.0"
readonly INFLUXDATA_KEY_URL="https://repos.influxdata.com/influxdata-archive.key"
readonly INFLUXDATA_KEY_FINGERPRINT="24C975CBA61A024EE1B631787C3D57159FC2F927"
readonly INFLUXDATA_DOWNLOAD_BASE="https://dl.influxdata.com/telegraf/releases"
readonly TELEGRAF_RELEASES_API="https://api.github.com/repos/influxdata/telegraf/releases/latest"

# Used only when the release API cannot be reached; kept current with the repository.
readonly TELEGRAF_FALLBACK_VERSION="1.39.3"

readonly ONEAGENT_METRICS_URL="http://localhost:14499/metrics/ingest"

# Overridable so the test suite can render into a temp tree, and so unusual hosts can move them.
TELEGRAF_CONF_DIR="${TELEGRAF_CONF_DIR:-/etc/telegraf/telegraf.d}"
MANAGED_DIR="${MANAGED_DIR:-/etc/telegraf/dynatrace-exec}"
SYSTEMD_DROPIN_DIR="${SYSTEMD_DROPIN_DIR:-/etc/systemd/system/telegraf.service.d}"

# Telegraf's exec input does not use a shell, so the wrapper is invoked explicitly.
# This also survives /etc being mounted noexec.
SHELL_BIN="${SHELL_BIN:-/bin/sh}"

# ---------------------------------------------------------------------------
# Settings, all overridable from the command line.
# ---------------------------------------------------------------------------
COMMAND=""
CONFIG_NAME="dynatrace-exec"
METRIC_NAME="telegraf_exec"
INTERVAL="60s"
TIMEOUT="30s"
DATA_FORMAT="value"
DATA_TYPE="float"
OUTPUT_MODE="oneagent"
ONEAGENT_URL="$ONEAGENT_METRICS_URL"
ONEAGENT_TOKEN=""
METRIC_PREFIX="telegraf"
OTLP_ENDPOINT=""
OTLP_TOKEN=""
OTLP_TOKEN_FILE=""
TELEGRAF_VERSION=""
INSTALL_METHOD="auto"
RUN_AS="telegraf"
DRY_RUN=0
NO_START=0
DO_UNINSTALL=0
PURGE=0
declare -a EXTRA_TAGS=()

# Filled in by detection.
OS_ID=""
OS_ID_LIKE=""
PKG_MANAGER=""
ARCH=""

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
    C_BOLD=$'\033[1m'; C_RED=$'\033[31m'; C_YELLOW=$'\033[33m'; C_GREEN=$'\033[32m'; C_OFF=$'\033[0m'
else
    C_BOLD=""; C_RED=""; C_YELLOW=""; C_GREEN=""; C_OFF=""
fi

log()  { printf '%s==>%s %s\n' "$C_BOLD" "$C_OFF" "$*"; }
ok()   { printf '%s  ok%s %s\n' "$C_GREEN" "$C_OFF" "$*"; }
warn() { printf '%swarn%s %s\n' "$C_YELLOW" "$C_OFF" "$*" >&2; }
die()  { printf '%serror%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }

# Every command that changes the system goes through run(), so --dry-run is honest:
# it prints the exact command instead of a paraphrase of it.
run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  [dry-run] %s\n' "$*"
        return 0
    fi
    "$@"
}

usage() {
    cat <<'USAGE'
install-telegraf-dynatrace.sh — run a command with Telegraf, ship the result to Dynatrace

  sudo ./install-telegraf-dynatrace.sh --command 'cat /proc/loadavg | cut -d" " -f1' \
       --metric-name node_load1

Required
  --command CMD            Shell command to run. Runs under /bin/sh, so pipes,
                           redirection and multiple statements all work.

What to collect
  --metric-name NAME       Metric name in Dynatrace (default: telegraf_exec)
  --interval DURATION      How often to run it (default: 60s)
  --timeout DURATION       Kill the command after this long (default: 30s)
  --data-format FORMAT     value | json | influx | csv | logfmt (default: value)
  --data-type TYPE         For --data-format value: float | integer | string
                           (default: float)
  --tag KEY=VALUE          Extra dimension on every data point. Repeatable.

Where to send it
  --output MODE            oneagent | otlp (default: oneagent)

  oneagent mode — the local OneAgent metric API, no credentials needed:
  --oneagent-url URL       Default: http://localhost:14499/metrics/ingest
  --oneagent-token TOKEN   Only needed if you point --oneagent-url somewhere other
                           than the local OneAgent, e.g. straight at a tenant
  --metric-prefix PREFIX   Prefix for metric names (default: telegraf)

  otlp mode — OTLP/HTTP protobuf to any OTLP endpoint:
  --otlp-endpoint URL      e.g. https://ENVID.live.dynatrace.com/api/v2/otlp/v1/metrics
                           or a local OpenTelemetry Collector on :4318
  --otlp-token TOKEN       Dynatrace API token with the metrics.ingest scope
  --otlp-token-file PATH   Read the token from a file instead (keeps it out of
                           your shell history and the process list)

Installation
  --telegraf-version X.Y.Z Pin a version (default: latest)
  --install-method METHOD  auto | repo | package | tarball | none (default: auto)
  --run-as USER            Service user (default: telegraf)
  --config-name NAME       Name for the generated files (default: dynatrace-exec)

Behaviour
  --dry-run                Print every action and the rendered files, change nothing
  --no-start               Write the config but do not restart Telegraf
  --uninstall              Remove the files this script generated
  --purge                  With --uninstall, also remove the Telegraf package
  -h, --help               This message
  --version                Print the script version

Note: OneAgent's local OTLP endpoint accepts traces only, so oneagent mode uses the
OneAgent metric API. Use otlp mode to send genuine OTLP metrics to a tenant endpoint
or a collector.
USAGE
}

# ---------------------------------------------------------------------------
# Argument parsing and validation
# ---------------------------------------------------------------------------
parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --command)           COMMAND="${2:?--command needs a value}"; shift 2 ;;
            --config-name)       CONFIG_NAME="${2:?--config-name needs a value}"; shift 2 ;;
            --metric-name)       METRIC_NAME="${2:?--metric-name needs a value}"; shift 2 ;;
            --interval)          INTERVAL="${2:?--interval needs a value}"; shift 2 ;;
            --timeout)           TIMEOUT="${2:?--timeout needs a value}"; shift 2 ;;
            --data-format)       DATA_FORMAT="${2:?--data-format needs a value}"; shift 2 ;;
            --data-type)         DATA_TYPE="${2:?--data-type needs a value}"; shift 2 ;;
            --tag)               EXTRA_TAGS+=("${2:?--tag needs KEY=VALUE}"); shift 2 ;;
            --output)            OUTPUT_MODE="${2:?--output needs a value}"; shift 2 ;;
            --oneagent-url)      ONEAGENT_URL="${2:?--oneagent-url needs a value}"; shift 2 ;;
            --oneagent-token)    ONEAGENT_TOKEN="${2:?--oneagent-token needs a value}"; shift 2 ;;
            --metric-prefix)     METRIC_PREFIX="${2:?--metric-prefix needs a value}"; shift 2 ;;
            --otlp-endpoint)     OTLP_ENDPOINT="${2:?--otlp-endpoint needs a value}"; shift 2 ;;
            --otlp-token)        OTLP_TOKEN="${2:?--otlp-token needs a value}"; shift 2 ;;
            --otlp-token-file)   OTLP_TOKEN_FILE="${2:?--otlp-token-file needs a value}"; shift 2 ;;
            --telegraf-version)  TELEGRAF_VERSION="${2:?--telegraf-version needs a value}"; shift 2 ;;
            --install-method)    INSTALL_METHOD="${2:?--install-method needs a value}"; shift 2 ;;
            --run-as)            RUN_AS="${2:?--run-as needs a value}"; shift 2 ;;
            --dry-run)           DRY_RUN=1; shift ;;
            --no-start)          NO_START=1; shift ;;
            --uninstall)         DO_UNINSTALL=1; shift ;;
            --purge)             PURGE=1; shift ;;
            -h|--help)           usage; exit 0 ;;
            --version)           printf '%s\n' "$SCRIPT_VERSION"; exit 0 ;;
            *)                   die "unknown option: $1 (try --help)" ;;
        esac
    done
}

validate_args() {
    [ "$DO_UNINSTALL" -eq 1 ] && return 0

    [ -n "$COMMAND" ] || die "--command is required (try --help)"

    case "$CONFIG_NAME" in
        *[!A-Za-z0-9_-]*|"") die "--config-name may only contain letters, digits, dash and underscore" ;;
    esac

    case "$METRIC_NAME" in
        *[!A-Za-z0-9_.-]*|"") die "--metric-name may only contain letters, digits, dot, dash and underscore" ;;
    esac

    is_duration "$INTERVAL" || die "--interval must look like 30s, 5m or 1h, got '$INTERVAL'"
    is_duration "$TIMEOUT"  || die "--timeout must look like 30s, 5m or 1h, got '$TIMEOUT'"

    case "$DATA_FORMAT" in
        value|json|influx|csv|logfmt) ;;
        *) die "--data-format must be one of value, json, influx, csv, logfmt" ;;
    esac

    case "$DATA_TYPE" in
        float|integer|string) ;;
        *) die "--data-type must be one of float, integer, string" ;;
    esac

    case "$INSTALL_METHOD" in
        auto|repo|package|tarball|none) ;;
        *) die "--install-method must be one of auto, repo, package, tarball, none" ;;
    esac

    local tag
    for tag in ${EXTRA_TAGS+"${EXTRA_TAGS[@]}"}; do
        case "$tag" in
            *=*) ;;
            *) die "--tag expects KEY=VALUE, got '$tag'" ;;
        esac
    done

    case "$OUTPUT_MODE" in
        oneagent)
            [ -n "$ONEAGENT_URL" ] || die "--oneagent-url cannot be empty"
            if [ "$ONEAGENT_URL" != "$ONEAGENT_METRICS_URL" ] && [ -z "$ONEAGENT_TOKEN" ]; then
                die "the Dynatrace output only skips authentication for the local OneAgent URL ($ONEAGENT_METRICS_URL). For any other URL it refuses to start without a token, so pass --oneagent-token."
            fi
            ;;
        otlp)
            [ -n "$OTLP_ENDPOINT" ] || die "otlp mode needs --otlp-endpoint"
            case "$OTLP_ENDPOINT" in
                http://*|https://*) ;;
                *) die "--otlp-endpoint must be a full http:// or https:// URL. Telegraf only speaks OTLP over gRPC for bare host:port, and Dynatrace does not accept gRPC." ;;
            esac
            if [ -n "$OTLP_TOKEN" ] && [ -n "$OTLP_TOKEN_FILE" ]; then
                die "use either --otlp-token or --otlp-token-file, not both"
            fi
            if [ -n "$OTLP_TOKEN_FILE" ] && [ "$DRY_RUN" -eq 0 ] && [ ! -r "$OTLP_TOKEN_FILE" ]; then
                die "cannot read --otlp-token-file '$OTLP_TOKEN_FILE'"
            fi
            case "$OTLP_ENDPOINT" in
                *dynatrace.com*|*/api/v2/otlp/*)
                    if [ -z "$OTLP_TOKEN" ] && [ -z "$OTLP_TOKEN_FILE" ]; then
                        die "a Dynatrace OTLP endpoint needs --otlp-token or --otlp-token-file (scope: metrics.ingest)"
                    fi
                    ;;
            esac
            ;;
        *) die "--output must be oneagent or otlp" ;;
    esac
}

is_duration() {
    case "$1" in
        ""|*[!0-9smh]*) return 1 ;;
        *[0-9][smh]) return 0 ;;
        *) return 1 ;;
    esac
}

# ---------------------------------------------------------------------------
# Host detection
# ---------------------------------------------------------------------------
detect_os() {
    if [ -r /etc/os-release ]; then
        # Sourced in a subshell so ID and friends cannot leak into this script's scope.
        # shellcheck disable=SC1091 # /etc/os-release only exists on the target host
        OS_ID=$(. /etc/os-release && printf '%s' "${ID:-}")
        # shellcheck disable=SC1091
        OS_ID_LIKE=$(. /etc/os-release && printf '%s' "${ID_LIKE:-}")
    fi
    [ -n "$OS_ID" ] || OS_ID="unknown"
}

detect_arch() {
    local machine
    machine=$(uname -m)
    ARCH=$(normalize_arch "$machine") || die "unsupported architecture: $machine"
}

# Telegraf's release assets use Go's naming, not uname's.
normalize_arch() {
    case "$1" in
        x86_64|amd64)   printf 'amd64' ;;
        aarch64|arm64)  printf 'arm64' ;;
        armv7l|armhf)   printf 'armhf' ;;
        i386|i686)      printf 'i386' ;;
        *)              return 1 ;;
    esac
}

detect_pkg_manager() {
    if command -v apt-get >/dev/null 2>&1; then PKG_MANAGER="apt"
    elif command -v dnf >/dev/null 2>&1;    then PKG_MANAGER="dnf"
    elif command -v yum >/dev/null 2>&1;    then PKG_MANAGER="yum"
    elif command -v zypper >/dev/null 2>&1; then PKG_MANAGER="zypper"
    else PKG_MANAGER="none"
    fi
}

resolve_install_method() {
    if [ "$INSTALL_METHOD" != "auto" ]; then
        printf '%s' "$INSTALL_METHOD"
        return 0
    fi
    case "$PKG_MANAGER" in
        apt|dnf|yum|zypper) printf 'repo' ;;
        *)                  printf 'tarball' ;;
    esac
}

resolve_version() {
    if [ -n "$TELEGRAF_VERSION" ]; then
        printf '%s' "$TELEGRAF_VERSION"
        return 0
    fi
    local tag
    tag=$(curl -fsSL --max-time 15 "$TELEGRAF_RELEASES_API" 2>/dev/null \
          | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"v\{0,1\}\([^"]*\)".*/\1/p' \
          | head -n 1) || tag=""
    if [ -n "$tag" ]; then
        printf '%s' "$tag"
    else
        warn "could not reach the Telegraf release API, falling back to $TELEGRAF_FALLBACK_VERSION"
        printf '%s' "$TELEGRAF_FALLBACK_VERSION"
    fi
}

tarball_url() {
    printf '%s/telegraf-%s_linux_%s.tar.gz' "$INFLUXDATA_DOWNLOAD_BASE" "$1" "$2"
}

deb_url() {
    printf '%s/telegraf_%s-1_%s.deb' "$INFLUXDATA_DOWNLOAD_BASE" "$1" "$2"
}

rpm_url() {
    local rpm_arch
    case "$2" in
        amd64) rpm_arch="x86_64" ;;
        arm64) rpm_arch="aarch64" ;;
        armhf) rpm_arch="armv6hl" ;;
        i386)  rpm_arch="i386" ;;
        *)     rpm_arch="$2" ;;
    esac
    printf '%s/telegraf-%s-1.%s.rpm' "$INFLUXDATA_DOWNLOAD_BASE" "$1" "$rpm_arch"
}

# ---------------------------------------------------------------------------
# Installing Telegraf
# ---------------------------------------------------------------------------
telegraf_installed() {
    command -v telegraf >/dev/null 2>&1
}

install_telegraf() {
    local method
    method=$(resolve_install_method)

    if [ "$method" = "none" ]; then
        telegraf_installed || die "--install-method none, but telegraf is not on PATH"
        ok "using the Telegraf already installed: $(telegraf --version 2>/dev/null || true)"
        return 0
    fi

    if telegraf_installed && [ -z "$TELEGRAF_VERSION" ]; then
        log "Telegraf is already installed: $(telegraf --version 2>/dev/null || true)"
        log "upgrading to the latest available"
    fi

    case "$method" in
        repo)    install_via_repo ;;
        package) install_via_package ;;
        tarball) install_via_tarball ;;
        *)       die "unknown install method: $method" ;;
    esac
}

install_via_repo() {
    case "$PKG_MANAGER" in
        apt)            install_apt_repo ;;
        dnf|yum|zypper) install_rpm_repo ;;
        *)              die "no supported package manager found; try --install-method tarball" ;;
    esac
}

install_apt_repo() {
    log "adding the InfluxData apt repository"
    local keyring="/etc/apt/keyrings/influxdata-archive.gpg"
    local tmp_key
    tmp_key=$(mktemp)

    run mkdir -p /etc/apt/keyrings

    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  [dry-run] curl -fsSL %s -o %s\n' "$INFLUXDATA_KEY_URL" "$tmp_key"
        printf '  [dry-run] verify fingerprint %s\n' "$INFLUXDATA_KEY_FINGERPRINT"
        printf '  [dry-run] gpg --dearmor > %s\n' "$keyring"
    else
        curl -fsSL "$INFLUXDATA_KEY_URL" -o "$tmp_key" \
            || die "could not download the InfluxData signing key"
        verify_key_fingerprint "$tmp_key" \
            || die "the InfluxData signing key does not match the expected fingerprint $INFLUXDATA_KEY_FINGERPRINT — refusing to add the repository"
        gpg --dearmor < "$tmp_key" > "$keyring" \
            || die "could not dearmor the InfluxData signing key"
        chmod 0644 "$keyring"
        ok "signing key verified against $INFLUXDATA_KEY_FINGERPRINT"
    fi
    rm -f "$tmp_key"

    write_file /etc/apt/sources.list.d/influxdata.list 0644 \
        "deb [signed-by=$keyring] https://repos.influxdata.com/debian stable main"

    run apt-get update
    if [ -n "$TELEGRAF_VERSION" ]; then
        run env DEBIAN_FRONTEND=noninteractive apt-get install -y "telegraf=${TELEGRAF_VERSION}-1"
    else
        run env DEBIAN_FRONTEND=noninteractive apt-get install -y telegraf
    fi
}

verify_key_fingerprint() {
    gpg --show-keys --with-fingerprint --with-colons "$1" 2>/dev/null \
        | grep -q "^fpr:*${INFLUXDATA_KEY_FINGERPRINT}:$"
}

install_rpm_repo() {
    log "adding the InfluxData rpm repository"
    write_file /etc/yum.repos.d/influxdata.repo 0644 "$(cat <<REPO
[influxdata]
name = InfluxData Repository - Stable
baseurl = https://repos.influxdata.com/stable/\$basearch/main
enabled = 1
gpgcheck = 1
gpgkey = $INFLUXDATA_KEY_URL
REPO
)"

    local pkg="telegraf"
    [ -n "$TELEGRAF_VERSION" ] && pkg="telegraf-${TELEGRAF_VERSION}"

    case "$PKG_MANAGER" in
        dnf)    run dnf install -y "$pkg" ;;
        yum)    run yum install -y "$pkg" ;;
        zypper) run zypper --non-interactive install -y "$pkg" ;;
    esac
}

install_via_package() {
    local version url tmp
    version=$(resolve_version)
    tmp=$(mktemp -d)

    case "$PKG_MANAGER" in
        apt)
            url=$(deb_url "$version" "$ARCH")
            log "downloading $url"
            run curl -fsSL "$url" -o "$tmp/telegraf.deb"
            run env DEBIAN_FRONTEND=noninteractive apt-get install -y "$tmp/telegraf.deb"
            ;;
        dnf|yum|zypper)
            url=$(rpm_url "$version" "$ARCH")
            log "downloading $url"
            run curl -fsSL "$url" -o "$tmp/telegraf.rpm"
            case "$PKG_MANAGER" in
                dnf)    run dnf install -y "$tmp/telegraf.rpm" ;;
                yum)    run yum install -y "$tmp/telegraf.rpm" ;;
                zypper) run zypper --non-interactive install --allow-unsigned-rpm "$tmp/telegraf.rpm" ;;
            esac
            ;;
        *) die "--install-method package needs apt, dnf, yum or zypper; try --install-method tarball" ;;
    esac
    rm -rf "$tmp"
}

install_via_tarball() {
    local version url tmp
    version=$(resolve_version)
    url=$(tarball_url "$version" "$ARCH")
    tmp=$(mktemp -d)

    log "downloading $url"
    run curl -fsSL "$url" -o "$tmp/telegraf.tar.gz"
    run tar -xzf "$tmp/telegraf.tar.gz" -C "$tmp"

    # The tarball unpacks a full filesystem layout under telegraf-<version>/.
    run install -m 0755 "$tmp/telegraf-$version/usr/bin/telegraf" /usr/bin/telegraf
    run mkdir -p /etc/telegraf/telegraf.d /var/log/telegraf

    if ! id -u "$RUN_AS" >/dev/null 2>&1; then
        log "creating the $RUN_AS service user"
        run useradd --system --no-create-home --shell /usr/sbin/nologin "$RUN_AS"
    fi
    run chown -R "$RUN_AS:$RUN_AS" /var/log/telegraf

    if [ ! -f /etc/telegraf/telegraf.conf ]; then
        write_file /etc/telegraf/telegraf.conf 0644 "$(render_base_telegraf_conf)"
    fi

    write_file /etc/systemd/system/telegraf.service 0644 "$(render_systemd_unit)"
    run systemctl daemon-reload
    rm -rf "$tmp"
}

render_base_telegraf_conf() {
    cat <<'CONF'
# Minimal base configuration written by install-telegraf-dynatrace.sh.
# Per-integration configuration lives in /etc/telegraf/telegraf.d/.
[agent]
  interval = "60s"
  round_interval = true
  metric_batch_size = 1000
  metric_buffer_limit = 10000
  collection_jitter = "0s"
  flush_interval = "10s"
  flush_jitter = "0s"
  precision = ""
  omit_hostname = false
CONF
}

render_systemd_unit() {
    cat <<UNIT
[Unit]
Description=Telegraf
Documentation=https://github.com/influxdata/telegraf
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
User=$RUN_AS
Group=$RUN_AS
ExecStart=/usr/bin/telegraf -config /etc/telegraf/telegraf.conf -config-directory /etc/telegraf/telegraf.d
ExecReload=/bin/kill -HUP \$MAINPID
Restart=on-failure
RestartForceExitStatus=SIGPIPE
KillMode=control-group

[Install]
WantedBy=multi-user.target
UNIT
}

# ---------------------------------------------------------------------------
# Rendering the generated files
# ---------------------------------------------------------------------------

# TOML basic strings take C-style escapes, so a backslash or a quote in a value has to
# be escaped or the config silently changes meaning.
toml_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

command_wrapper_path() { printf '%s/%s.sh' "$MANAGED_DIR" "$CONFIG_NAME"; }
config_path()          { printf '%s/%s.conf' "$TELEGRAF_CONF_DIR" "$CONFIG_NAME"; }
env_file_path()        { printf '%s/%s.env' "$MANAGED_DIR" "$CONFIG_NAME"; }
dropin_path()          { printf '%s/%s.conf' "$SYSTEMD_DROPIN_DIR" "$CONFIG_NAME"; }

# The command goes into its own file rather than into the TOML. Telegraf's exec input
# splits commands itself and does not use a shell, so a pipe in the TOML would be passed
# as a literal argument; and putting arbitrary user text through TOML quoting on top of
# shell quoting is a reliable way to produce a config that means something else.
render_command_wrapper() {
    cat <<WRAPPER
#!/bin/sh
# Generated by install-telegraf-dynatrace.sh — regenerate rather than editing.
# Runs every $INTERVAL under Telegraf's exec input as user $RUN_AS.
set -eu

$COMMAND
WRAPPER
}

telegraf_cmd() { printf '%s' "${TELEGRAF_BIN:-telegraf}"; }

# Empty when Telegraf is not installed yet, which is the normal case under --dry-run.
# Kept failure-tolerant on purpose: pipefail would otherwise abort the whole script here.
installed_telegraf_version() {
    local output
    output=$("$(telegraf_cmd)" --version 2>/dev/null) || return 0
    printf '%s' "$output" | sed -n 's/^Telegraf \([0-9][0-9.]*\).*/\1/p' | head -n 1
}

# version_at_least HAVE WANT
version_at_least() {
    [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n 1)" = "$2" ]
}

# Telegraf 1.39 takes each command as an argv array and deprecated the single-string
# form (removed in 1.45). The array form is also the only one that survives a space in a
# path, since the string form is split on whitespace.
render_commands_line() {
    local have
    have=$(installed_telegraf_version || true)
    if [ -n "$have" ] && ! version_at_least "$have" "1.39.0"; then
        printf '  commands = ["%s %s"]\n' \
            "$(toml_escape "$SHELL_BIN")" "$(toml_escape "$(command_wrapper_path)")"
    else
        printf '  commands = [["%s", "%s"]]\n' \
            "$(toml_escape "$SHELL_BIN")" "$(toml_escape "$(command_wrapper_path)")"
    fi
}

render_input_section() {
    printf '[[inputs.exec]]\n'
    render_commands_line
    printf '  interval = "%s"\n' "$(toml_escape "$INTERVAL")"
    printf '  timeout = "%s"\n' "$(toml_escape "$TIMEOUT")"
    printf '  name_override = "%s"\n' "$(toml_escape "$METRIC_NAME")"
    printf '  data_format = "%s"\n' "$(toml_escape "$DATA_FORMAT")"
    if [ "$DATA_FORMAT" = "value" ]; then
        printf '  data_type = "%s"\n' "$(toml_escape "$DATA_TYPE")"
    fi

    if [ "${#EXTRA_TAGS[@]}" -gt 0 ]; then
        printf '  [inputs.exec.tags]\n'
        local tag key value
        for tag in "${EXTRA_TAGS[@]}"; do
            key="${tag%%=*}"
            value="${tag#*=}"
            printf '    %s = "%s"\n' "$key" "$(toml_escape "$value")"
        done
    fi
}

# The plugin recognises only the localhost form as "the local OneAgent" and waives the
# token there. Anywhere else the token comes from the environment, as it does for OTLP.
oneagent_api_token_value() {
    if [ -n "$ONEAGENT_TOKEN" ]; then
        # shellcheck disable=SC2016 # the literal is what Telegraf expands at run time
        printf '${DT_API_TOKEN}'
    fi
}

# Telegraf's OTLP conversion names every metric <measurement>_<field>, so the field the
# value parser calls "value" would land in Dynatrace as "<metric>_value". A field named
# "gauge" collapses to just the measurement name and types the point as an OTLP gauge.
# Only the value parser produces that field; other formats carry field names the user
# chose, and renaming those would be wrong.
render_processor_section() {
    if [ "$OUTPUT_MODE" != "otlp" ] || [ "$DATA_FORMAT" != "value" ]; then
        return 0
    fi
    cat <<PROC
[[processors.rename]]
  ## Without this the metric arrives as ${METRIC_NAME}_value.
  namepass = ["$(toml_escape "$METRIC_NAME")"]
  [[processors.rename.replace]]
    field = "value"
    dest = "gauge"

PROC
}

# namepass matters: a file in telegraf.d adds a *global* output, so without it this
# output would also receive metrics from every other input on the host.
render_output_section() {
    if [ "$OUTPUT_MODE" = "oneagent" ]; then
        cat <<OUT
[[outputs.dynatrace]]
  ## The local OneAgent metric API. It needs no credentials — OneAgent authenticates
  ## on this host's behalf and enriches every data point with host context.
  ## Requires "Enable local HTTP Metric, Log and Event Ingest API" in the
  ## Extension Execution Controller settings.
  url = "$(toml_escape "$ONEAGENT_URL")"
  api_token = "$(oneagent_api_token_value)"
  prefix = "$(toml_escape "$METRIC_PREFIX")"
  timeout = "10s"
  namepass = ["$(toml_escape "$METRIC_NAME")"]
OUT
    else
        cat <<OUT
[[outputs.opentelemetry]]
  ## OTLP over HTTP with protobuf encoding. Dynatrace does not accept OTLP/gRPC and
  ## does not accept JSON, so both of these settings are load-bearing.
  service_address = "$(toml_escape "$OTLP_ENDPOINT")"
  encoding_type = "protobuf"
  compression = "gzip"
  timeout = "10s"
  namepass = ["$(toml_escape "$METRIC_NAME")"]
OUT
        if [ -n "$OTLP_TOKEN" ] || [ -n "$OTLP_TOKEN_FILE" ]; then
            cat <<'OUT'
  ## The token is read from the environment so it never lands in this file.
  ## systemd supplies it from the EnvironmentFile drop-in, as root, before
  ## dropping to the telegraf user.
  [outputs.opentelemetry.headers]
    Authorization = "Api-Token ${DT_API_TOKEN}"
OUT
        fi
    fi
}

render_config() {
    cat <<HEADER
# Generated by install-telegraf-dynatrace.sh v$SCRIPT_VERSION — do not edit by hand.
# Regenerate with the same script; this file is overwritten on reinstall.
#
# Command:  $(command_wrapper_path)
# Metric:   $METRIC_NAME
# Interval: $INTERVAL
# Output:   $OUTPUT_MODE

HEADER
    render_input_section
    printf '\n'
    render_processor_section
    render_output_section
}

# ---------------------------------------------------------------------------
# Writing files
# ---------------------------------------------------------------------------
write_file() {
    local path="$1" mode="$2" content="$3"

    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  [dry-run] write %s (mode %s):\n' "$path" "$mode"
        printf '%s\n' "$content" | sed 's/^/      | /'
        return 0
    fi

    mkdir -p "$(dirname "$path")"
    printf '%s\n' "$content" > "$path"
    chmod "$mode" "$path"
}

install_files() {
    log "writing the command wrapper and Telegraf configuration"

    run mkdir -p "$MANAGED_DIR" "$TELEGRAF_CONF_DIR"
    write_file "$(command_wrapper_path)" 0750 "$(render_command_wrapper)"
    write_file "$(config_path)" 0640 "$(render_config)"

    if [ "$DRY_RUN" -eq 0 ]; then
        # Readable by the service user, writable only by root.
        chown "root:$RUN_AS" "$(command_wrapper_path)" "$(config_path)" 2>/dev/null || true
    fi

    if [ -n "$(resolved_token_source)" ]; then
        install_token
    fi

    ok "wrote $(config_path)"
}

# Empty unless this run has a token to install, in either output mode.
resolved_token_source() {
    if [ "$OUTPUT_MODE" = "otlp" ]; then
        [ -n "$OTLP_TOKEN" ] && printf 'inline'
        [ -n "$OTLP_TOKEN_FILE" ] && printf 'file'
    elif [ -n "$ONEAGENT_TOKEN" ]; then
        printf 'inline'
    fi
    return 0
}

install_token() {
    local token="$OTLP_TOKEN"
    [ "$OUTPUT_MODE" = "oneagent" ] && token="$ONEAGENT_TOKEN"
    if [ -n "$OTLP_TOKEN_FILE" ] && [ "$DRY_RUN" -eq 0 ]; then
        token=$(tr -d '\r\n' < "$OTLP_TOKEN_FILE")
    fi

    # 0600 root:root — systemd reads EnvironmentFile as root, so the telegraf user
    # never needs read access to the token on disk.
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  [dry-run] write %s (mode 0600): DT_API_TOKEN=***redacted***\n' "$(env_file_path)"
    else
        mkdir -p "$MANAGED_DIR"
        printf 'DT_API_TOKEN=%s\n' "$token" > "$(env_file_path)"
        chmod 0600 "$(env_file_path)"
        chown root:root "$(env_file_path)" 2>/dev/null || true
    fi

    write_file "$(dropin_path)" 0644 "$(cat <<DROPIN
[Service]
EnvironmentFile=$(env_file_path)
DROPIN
)"
    run systemctl daemon-reload
}

# ---------------------------------------------------------------------------
# Validating and starting
# ---------------------------------------------------------------------------
validate_config() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  [dry-run] telegraf --test --config %s\n' "$(config_path)"
        return 0
    fi

    log "asking Telegraf to run the command once and show what it would send"

    # --test resolves ${DT_API_TOKEN} in the config, so it has to be in the environment
    # here the way systemd will supply it to the service.
    if [ -r "$(env_file_path)" ]; then
        DT_API_TOKEN=$(sed -n 's/^DT_API_TOKEN=//p' "$(env_file_path)" | head -n 1)
        export DT_API_TOKEN
    fi

    if ! "$(telegraf_cmd)" --test --config "$(config_path)" 2>&1 | sed 's/^/      /'; then
        die "Telegraf rejected the configuration or the command failed — nothing was started. See the output above."
    fi
    ok "configuration is valid and the command produced a metric"
}

start_service() {
    if [ "$NO_START" -eq 1 ]; then
        warn "--no-start given; restart Telegraf yourself with: systemctl restart telegraf"
        return 0
    fi

    log "enabling and restarting Telegraf"
    run systemctl enable telegraf
    run systemctl restart telegraf

    if [ "$DRY_RUN" -eq 0 ]; then
        sleep 2
        if systemctl is-active --quiet telegraf; then
            ok "telegraf.service is active"
        else
            die "telegraf.service did not start. Check: journalctl -u telegraf -n 50 --no-pager"
        fi
    fi
}

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------
uninstall() {
    log "removing the files generated for '$CONFIG_NAME'"
    run rm -f "$(config_path)" "$(command_wrapper_path)" "$(env_file_path)" "$(dropin_path)"
    run systemctl daemon-reload

    if [ "$PURGE" -eq 1 ]; then
        log "removing the Telegraf package"
        case "$PKG_MANAGER" in
            apt)    run env DEBIAN_FRONTEND=noninteractive apt-get remove -y telegraf ;;
            dnf)    run dnf remove -y telegraf ;;
            yum)    run yum remove -y telegraf ;;
            zypper) run zypper --non-interactive remove telegraf ;;
            *)      warn "no package manager found; remove /usr/bin/telegraf by hand if you installed from a tarball" ;;
        esac
    else
        run systemctl restart telegraf || true
    fi
    ok "done"
}

require_root() {
    if [ "$DRY_RUN" -eq 1 ]; then
        return 0
    fi
    if [ "$(id -u)" -ne 0 ]; then
        die "this needs root — rerun with sudo, or use --dry-run to see what it would do"
    fi
}

# The Dynatrace output builds the key as <prefix>.<measurement>.<field>, so with the
# value parser the key carries a trailing ".value". Getting this wrong sends people to a
# DQL query that quietly returns nothing.
dynatrace_metric_key() {
    if [ "$DATA_FORMAT" = "value" ]; then
        printf '%s.%s.value' "$METRIC_PREFIX" "$METRIC_NAME"
    else
        printf '%s.%s.<field>' "$METRIC_PREFIX" "$METRIC_NAME"
    fi
}

print_summary() {
    local where
    if [ "$OUTPUT_MODE" = "oneagent" ]; then
        where="the local OneAgent at $ONEAGENT_URL, as $(dynatrace_metric_key)"
    else
        # otlp mode renames the field to "gauge", which collapses the name to just this.
        where="$OTLP_ENDPOINT over OTLP/HTTP, as $METRIC_NAME"
    fi

    cat <<SUMMARY

${C_BOLD}Done.${C_OFF} Telegraf runs your command every $INTERVAL and sends the result to
$where

  command    $(command_wrapper_path)
  config     $(config_path)
  logs       journalctl -u telegraf -f
  test again telegraf --test --config $(config_path)
  remove     $0 --uninstall --config-name $CONFIG_NAME
SUMMARY

    if [ "$OUTPUT_MODE" = "oneagent" ]; then
        cat <<NEXT

In Dynatrace, find it with:
  timeseries avg($(dynatrace_metric_key)), by: {host.name}
NEXT
    fi
}

# ---------------------------------------------------------------------------
main() {
    parse_args "$@"
    validate_args

    detect_os
    detect_arch
    detect_pkg_manager

    log "host: $OS_ID ${OS_ID_LIKE:+($OS_ID_LIKE) }/ $ARCH / package manager: $PKG_MANAGER"

    require_root

    if [ "$DO_UNINSTALL" -eq 1 ]; then
        uninstall
        exit 0
    fi

    install_telegraf
    install_files
    validate_config
    start_service
    print_summary
}

# Sourced by the test suite, which needs the functions without running anything.
if [ "${TELEGRAF_DT_LIB_ONLY:-0}" != "1" ]; then
    main "$@"
fi
