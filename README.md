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


| `--output` | Goes to | Arrives as | Credentials |
|---|---|---|---|
| `oneagent` (default) | OneAgent on this host, `/metrics/ingest` | a metric | None — OneAgent authenticates for you |
| `otlp` | Any OTLP endpoint you name | a metric | API token with `metrics.ingest` |
| `file` | A log file OneAgent tails | **log records** | None |

**Use `oneagent`** when the host has a OneAgent and your command prints a number. No token
to manage or rotate, and every data point is enriched with host context.

**Use `otlp`** when you specifically want OTLP: sending straight to a tenant, or into an
OpenTelemetry Collector you already run.

**Use `file`** when you want the command's *text*, not a number. Telegraf writes each
output line to a log file and OneAgent ingests it — no token, and it sidesteps the
traces-only limitation entirely, because it never touches an OTLP endpoint.

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

## File mode: the command's text as log records

```bash
sudo ./install-telegraf-dynatrace.sh \
  --output file \
  --command 'systemctl --failed --no-legend' \
  --metric-name failed_units \
  --interval 5m --tag env=prod
```

Telegraf writes to `/var/log/telegraf-dynatrace-exec/dynatrace-exec.log`, one JSON record
per line of command output:

```json
{"timestamp":"2026-08-27T02:37:11.000Z","content":"nginx.service loaded failed failed","host.name":"web-01","log.source":"failed_units","env":"prod"}
```

Those field names are deliberate. `content` becomes the log message and `timestamp` the
log timestamp; the rest become attributes. Telegraf's default JSON nests everything under
`fields`, which would leave you querying `fields.content` instead of reading a log line, so
the generated config reshapes it with `json_transformation`.

Each output line is its own record. That takes the line-oriented `grok` parser — the
`value` parser hands Telegraf the whole stdout as a single string, so a three-line command
would arrive as one record with `\n` in the middle of it.

### You must add a custom log source, or nothing arrives

**OneAgent does not discover arbitrary log files.** The service will look perfectly
healthy, the file will fill up, and Dynatrace will show nothing. Add the path once:

> Settings → Collect and capture → Log monitoring → Configure log module → Sources →
> **New log source rule**, and give it the absolute path the script printed.

The path must be absolute; Dynatrace rejects relative ones. Then:

```
fetch logs
| filter log.source == "failed_units"
| sort timestamp desc
```

Rotation is on by default — 10 MB, 5 archives, tunable with `--log-rotate-size` and
`--log-keep`. Rotated files are renamed (`dynatrace-exec.2026-08-26-1787798259.log`), so
pointing the log source at the exact active path picks up new records without re-reading
archives. Uninstall leaves the log files alone; delete them yourself if you want them gone.

## What arrives in Dynatrace

`--command 'cat /proc/loadavg | cut -d" " -f1' --metric-name node_load1` gives you:

| Mode | Metric key in Dynatrace |
|---|---|
| `oneagent` | `telegraf.node_load1.value` |
| `otlp` | `node_load1` |

The two differ, and the difference bites. The Dynatrace output builds its key as
`<prefix>.<measurement>.<field>`, so the field that the `value` parser produces shows up
as a trailing `.value` — query `telegraf.node_load1` and you get nothing back. The prefix
is `--metric-prefix`, and the script prints the exact key to query when it finishes.

```
timeseries avg(telegraf.node_load1.value), by: {host.name}
```

With `--data-format json` or `influx`, the last segment is whichever field your command
emitted instead of `value`.

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
| `/var/log/telegraf-dynatrace-exec/<name>.log` | 0755 dir, telegraf-owned | File mode only: the log OneAgent tails |

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
| `--data-format` | `lines` in file mode, else `value` | `lines`, `value`, `json`, `influx`, `csv`, `logfmt` |
| `--output` | `oneagent` | `oneagent`, `otlp`, `file` |
| `--log-path` | `/var/log/telegraf-dynatrace-exec/<config-name>.log` | file mode only |
| `--log-rotate-size` / `--log-keep` | `10MB` / `5` | file mode only |
| `--data-type` | `float` | For `value`: what the command prints |
| `--tag k=v` | — | Extra dimension. Repeatable |
| `--config-name` | `dynatrace-exec` | Run the script again with a different one to add a second command |
| `--install-method` | `auto` | `repo`, `package`, `tarball`, `none` |
| `--dry-run` | — | Print every action and file, change nothing |
| `--uninstall` | — | Remove what this script generated |

Your command must print something numeric for `--data-format value`. If it prints
structured output, use `json` or `influx`. If what you want is the command's **text**, use
`--output file`, which captures it as log records instead.

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

- The metric modes need a number. Use `--output file` for text.
- File mode needs a custom log source configured in Dynatrace once, per path.
- No OTLP logs: Telegraf's OTLP output plugin emits metrics only, whatever the endpoint.
- The command must finish. Anything long-running hits `--timeout`; there is no streaming.
- No interactive commands — no `sudo` password prompts. Use `NOPASSWD` or a dedicated user.
- `oneagent` mode needs "Enable local HTTP Metric, Log and Event Ingest API" turned on in
  the Extension Execution Controller settings, or OneAgent will not be listening on 14499.

## License

MIT — see [LICENSE](LICENSE).
