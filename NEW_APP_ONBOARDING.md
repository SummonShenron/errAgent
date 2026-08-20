# New Application Onboarding

Use this runbook when integrating a new target application with errAgent and Patchy.

The goal is to make the app observable, incident-aware, testable, and available to Patchy's production operations commands.

## Integration checklist

Copy this checklist into the onboarding issue or pull request:

```text
[ ] Target app identity and service alias chosen
[ ] Target app logging handler installed
[ ] Target app exception hooks verified
[ ] errAgent ingest credentials configured
[ ] errAgent incident payload verified
[ ] errAgent service registry updated
[ ] Health endpoint registered and reachable
[ ] Render service ID configured, if deployed on Render
[ ] GitHub repository and default branch verified
[ ] GitHub Actions test workflow configured, if using Patchy test execution
[ ] Patchy aliases and command help updated
[ ] Frontend service labels/filters updated, if needed
[ ] Local integration test completed
[ ] Production smoke test completed
[ ] Secrets and credentials stored only in environment/secret manager
```

## 1. Define the application contract

Record these values before editing code:

| Field | Example | Required |
| --- | --- | --- |
| Display name | `SAAPP Widget` | Yes |
| Patchy alias | `saapp` | Yes |
| Runtime log service name | `SAAPP` | Yes |
| Production URL | `https://saapp.onrender.com` | Yes |
| Health path | `/api/health` | Yes |
| Environment | `production` | Yes |
| GitHub repository | `SummonShenron/SAAPP` | Yes for AI/test workflows |
| GitHub base branch | `main` | Yes for AI/test workflows |
| Render service ID | `srv-...` | If using Render |

Keep the display name, Patchy alias, log service name, and repository name consistent. Most filtering behavior depends on these values matching.

## 2. Install target-app logging

The integration handler lives at:

[integrations/erragent_handler.py](integrations/erragent_handler.py)

Install it in the target application startup path:

```python
import logging
from integrations.erragent_handler import install_erragent_logging

logger = logging.getLogger("target-app")
install_erragent_logging(logger)
```

Configure the target application environment:

```env
ERRAGENT_URL=https://<erragent-host>
ERRAGENT_INGEST_SECRET=<shared-ingest-secret>
ERRAGENT_SERVICE=<runtime-log-service-name>
ERRAGENT_TIMEOUT_SECONDS=30
```

Important logging checks:

- Install the handler on the logger that actually receives application events.
- If the app uses `propagate = False`, install it on that named logger rather than relying on the root logger.
- Verify `logger.error(...)` and `logger.exception(...)` arrive as `error` events.
- Verify uncaught process, thread, and asyncio exceptions are captured.
- Do not place credentials in log context or exception messages.
- The handler queues events and retries delivery; it must not block the request path indefinitely.

## 3. Verify the errAgent ingestion contract

The target app sends structured events to:

```text
POST /api/v1/logs
```

Required event shape:

```json
{
  "service": "SAAPP",
  "level": "error",
  "message": "Document download failed",
  "timestamp": 1724070000000,
  "context": {
    "logger": "target-app",
    "module": "downloads",
    "function": "download_document",
    "line": 42
  }
}
```

Use the `x-ingest-secret` header. Confirm that:

- the request is accepted with HTTP `202`
- the event appears in Live Console
- an error event creates or updates an incident
- the incident contains useful stack trace and metadata
- duplicate events are deduplicated as expected
- non-error informational logs do not create incidents

Relevant backend surface:

- [backend/app/app.py](backend/app/app.py): `/api/v1/logs` ingestion and incident promotion
- [backend/utils/app_utils.py](backend/utils/app_utils.py): ingestion, payload normalization, and incident helpers
- [backend/services/log_broker.py](backend/services/log_broker.py): live in-memory delivery

## 4. Register the service for health monitoring

Add the application to `SERVICES` in:

[backend/utils/app_utils.py](backend/utils/app_utils.py)

Example:

