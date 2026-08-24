# errAgent

errAgent is an incident-response workspace for collecting application errors, analyzing incidents, proposing safe remediations, validating fixes with focused tests, and monitoring operational health. It combines a FastAPI backend, a Clerk-authenticated React console, MongoDB persistence, GitHub integration, and Patchy, a human-in-the-loop operations assistant.

Patchy can recommend and coordinate work, but it does not silently approve changes, execute arbitrary shell commands, browse arbitrary URLs, or merge code without an operator decision.

## What It Does

### Incident ingestion

Target applications send structured logs and errors to errAgent. Error events become persisted incidents; informational and warning events remain operational logs.

Supported ingestion surfaces include:

- `POST /api/v1/logs` for structured application logs
- `POST /api/v1/client-errors` for sanitized browser errors sent through a trusted app backend proxy
- `POST /api/v1/webhooks/ingest` for machine-to-machine incident payloads
- Sentry, Vercel, and Render webhook adapters
- In-memory live log streaming through the console

Incident records include service identity, environment, error message, stack trace, repository, metadata, timestamps, fingerprints, and lifecycle status. Recent duplicate events are deduplicated by fingerprint.

### Health and operations monitoring

errAgent monitors registered services and stores health snapshots for operational history. The console exposes:

- Health checks with latency and HTTP status
- Production operations status
- Render service and deployment status
- Active, open, resolved, and closed incident views
- Recent in-memory logs filtered by service and severity
- Combined diagnostics across health, incidents, and error logs
- Scheduled health monitoring with optional Discord alerts for critical outages

### Incident analysis and remediation

For incidents with repository context, errAgent can:

1. Fetch the relevant source and branch context from GitHub.
2. Ask Gemini for structured root-cause analysis.
3. Produce a constrained remediation proposal.
4. Validate patch size, file scope, snippets, and content hashes.
5. Create a hotfix branch and pull request only after operator approval.
6. Record remediation and audit history in MongoDB.

Generated changes are restricted by policy and are never applied directly to `main`.

### Regression test workflow

Patchy supports a reviewable incident-to-test pipeline:

```text
incident
  -> analysis and remediation
  -> operator approves hotfix
  -> hotfix branch and pull request
  -> test generate <incident-id>
  -> operator reviews generated test
  -> operator approves test commit
  -> test plan <incident-id>
  -> operator approves GitHub Actions execution
  -> test status <test-plan-id>
  -> operator merges only after tests pass
```

Commands:

```text
test generate <incident-id>
test approve <generated-test-id>
test plan <incident-id>
test run <test-plan-id>
test status <test-plan-id>
test guide <incident-id>
```

The generated test is validated for syntax, assertions, file naming, side effects, and overwrite safety. Test plans are built from real repository context and commands are checked against discovered test files. Execution happens in the target repository's GitHub Actions workflow, not on the errAgent host. A linked test plan must be persisted as `passed` before the merge endpoint accepts a hotfix merge.

### HITL proposals and guided workflows

Patchy separates planning from execution. Actions that contact registered services, commit files, dispatch CI, or run synthetic checks create proposals in `patchy_proposals`.

The operator sees the exact action, destination, branch, risk, and relevant content before selecting **Approve & Run** or **Disapprove**.

Available workflows include:

- Read-only service probes
- Registered synthetic health assertions
- Staged or explicitly approved production Sonic questions
- Two-step health and latency verification
- Deterministic investigation plans
- Guided incident-to-test workflows

Failures and rejections stop the relevant workflow. Patchy does not self-approve a proposal.

### Pentest Sweep ###

Patchy supports a safe, bounded pentest sweep workflow for registered applications.
Sweeps are human-approved and operate within strict limits:

Synthetic-only fuzzing for public endpoints

Authenticated Browser Agent fuzzing for Clerk-protected admin endpoints

No arbitrary scanning, crawling, or shell execution

No destructive side effects

All actions logged and reviewable

Operators initiate sweeps through Patchy Terminal:

text
pentest sweep <alias> [target]
Supported targets:

public — synthetic fuzzing

admin_leads — authenticated admin leads fuzzing

admin_content — authenticated admin content fuzzing

admin_all — both admin fuzzers

full — public + admin fuzzers

Sweeps create a proposal that must be explicitly approved.
Findings are persisted as incidents with full context.

### Reusable synthetic flow plans

