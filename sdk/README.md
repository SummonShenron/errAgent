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
| `ERRAGENT_LOCAL_ONLY` | `true` to skip cloud reporting entirely while `ERRAGENT_LOCAL_URL` is set (fully offline local sessions) |
| `ERRAGENT_TIMEOUT_SECONDS` | HTTP timeout for delivery (default `30`) |

When both `ERRAGENT_URL` and `ERRAGENT_LOCAL_URL` are configured, `erragent.install()` attaches
**both** a cloud handler and a local handler by default — local-dev sessions keep streaming into
errAgent's Live Console exactly as they do today, while the local daemon additionally gets the
same stream for file-aware remediation. Set `ERRAGENT_LOCAL_ONLY=true` to opt out of cloud
reporting during local development.

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
erragent serve --root .
```

Then set `ERRAGENT_LOCAL_URL=http://127.0.0.1:8765` (the daemon's default port) before starting
your app. When an error occurs locally, the daemon reads the relevant source file directly off
disk, sends it to errAgent for analysis, and prompts you in the terminal to approve or decline
the proposed fix before writing anything to disk. See the main [README](../README.md) for the
full remediation/HITL model this participates in.
