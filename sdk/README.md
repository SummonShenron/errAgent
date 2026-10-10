# erragent-sdk

Client SDK for [errAgent](../README.md). Replaces copy-pasting `integrations/erragent_handler.py`
into every target application with a single installable package.

## Install

```bash
pip install erragent-sdk
# or, for the local-dev remediation daemon:
pip install "erragent-sdk[local]"
```

## Quickstart

```python
import erragent

erragent.install()  # reads ERRAGENT_* env vars, installs logging handler(s) + exception hooks
```

For a FastAPI/Starlette app, also seed request-scoped context automatically:

```python
app.add_middleware(erragent.Middleware)
```

That's the whole integration surface for structured error/log reporting — no per-call-site
instrumentation required.

## Configuration

All configuration is environment-only, matching errAgent's existing convention:

| Variable | Purpose |
| --- | --- |
| `ERRAGENT_URL` | Cloud errAgent base URL |
| `ERRAGENT_APP_ID` + `ERRAGENT_APP_SECRET` | **Recommended.** Per-app ingest credentials, checked against errAgent's `ingest_clients` registry. Ask the errAgent operator to register your app. |
| `ERRAGENT_INGEST_SECRET` | Legacy shared ingest secret. Supported for backward compatibility; prefer `ERRAGENT_APP_ID`/`ERRAGENT_APP_SECRET` for new integrations. |
| `ERRAGENT_SERVICE` | Stable service name reported with every event |
| `ERRAGENT_LOCAL_URL` | Local-dev daemon URL (e.g. `http://127.0.0.1:8765`), set only when running `erragent serve` |
| `ERRAGENT_LOCAL_ONLY` | Defaults to local-only whenever `ERRAGENT_LOCAL_URL` is set (see below). Set to `false` to also install the cloud handler alongside it. |
| `ERRAGENT_READ_SECRET` | Optional. This app's **read** secret, only needed to read its own incidents back (see "Reading your app's own incidents"). Not the ingest secret. |
| `ERRAGENT_TIMEOUT_SECONDS` | HTTP timeout for delivery and reads (default `30`) |

When `ERRAGENT_LOCAL_URL` is set, `erragent.install()` installs **only** the local handler by
default, not the cloud one — the local daemon already forwards every error it handles to the
cloud itself (tagged `local_dev=true`), so incident visibility in the console isn't lost. Installing
both would report each local error twice: once via the daemon (works, since it has the real local
file content) and once via a direct cloud report that's guaranteed to fail analysis, since the
cloud pipeline can't fetch an uncommitted local-only fix from GitHub. Set `ERRAGENT_LOCAL_ONLY=false`
if you want the direct cloud handler installed too anyway (e.g. to keep streaming non-error log
lines to the shared Live Console during local dev).

## Auto-attaching structured context

Instead of hand-building a context dict at every log call site, wrap the surrounding operation
once:

```python
import erragent

with erragent.context(workflow_name="ingest", request_id=req_id, node="fetch_docs"):
    logger.info("starting fetch")   # automatically carries workflow_name/request_id/node
    ...
    logger.error("fetch failed")     # same context, no dict to repeat
```

`erragent.context` is built on `contextvars`, so it survives `asyncio.create_task` boundaries
created inside the `with` block. It also works as a decorator:

```python
@erragent.context(node="fetch_docs")
async def fetch_docs(request_id: str):
    ...
```

## Reporting a pre-formed incident

If your app already has a structured error (not a log line you want streamed), use
`report_incident` instead of the logging handler:

```python
await erragent.report_incident(
    error_message="Document download failed",
    stack_trace=traceback.format_exc(),
    repository="org/repo",
    metadata={"route": "/download"},
)

# fire-and-forget variant for request handlers that can't await
erragent.report_incident_nowait(error_message="...", stack_trace="...")
```

## Reading your app's own incidents

Everything above reports *to* errAgent. If your app also wants to look at what errAgent holds about **itself** (an admin tool, a
status page, an overnight digest), use the read functions:

```python
incidents = await erragent.list_incidents(since="24h", status="open", limit=10)
detail = await erragent.get_incident(incidents[0]["id"])   # + stack trace, suggested fix, operational metadata
deploy = await erragent.latest_deploy()                    # latest Render deploy, if one is configured for the app
logs = await erragent.list_logs(level="warn", since="6h", contains="timeout", limit=50)   # recent log lines, oldest first
```

`list_logs` reads errAgent's live log buffer, which is held in memory: it covers what arrived since errAgent last restarted (up to
the buffer size per service), not a full history. The result says how far back it reaches (`oldest_buffered`) so "nothing matched"
can be told apart from "the buffer doesn't go back that far". `level` is a minimum severity, `request_id` follows one request, and
log context is filtered through the same allowlist as an incident's metadata.

Reads use their own credential, `ERRAGENT_READ_SECRET` (with the same `ERRAGENT_URL` and `ERRAGENT_APP_ID`). It is **not** the ingest
secret: the ingest secret sits in every reporting app's environment and must not double as a key to read data. An errAgent operator
issues it per app:

```bash
python -m backend.scripts.manage_app_read_access services                   # which service names incidents are filed under
python -m backend.scripts.manage_app_read_access enable --app-id myapp --service myapp [--render-service-id srv-...] [--create]
python -m backend.scripts.manage_app_read_access rotate  --app-id myapp
python -m backend.scripts.manage_app_read_access disable --app-id myapp
```

The secret is shown once and stored only as a hash. It is scoped on the server to the app's team and the service names written
on its record, so it can only ever read **that app's** incidents (another app's id is a 404, the same as one that doesn't exist).
Reads work in local-dev mode too (`ERRAGENT_LOCAL_URL` only switches off *reporting* to the cloud).

What comes back is small and content-free: id, status, the first line of the message, a truncated stack trace, the AI analysis
summary, fix status, and a short allowlist of operational metadata. Anything else an incident carried (a whole conversation state,
for example) is dropped server-side. Incident text is still data written by whatever failed, so if you pass it to a language model,
treat it as **untrusted input**, never as instructions. Failures raise `ErrAgentReadError` (with `status_code`, or `None` when
errAgent can't be reached) and never include the secret.

## Local-dev remediation

```bash
pip install "erragent-sdk[local]"
```

Easiest: run the daemon and your app together in one terminal with `erragent dev`, which
starts `erragent serve` as its own process (so your app's `--reload` only ever restarts your
app, never the daemon), auto-sets `ERRAGENT_LOCAL_URL` for it, and stops the daemon when the
app exits or you Ctrl+C:

```bash
erragent dev --root . -- uvicorn app:app --reload
```

Or run them separately if you'd rather — start the daemon in its own terminal:

```bash
erragent serve --root .
```

and set `ERRAGENT_LOCAL_URL=http://127.0.0.1:8765` (the daemon's default port) yourself before
starting your app.

Either way: when an error occurs locally, the daemon reads the relevant source file directly
off disk, sends it to errAgent for analysis, and prompts you in the terminal to approve or
decline the proposed fix before writing anything to disk. See the main
[README](../README.md) for the full remediation/HITL model this participates in.
