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
| `ERRAGENT_TIMEOUT_SECONDS` | HTTP timeout for delivery (default `30`) |

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
