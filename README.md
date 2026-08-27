# telegraf-dynatrace-exec

One script that installs Telegraf on a Linux host, runs a command you choose on a
schedule, and sends the result to Dynatrace.

```bash
sudo ./install-telegraf-dynatrace.sh \
  --command 'cat /proc/loadavg | cut -d" " -f1' \
  --metric-name node_load1 \
  --interval 60s
```

That installs the latest Telegraf from InfluxData's repository, writes a command wrapper
and a Telegraf config, validates them by running the command once, and starts the service.
Nothing else on the host is touched.

## Read this first: OneAgent's OTLP endpoint is traces-only

If you came here to send command output to OneAgent's local **OTLP** endpoint, that is not
possible, and it is worth knowing why before you start. Dynatrace's documentation is
explicit:

> Traces-only means OneAgent only accepts tracing information, not metrics or logs.

OneAgent's local endpoints on port 14499 are three separate things:

| Path | Protocol | Accepts |
|---|---|---|
| `/otlp/v1/traces` | OTLP/HTTP | Traces only |
| `/metrics/ingest` | Dynatrace metric line protocol | Metrics |
| `/v2/logs/ingest` | Dynatrace log JSON | Logs |

So a metric from a command cannot go through OneAgent *as OTLP*. This script offers the
two paths that do work:

| `--output` | Goes to | Protocol | Credentials |
|---|---|---|---|
| `oneagent` (default) | OneAgent on this host | Dynatrace metric line protocol | None — OneAgent authenticates for you |
| `otlp` | Any OTLP endpoint you name | OTLP/HTTP + protobuf | API token with `metrics.ingest` |

**Use `oneagent`** when the host already has a OneAgent. No token to manage or rotate, and
every data point is enriched with host context so it lands on the right host in Dynatrace.

**Use `otlp`** when you specifically want OTLP: sending straight to a tenant, or into an
OpenTelemetry Collector you already run.

```bash
sudo ./install-telegraf-dynatrace.sh \
  --command 'systemctl list-units --state=failed --no-legend | wc -l' \
  --metric-name failed_units \
  --output otlp \
  --otlp-endpoint https://abc12345.live.dynatrace.com/api/v2/otlp/v1/metrics \
  --otlp-token-file /root/.dt-token
```

Two things about that endpoint are load-bearing, and the script sets both: Dynatrace
accepts **HTTP only** (`gRPC is not supported`) and **protobuf only** (`JSON is not
supported`). Telegraf defaults to gRPC, so pointing it at a bare `host:port` silently
produces a config that can never deliver. The script refuses an endpoint that is not a
full `http(s)://` URL for exactly that reason.

## What arrives in Dynatrace

`--command 'cat /proc/loadavg | cut -d" " -f1' --metric-name node_load1` gives you a metric
named `telegraf.node_load1` in `oneagent` mode (the `telegraf` prefix is
`--metric-prefix`), or `node_load1` in `otlp` mode. Query it:

```
timeseries avg(telegraf.node_load1), by: {host.name}
```

Every point carries the host and whatever you add with `--tag key=value`.

### Getting a clean metric name in OTLP mode

Telegraf's OTLP conversion names each metric `<measurement>_<field>`, so the field that the
`value` parser produces would arrive as `node_load1_value`. The script generates a small
rename processor that turns the field into `gauge`, which collapses the name back to
`node_load1` and types the point as an OTLP gauge. This is easy to miss by hand and there
is a test that pins it.

## What the script puts on the host

| Path | Mode | What it is |
|---|---|---|
| `/etc/telegraf/dynatrace-exec/<name>.sh` | 0750 root:telegraf | Your command, verbatim, in a `#!/bin/sh` wrapper |
| `/etc/telegraf/telegraf.d/<name>.conf` | 0640 root:telegraf | The Telegraf input, processor and output |
| `/etc/telegraf/dynatrace-exec/<name>.env` | 0600 root:root | The API token, only when there is one |
| `/etc/systemd/system/telegraf.service.d/<name>.conf` | 0644 | Points systemd at that env file |

The command lives in its own file rather than inside the TOML on purpose. Telegraf's exec
input does not run its command through a shell, so a pipe written into the config would be
passed to the program as a literal argument; and putting arbitrary text through TOML
quoting on top of shell quoting is a reliable way to end up with a config that means
something other than what you typed. With the wrapper, pipes, redirection, `&&` and
multi-line scripts all behave the way they do in your terminal.