Flow plans test structured HTTP journeys without UI automation or arbitrary shell execution. They work for registered services such as BTY and SAAPP and can be reused for future applications.

Simple syntax:

```text
flow define bty health simple GET /api/health ASSERT 200
flow list bty
flow run flow_<id>
```

Supported actions and assertions:

- `GET`, `POST`, `PUT`, `PATCH`, and `DELETE` requests
- `assert_status`
- `assert_json`
- `assert_body`
- JSON, header, and cookie value capture
- `{{variable}}` reuse in later URLs, bodies, and headers

Flow rules include site-relative URLs, registered service base URLs, bounded body size, a maximum of 12 steps, and approval before execution. Execution stops at the first failed step and reports each step's status, HTTP code, and latency.

For Clerk-protected admin routes, a flow can use an environment-backed bearer token:

```text
flow define bty admin-schedule simple GET /api/admin/schedule ASSERT 200 --auth '{"type":"env_bearer","env":"ERRAGENT_BTY_ADMIN_TOKEN"}'
```

The token is read at execution time and is never stored in MongoDB or the flow definition. Only `ERRAGENT_*` environment variables are permitted for this purpose.

### errAgent self-monitoring

errAgent captures its own unhandled server errors as `erragent` incidents. Selected Patchy infrastructure failures, including GitHub, Gemini, timeout, network, and database failures, are also eligible for self-monitoring.

Self-monitoring is loop-safe:

- It writes directly to MongoDB instead of calling the HTTP ingest endpoint.
- It deduplicates repeated failures for a short window.
- It records request path, exception type, traceback, and source metadata.
- Normal command validation errors, missing IDs, and declined proposals remain terminal responses rather than incidents.

### Frontend error reporting

For a frontend-heavy target such as BTY, the browser should report errors to the target app's own FastAPI backend. That backend sanitizes and forwards the event to `POST /api/v1/client-errors` using the existing `x-ingest-secret` and optional `x-app-id` headers. The browser must never receive errAgent ingestion secrets.

Client error payloads can include the service, environment, release, route, error source, sanitized message, stack, and limited metadata. errAgent bounds payload sizes and redacts common credentials such as authorization values, cookies, passwords, secrets, tokens, and API keys before incident creation.

## Architecture

```text
Target apps / webhooks
        |
        v
FastAPI ingestion and API layer
        |
        +--> MongoDB incidents, logs, plans, proposals, audit records
        +--> LogBroker live events
        +--> GitHub and Render integrations
        +--> Gemini analysis and test planning
        |
        v
React + Clerk Patchy console
```

Important backend modules:

- `backend/app/app.py` — FastAPI application and API routes
- `backend/utils/app_utils.py` — ingestion, analysis pipeline, repository resolution, patch validation, self-monitoring helpers
- `backend/services/log_broker.py` — bounded in-memory log history and live subscribers
- `backend/services/patchy_terminal.py` — Patchy command router and guided workflows
- `backend/services/patchy_hitl.py` — proposal lifecycle and approvals
- `backend/services/patchy_flow_runner.py` — reusable synthetic flow validation and execution
- `backend/services/github_service.py` — GitHub API access, caching, branch and workflow operations
- `backend/services/patchy_test_generator.py` — regression test drafting
- `backend/services/patchy_test_planner.py` — focused test-plan drafting and command validation
- `backend/services/patchy_test_runner.py` — GitHub Actions dispatch and status tracking

## Local Development

### Prerequisites

- Python 3.11 or newer
- Node.js compatible with the frontend toolchain
- MongoDB, unless running only isolated tests
- Clerk credentials for the authenticated frontend and API
- GitHub token for repository-aware analysis and test workflows
- Gemini API key for LLM-backed analysis and test planning

### Install dependencies

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
cd frontend
npm install
cd ..
```

### Configure environment

Create `backend/.env` from your deployment or local secret manager. Do not commit credentials. Common settings include:

```env
MONGO_URI=<mongodb-connection-string>
GOOGLE_API_KEY=<gemini-api-key>
GITHUB_TOKEN=<github-token>
DEFAULT_TARGET_REPO=<owner/repository>
CLERK_JWKS_URL=<clerk-jwks-url>
SENTRY_WEBHOOK_SECRET=<sentry-secret>
ERRAGENT_INGEST_SECRET=<ingest-secret>
PATCHY_TEST_WORKFLOW=patchy-tests.yml
PATCHY_LLM_TIMEOUT_SECONDS=60
```

For the frontend, configure the Vite variables required by `frontend/src/main.tsx`, including:

```env
VITE_CLERK_PUBLISHABLE_KEY=<clerk-publishable-key>
```

Use a secret manager for deployed environments. Never put private Clerk keys, GitHub tokens, Gemini keys, Render keys, or bearer session tokens in frontend variables.

### Start locally

From the repository root:

```powershell
.\start-local.ps1
```

The helper starts:

- Backend: `http://127.0.0.1:8006`
- Frontend: `http://127.0.0.1:8086`