```python
{
    "name": "New App",
    "url": "https://new-app.onrender.com",
    "health_path": "/api/health",
}
```

Then verify:

- `health all` includes the new app
- `health <alias>` returns only the new app
- scheduled health monitoring includes it
- startup baseline logic does not create a false cold-start alert
- `/api/v1/health/services` exposes the service
- the health endpoint returns a stable JSON response and appropriate HTTP status

The service name must match the health result name used by `run_service_health_checks`.

## 5. Add Patchy aliases and command support

Update the alias maps in:

[backend/services/patchy_terminal.py](backend/services/patchy_terminal.py)

At minimum, update:

- `_SERVICE_ALIASES` for health commands
- `_LOG_SERVICE_ALIASES` for log filters
- `COMMAND_HELP` descriptions if a new command is introduced

Then verify:

```text
health <alias>
logs <alias> error
ops status <alias>
render status <alias>
```

For production operations, update the provider adapter as well:

- [backend/services/production_ops.py](backend/services/production_ops.py): service aliases and deployment metadata
- [backend/services/render_ops.py](backend/services/render_ops.py): Render service ID environment mapping

## 6. Configure Render, if applicable

For a Render-hosted app, add these only to the errAgent backend environment or local `backend/.env`:

```env
RENDER_API_KEY=<scoped-render-api-key>
ERRAGENT_NEWAPP_RENDER_SERVICE_ID=srv-...
```

Use the Patchy alias in the variable name. Do not put the Render API key in the target app unless the target app itself needs it.

Verify:

```text
render status <alias>
```

Expected output includes service name, suspension state, latest deploy status, commit, and deploy ID.

If the key or service ID is absent, Patchy should report `NOT_CONFIGURED` without making a request.

## 7. Configure GitHub and test validation

Record the target repository in the incident payload or configure the default repository through `DEFAULT_TARGET_REPO`.

For Patchy's test workflow:

```env
PATCHY_TEST_WORKFLOW=<existing-workflow-file.yml>
```

There is no invented default workflow. The target repository must already contain the workflow under `.github/workflows/` and support:

- `workflow_dispatch`
- a `test_commands` input
- checkout of the requested hotfix branch
- `pytest` installed in the CI environment, for example `python -m pip install pytest`
- focused `python -m pytest ...` execution inside repository CI
- a published GitHub Actions result

GitHub access is read-only for planning and requires workflow-dispatch permission for execution. The test command lifecycle is:

```text
1. Approve the hotfix on the incident detail page so its head branch exists.
2. test generate <incident-id>
3. Review the generated test file and its rationale.
4. test approve <generated-test-id>
5. Approve the generated-test commit proposal.
6. Patchy displays the next step: test plan <incident-id>
7. test plan <incident-id>
8. Review the test plan and record its test-plan ID.
9. test run <test-plan-id>
10. Approve the GitHub Actions execution proposal.
11. test status <test-plan-id>
12. Merge only after the status is passed.
```

The `test approve <generated-test-id>` command creates the commit proposal. After the operator approves that proposal and the test file is committed to the hotfix branch, the next Patchy command is:

```text
test plan <incident-id>
```

The plan must scan the updated hotfix branch so it can include the generated regression test. Patchy uses `python -m pytest` commands so execution does not depend on a standalone `pytest` executable being on `PATH`.

For a direct smoke check after configuration:

```text
test plan <incident-id>
test run <test-plan-id>
test status <test-plan-id>
```

Patchy must inspect the hotfix branch, not only `main`, when a remediation `head_branch` exists.

## 8. Add incident-specific test coverage

Update or add tests in:

[backend/tests](backend/tests)

At minimum cover:

- service registration and health filtering
- log service filtering
- error-to-incident promotion
- Render configuration missing and configured paths
- GitHub repository/test-file allowlisting
- generated-test path and syntax validation
- generated-test approval cannot overwrite an existing file
- test-plan branch provenance
- merge blocked until the linked test plan is `passed`