The generated output has `namepass` set to just your metric. A file in `telegraf.d` adds a
**global** output, so without it, every other input on the host would also start flowing to
Dynatrace.

## Options

`--help` has the full list. The ones worth knowing:

| Option | Default | Notes |
|---|---|---|
| `--command` | required | Runs under `/bin/sh` |
| `--metric-name` | `telegraf_exec` | The name you will query |
| `--interval` | `60s` | How often to run it |
| `--timeout` | `30s` | The command is killed after this |
| `--data-format` | `value` | `value`, `json`, `influx`, `csv`, `logfmt` |
| `--data-type` | `float` | For `value`: what the command prints |
| `--tag k=v` | — | Extra dimension. Repeatable |
| `--config-name` | `dynatrace-exec` | Run the script again with a different one to add a second command |
| `--install-method` | `auto` | `repo`, `package`, `tarball`, `none` |
| `--dry-run` | — | Print every action and file, change nothing |
| `--uninstall` | — | Remove what this script generated |

Your command must print something numeric for `--data-format value`. If it prints
structured output, use `json` or `influx` instead. If what you actually want is the
command's **text** in Dynatrace, you want log ingest, not a metric — this script does not
do that.

Run it again with a different `--config-name` to add more commands; each gets its own
wrapper, config and schedule.

### Dry run

`--dry-run` needs no root and writes nothing. It prints every command it would run and the
full contents of every file it would write, so you can read the plan before it touches
anything. Tokens are redacted.

## Security

- The wrapper runs as the `telegraf` user. Give that user only what your command needs.
- Anyone who can edit `/etc/telegraf/telegraf.d/` can change what runs. It is root-owned.
- A token never lands in the Telegraf config. It goes in a `0600 root:root` env file that
  systemd reads as root before dropping privileges, and the config references
  `${DT_API_TOKEN}`. Prefer `--otlp-token-file` over `--otlp-token` so it never enters your
  shell history or the process list.
- The InfluxData signing key is checked against fingerprint
  `24C975CBA61A024EE1B631787C3D57159FC2F927` before the repository is added; a mismatch
  aborts rather than warns.

## Uninstall

```bash
sudo ./install-telegraf-dynatrace.sh --uninstall --config-name dynatrace-exec
sudo ./install-telegraf-dynatrace.sh --uninstall --purge   # also remove Telegraf itself
```

## Supported hosts

Debian/Ubuntu (apt), RHEL/CentOS/Rocky/Alma/Fedora (dnf/yum), SUSE (zypper). Anything else
falls back to the official tarball plus a generated systemd unit. amd64, arm64, armhf and
i386.

## Development

```bash
python -m venv .venv && .venv/bin/pip install pytest opentelemetry-proto
pytest -q                # 60 tests
shellcheck --severity=style install-telegraf-dynatrace.sh
```

The tests do not assert on the text of the generated config. They render it with the
script, hand it to a **real Telegraf binary** (downloaded and cached on first run), and
decode what comes out the other end — the Dynatrace metric line protocol on one side, and
OTLP protobuf parsed with the generated OpenTelemetry classes on the other. A config that
parses but delivers nothing fails these tests.

That harness earned its keep. It caught, among other things: that Telegraf's OTLP output
would have shipped metrics named `<metric>_value`; that `outputs.dynatrace` waives its
token requirement for the `localhost` form of the OneAgent URL but *not* the `127.0.0.1`
form; and that the single-string `commands` syntax is deprecated in Telegraf 1.39 and
removed in 1.45.

`*.sh` is pinned to LF in `.gitattributes`. A shell script with CRLF endings fails on
Linux with `bad interpreter: /bin/bash^M`.

## Limitations

- Metrics only. For a command's text output as logs, this is the wrong tool.
- The command must finish. Anything long-running hits `--timeout`; there is no streaming.
- No interactive commands — no `sudo` password prompts. Use `NOPASSWD` or a dedicated user.
- `oneagent` mode needs "Enable local HTTP Metric, Log and Event Ingest API" turned on in
  the Extension Execution Controller settings, or OneAgent will not be listening on 14499.

## License

MIT — see [LICENSE](LICENSE).