The backend API is available under `/api/v1`. The frontend requires a valid Clerk session.

### Run tests and build

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests -q
cd frontend
npm run build
```

## API Overview

### Health and monitoring

```text
GET  /health
POST /api/v1/health/check
GET  /api/v1/health/full
GET  /api/v1/health/services
GET  /api/v1/events
```

### Incidents and remediation

```text
GET    /api/v1/incidents
GET    /api/v1/incidents/{incident_id}
POST   /api/v1/incidents
DELETE /api/v1/incidents/{incident_id}
POST   /api/v1/incidents/{incident_id}/approve-hotfix
POST   /api/v1/incidents/{incident_id}/merge-hotfix
POST   /api/v1/incidents/{incident_id}/reanalyze
```

### Patchy

```text
POST /api/v1/patchy/command
GET  /api/v1/patchy/proposals
POST /api/v1/patchy/proposals/{proposal_id}/approve
POST /api/v1/patchy/proposals/{proposal_id}/decline
```

### Ingestion and replay

```text
POST /api/v1/logs
POST /api/v1/client-errors
POST /api/v1/webhooks/ingest
POST /api/v1/webhooks/sentry
POST /api/v1/webhooks/vercel
POST /api/v1/webhooks/render
POST /api/v1/replay
GET  /api/v1/replay
GET  /api/v1/replay/runs
```

Protected routes require the authentication or ingestion headers described by the deployment configuration. See [PATCHY_TERMINAL.md](PATCHY_TERMINAL.md) for the complete command reference and [NEW_APP_ONBOARDING.md](NEW_APP_ONBOARDING.md) for integrating another application.

## Onboarding a Target App

A target app generally needs no Patchy-specific application code beyond structured error reporting. The integration contract is:

1. Install the errAgent logging handler.
2. Set `ERRAGENT_URL`, `ERRAGENT_INGEST_SECRET`, and a stable service name.
3. Send structured error events to `/api/v1/logs`.
4. Register its health URL and Patchy alias in errAgent.
5. Map its service name to its GitHub repository when using analysis or test workflows.
6. Add a repository-owned `workflow_dispatch` test workflow when using Patchy CI execution.

For browser observability, add a target-app backend proxy that accepts sanitized frontend errors and forwards them to `/api/v1/client-errors`. Never embed `ERRAGENT_INGEST_SECRET` in browser JavaScript.

Keep credentials in the target app's environment or secret manager. Do not send credentials in log messages or metadata.

## Security Boundaries

- Clerk authenticates operators and protects the workspace.
- Ingestion endpoints require configured machine credentials.
- Patchy commands use an allowlist.
- Synthetic flows require registered service bases and site-relative paths.
- External actions require explicit operator approval.
- Generated tests and patches are validated before persistence or commit.
- Test execution is delegated to the target repository's CI environment.
- Private tokens are resolved from backend environment variables and are not stored in flow plans.
- Self-monitoring bypasses HTTP re-ingestion to prevent recursive failure loops.

## Project Documentation

- [PATCHY_TERMINAL.md](PATCHY_TERMINAL.md) — complete Patchy command reference and workflow details
- [NEW_APP_ONBOARDING.md](NEW_APP_ONBOARDING.md) — target application integration runbook
- [backend/tests](backend/tests) — backend behavior and policy tests
- [frontend/src](frontend/src) — authenticated console and UI components

## Current Limitations

- The live log broker is in-memory and is not a durable event bus.
- MongoDB is required for persisted incidents, plans, proposals, and audit history.
- LLM-backed operations depend on external Gemini availability and configured timeouts.
- GitHub Actions workflows remain repository-owned and must be configured per target repository.
- Clerk session-token automation is not enabled; authenticated synthetic flows use a backend environment token.
- UI automation is intentionally out of scope for HTTP synthetic flows.

## License

No license is currently specified for this repository.