For the target application repository, ensure there is a regression test for the new app's most important failure path.

## 9. Frontend verification

The main frontend surfaces are:

- [frontend/src/App.tsx](frontend/src/App.tsx): service and incident presentation
- [frontend/src/components/LiveConsole.tsx](frontend/src/components/LiveConsole.tsx): live logs
- [frontend/src/components/PatchyTerminal.tsx](frontend/src/components/PatchyTerminal.tsx): Patchy commands and proposals
- [frontend/src/index.css](frontend/src/index.css): console and service styling

Update frontend code only when the new app needs a dedicated label, filter, icon, or display behavior. Generic incident/log rendering should not require a component change.

Check desktop and mobile layouts after adding longer service names or new command output.

## 10. End-to-end verification

Run this sequence with a controlled test event:

```text
1. Start the target app with ERRAGENT_* variables configured.
2. Emit one info log and one error log.
3. Confirm both arrive in Live Console.
4. Confirm only the error creates an incident.
5. Run health <alias>.
6. Run logs <alias> error.
7. Run ops status <alias>.
8. Run render status <alias>, if applicable.
9. Confirm the incident's repository and service metadata.
10. Generate and review the AI analysis.
11. Approve a hotfix only in a controlled environment.
12. Run the validated hotfix test workflow before merging.
```

Production acceptance requires:

- no credential values in logs or committed files
- no false startup incident
- no unbounded log queue growth
- no duplicate handler installation
- no unapproved network or repository mutation
- a successful health check
- a successful controlled error ingestion
- a documented rollback or disable path

## Files commonly updated

| Area | File | Typical change |
| --- | --- | --- |
| Target app | Target app startup/config | Install handler and set `ERRAGENT_*` variables |
| Target app | Target app health route | Provide stable health endpoint |
| Integration | [integrations/erragent_handler.py](integrations/erragent_handler.py) | Usually no change; update only for shared handler behavior |
| Service registry | [backend/utils/app_utils.py](backend/utils/app_utils.py) | Add service URL and health path |
| Patchy commands | [backend/services/patchy_terminal.py](backend/services/patchy_terminal.py) | Add aliases/help if needed |
| Production status | [backend/services/production_ops.py](backend/services/production_ops.py) | Add service alias and metadata mapping |
| Render | [backend/services/render_ops.py](backend/services/render_ops.py) | Add Render service ID environment mapping |
| Incident schema | [backend/schemas/incident_schemas.py](backend/schemas/incident_schemas.py) | Change only if new incident fields are required |
| GitHub/test flow | Target repo `.github/workflows/` | Provide the repository-owned test workflow |
| Backend tests | [backend/tests](backend/tests) | Add integration and policy coverage |
| Frontend | [frontend/src/App.tsx](frontend/src/App.tsx) | Only for app-specific presentation |
| Frontend | [frontend/src/components/PatchyTerminal.tsx](frontend/src/components/PatchyTerminal.tsx) | Only for app-specific Patchy controls |
| Documentation | [PATCHY_TERMINAL.md](PATCHY_TERMINAL.md) | Add app-specific operations or workflow notes |

## Secrets and rollback

Never commit:

- `ERRAGENT_INGEST_SECRET`
- `RENDER_API_KEY`
- `GITHUB_TOKEN`
- `GOOGLE_API_KEY`
- MongoDB credentials
- Clerk credentials
- Discord webhook URLs

Use the target platform's secret manager for production and `backend/.env` only for local development. Rotate any credential that appears in logs, screenshots, chat, commits, or issue descriptions.

To disable an integration quickly:

1. Remove or disable the target app's `ERRAGENT_URL` or `ERRAGENT_INGEST_SECRET`.
2. Disable its health registry entry or scheduled monitoring if health checks are harmful.
3. Revoke exposed integration credentials.
4. Keep the app running independently while errAgent ingestion is repaired.
5. Record the outage and restoration in the incident audit trail.
