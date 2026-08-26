synthetic [bty|saapp]
# Patchy Terminal

Patchy Terminal is errAgent's authenticated operations console. It uses a strict command allowlist for immediate diagnostics and a human-in-the-loop (HITL) proposal flow for executable actions. It does not expose an arbitrary shell.

## Open the terminal

Sign in to errAgent and select **Patchy** beside **Replay Workflow** and **Console**.

## Validated hotfix workflow

This is the recommended incident-resolution workflow when a code hotfix is proposed:

```text
incident arrives
	-> Patchy analyzes the incident and drafts a remediation
	-> operator approves the hotfix on the incident detail page
	-> errAgent creates the hotfix branch and pull request
	-> Patchy drafts a regression test on that hotfix branch
	-> operator reviews and approves the new test commit
	-> Patchy creates and runs a focused test plan in GitHub Actions
	-> operator reviews the CI result
	-> operator merges the hotfix from the incident detail page
```

### Step 1: Create the hotfix branch

On the incident detail page, review the AI analysis and remediation diff. Use the normal **Approve Hotfix** action. This creates the remediation branch and pull request. Do this before generating a test because Patchy needs the remediation's `head_branch` to place the regression test beside the fix.

### Step 2: Generate a regression test

Copy the incident ID and run:

```text
test generate <incident-id>
```

Patchy reads the incident, hotfix diff, changed Python files, and existing tests through GitHub. Gemini drafts one new focused pytest file. Patchy validates that the file is syntactically valid, contains assertions, uses no shell/network/credential side effects, and will not overwrite an existing file.

The response displays a generated-test ID and the exact proposed file content:

```text
Generated test ID: generated_test_...
test approve generated_test_...
```

### Step 3: Approve the test commit

Review the complete generated file in the terminal. If it is appropriate, run:

```text
test approve <generated-test-id>
```

This creates an approval proposal. Select **Approve & Run**. Only then is the new test file committed to the existing hotfix branch. It is never committed directly to `main`, and an existing file cannot be overwritten.

### Step 4: Create and execute the test plan

After the test commit completes, create a fresh plan:

```text
test plan <incident-id>
```

The plan scans the updated hotfix branch, includes the hotfix diff against the base branch, and displays a persisted test-plan ID:

```text
Test plan ID: testplan_...
test run testplan_...
```

Review the recommended pytest commands, then create the execution proposal:

```text
test run <test-plan-id>
```

Select **Approve & Run**. Patchy dispatches the configured repository-owned GitHub Actions workflow on the exact hotfix branch. Patchy does not run a local shell.

### Step 5: Verify CI passed

Dispatching CI only queues the run. Check the actual result with:

```text
test status <test-plan-id>
```

Only a persisted `passed` status satisfies the merge gate. Queued, running, failed, cancelled, or missing runs do not.

### Step 6: Merge normally

Return to the incident detail page and use the normal **Merge Hotfix** action. The backend checks the remediation's linked test plan and rejects the merge unless its status is `passed`. The final merge remains an explicit operator action.

### Required IDs

The workflow uses three different identifiers:

| Identifier | Created by | Used for |
| --- | --- | --- |
| Incident ID | Incident ingestion | `test generate <incident-id>` and `test plan <incident-id>` |
| Generated test ID | `test generate` | `test approve <generated-test-id>` |
| Test plan ID | `test plan` | `test run` and `test status` |

Do not use the incident ID where a generated-test ID or test-plan ID is requested.

## Diagnostic commands

These commands run immediately because they are read-only operations implemented by errAgent:

```text
help
health [all|bty|saapp]
ops status [all|bty|saapp]
render status [all|bty|saapp]
incidents
list incidents [all|open|resolved]
logs [all|bty|saapp|erragent] [info|warn|error]
diagnostics
explain <incident-id>
summarize <incident-id>
confirm deployed <incident-id>
test plan <incident-id>
test run <test-plan-id>
test status <test-plan-id>
test generate <incident-id>
test approve <generated-test-id>
test analyze <test-plan-id>
investigate [incident-id]
verify [bty|saapp]
plan verify [bty|saapp] stability
plan investigate incidents
next [plan-id]
pentest sweep [bty|saapp]
discover endpoints <serviceAlias|url>
clear
```

## HITL probe flow

The first approved-action commands are:

```text
probe bty
probe saapp
```

A probe follows this lifecycle:

```text
DRAFTED -> AWAITING_APPROVAL -> RUNNING -> SUCCEEDED | FAILED
```

1. Patchy creates a proposal in MongoDB's `patchy_proposals` collection.
2. The terminal displays the exact HTTP method, URL, timeout, and risk classification.
3. Nothing executes while the proposal is awaiting approval.
4. The operator selects **Approve & Run**.
5. The backend revalidates the proposal against policy.
6. A read-only GET request runs against the registered service health endpoint.
7. The approver, timestamps, HTTP result, elapsed time, and response are stored for audit.
8. A proposal cannot be approved twice.

## Clarification flow

Patchy can pause when an action is missing required context. For example:

```text
probe
```

returns a clarification request asking which registered service to use. The operator can choose BTY Fitness or SAAPP Widget from the terminal. Patchy does not guess, create a proposal, or accept an arbitrary URL. The selected value is submitted through the normal command parser and policy checks.

This pattern is the safe foundation for more agentic behavior: ask for missing information, constrain the possible answers, record the decision, then continue through the existing HITL flow.

## Synthetic workflow foundation

The first synthetic adapter is intentionally narrow:

```text
synthetic [bty|saapp]
```

It proposes an approved GET against a registered service health endpoint and asserts that the response is successful and completes within the timeout. It does not yet provide a staging sandbox or execute arbitrary user prompts. The endpoint remains visible in the approval panel, and the operator must approve it before the request runs.

The next expansion is a staging-only declarative workflow adapter for structured actions and assertions. Natural-language requests should be translated into that allowlisted structure before execution, never directly into shell, browser, or arbitrary HTTP commands.

The first staging assistant adapter uses:

```env
ERRAGENT_SONIC_SYNTHETIC_URL=https://<sonic-staging-host>/<question-route>
ERRAGENT_SONIC_SYNTHETIC_ENV=staging
ERRAGENT_SONIC_SYNTHETIC_BEARER_TOKEN=<optional-service-bearer-token>
```

Once Sonic exposes a staging question endpoint accepting `{"question": "..."}` and returning an answer field, use:

```text
synthetic ask sonic "What is my balance?"
```

Patchy displays the exact question, staging URL, and non-empty-answer assertion before approval. The adapter rejects production environments, non-HTTPS URLs, missing configuration, and questions longer than 2000 characters.

If staging is unavailable and the endpoint is explicitly safe for production read-only checks, enable the separate production mode in the errAgent backend environment:

```env
ERRAGENT_ALLOW_PRODUCTION_SYNTHETICS=true
ERRAGENT_SONIC_SYNTHETIC_ENV=production
ERRAGENT_SONIC_SYNTHETIC_URL=https://<sonic-production-host>/<read-only-question-route>
ERRAGENT_SONIC_SYNTHETIC_BEARER_TOKEN=<optional-service-bearer-token>
```

Then the command must include the explicit flag:

```text
synthetic ask sonic "What is my balance?" --production-read-only
```

Both the environment flag and command flag are required. The proposal displays `production_read_only` risk and still requires approval. The endpoint must be read-only, HTTPS, side-effect-free, bounded by timeout and response limits, and must not expose credentials or perform account mutations. Leave `ERRAGENT_ALLOW_PRODUCTION_SYNTHETICS` unset unless this production route has been reviewed. If the Sonic endpoint requires authentication, set `ERRAGENT_SONIC_SYNTHETIC_BEARER_TOKEN`; Patchy forwards it as an outbound `Authorization: Bearer ...` header.

BTY is a normal website, not a conversational assistant. Its current synthetic coverage is limited to registered health checks. Website workflows such as signing in, filling a form, submitting a request, and asserting the resulting page require a separate staging-only Playwright adapter. That adapter should be added only after the staging URL, test account strategy, selectors, and expected assertions are defined.

## Synthetic flow plans

Reusable multi-step HTTP user journeys for any registered service — signup, login, contact form, page loads — defined once and re-runnable under HITL approval.

```text
flow define <bty|saapp> <name> <json-actions>
flow list [bty|saapp]
flow run <flow-id>
probe validation <bty|saapp>
validate email <bty|saapp> <path>
```

For simple flows, use the easier compact form instead of escaped JSON:

```text
flow define bty health simple GET /api/health ASSERT 200
flow define bty admin-schedule simple GET /api/admin/schedule ASSERT 200
flow define bty contact simple POST /api/contact BODY '{"name":"Patchy","email":"test@example.com"}' ASSERT 201
```

Compact syntax supports `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `ASSERT <status>`, and `BODY <json>`. Use the JSON form below when you need captures, custom headers, JSON assertions, or multiple fields that are awkward to type inline.

For bounded staging-only input validation probing, define a `fuzz` step in JSON form:

```text
flow define bty email-validation "[{\"type\":\"fuzz\",\"url\":\"/api/contact\",\"field\":\"email\",\"body\":{\"email\":\"{{fuzz_value}}\",\"message\":\"test\"},\"catalog\":\"email\",\"expect_status\":422}]"
```

Fuzz steps support up to eight bounded catalog values, only use `POST`, and require a validation status (`400`, `401`, `403`, `404`, `409`, or `422`). They also check response text for raw `<script>`, `javascript:`, `onerror=`, and `onload=` reflection by default. Set `"assert_sanitized": false` only when a flow has a documented reason to inspect a response without that check. A `2xx` response is reported as an unexpected success; a `5xx` response is reported as a server error; unsafe reflected markup is reported separately as a sanitization failure.

By default, fuzz flows require `ERRAGENT_BTY_SYNTHETIC_ENV=staging`. For a production deployment with a verified synthetic safety contract, the target app must honor these headers on every request:

```text
X-ErrAgent-Synthetic: true
X-ErrAgent-Run-Id: <unique-run-id>
X-ErrAgent-Flow-Id: <flow-id>
```

The target app must prevent real side effects for synthetic requests: no real email delivery, appointment creation, calendar mutation, or lead persistence. After that behavior is deployed and reviewed, explicitly opt in from errAgent with `ERRAGENT_BTY_SYNTHETIC_MUTATIONS_SAFE=true`. Patchy then permits the bounded fuzz flow against the registered production base URL and records the run ID for correlation. This flag is a contract, not a bypass: without target-app enforcement, production fuzzing remains blocked.

Run all saved validation flows for a service with one approval gate:

```text
probe validation bty
```

The audit runs a health check, then the registered fuzz flows, stops each flow at its first unexpected result, and returns per-case evidence. If malformed input receives a `2xx` response, Patchy automatically creates a deduplicated high-severity `input_validation_bypass` incident with the endpoint, status, run ID, submitted canary, and redacted response excerpt. If the response leaks MongoDB or driver details, it creates a high-severity `database_error_disclosure` incident instead. Patchy does not enumerate data or execute database expressions.

It does not automatically ask the LLM to modify code or create a PR; remediation remains a separate reviewed phase. Open the created incident to review the evidence, then use the normal analysis and HITL patch workflow.

For the common BTY booking email-validation check, no JSON is needed. Patchy supplies the other valid `AppointmentBooking` fields and varies only `email`, so a `422` specifically tests email validation:

```text
validate email bty /api/consultations
```

For the booking page, use:

```text
validate email bty /api/bookings
```

Synthetic booking requests include `X-ErrAgent-Synthetic: true`, `X-ErrAgent-Correlation-Id`, and `X-ErrAgent-Flow-Id`. BTY's existing synthetic dependency validates the complete request first, then suppresses booking persistence and notification email when the request is synthetic.

For the Home page consultation form, use the same shortcut with its actual endpoint:

```text
validate email bty /api/consultation
```

Patchy supplies BTY's required `full_name`, `coaching_preference`, and `primary_goal` fields and varies only `email`. This tests the Home form's `ConsultationLead.email` validation without creating a lead or sending a notification email.

This creates or reuses a bounded email-validation flow, then presents the normal approval card. The equivalent long form is also available:

```text
probe validation email bty /api/consultations
```

Example — a signup flow with a captured value reused later:

```text
flow define bty signup "[{\"type\":\"GET\",\"url\":\"/signup\"},{\"type\":\"POST\",\"url\":\"/api/signup\",\"body\":{\"email\":\"test@example.com\",\"password\":\"123456\"},\"expect_status\":201,\"capture\":{\"uid\":\"json.userId\"}},{\"type\":\"GET\",\"url\":\"/api/users/{{uid}}\"},{\"type\":\"assert_json\",\"has\":\"email\"}]"
```

Step types:

- `GET` / `POST` / `PUT` / `PATCH` / `DELETE` — site-relative `url` only (must start with `/`); optional `body` (JSON object), `headers`, `expect_status`, and `capture`
- `assert_status` — `{ "equals": 200 }` checks the previous response
- `assert_json` — `{ "has": "userId" }` or `{ "equals": {"field": "value"} }` (dot paths supported)
- `assert_body` — `{ "contains": "text" }` checks the raw response body

`capture` pulls a value from a response — `json.<field>`, `header.<name>`, or `cookie.<name>` — and later steps reuse it with `{{variable}}` templates in URLs, bodies, and headers. Execution stops at the first failed step and reports per-step status, HTTP code, and latency.

Safety rules: steps are capped at 12, URLs can only be paths on the flow's registered base URL (no cross-site requests), bodies are size-limited, and every run requires an approval card showing the step count and base URL before anything executes. Flow plans are stored per service and can be re-run repeatedly (`flow run <flow-id>`) without redefining them.

### Authenticated flows (Clerk admin routes)

Services like BTY gate admin routes behind Clerk-issued JWTs verified via JWKS (`Authorization: Bearer <token>`). Flows support this with an auth block declared at define time:

```text
flow define bty admin-schedule "[{\"type\":\"GET\",\"url\":\"/api/admin/schedule\"},{\"type\":\"assert_json\",\"has\":\"status\"}]" --auth "{\"type\":\"env_bearer\",\"env\":\"ERRAGENT_BTY_ADMIN_TOKEN\"}"
```

The token itself never goes into the flow document or Mongo — only the env var name. At execution, Patchy reads `ERRAGENT_BTY_ADMIN_TOKEN` from the errAgent backend environment and injects it as `Authorization: Bearer ...` on every request step (per-step headers can still override it).

Setup: create a synthetic admin user in BTY's Clerk dashboard, add its email to BTY's `ADMIN_EMAILS`, mint a long-lived session token for that user, and set it as `ERRAGENT_BTY_ADMIN_TOKEN` in errAgent's `.env`. Only `ERRAGENT_*` env vars are readable by flows, and the approval card shows which auth mode a flow uses before it runs. A `clerk_session_token` mode (minting tokens on demand via Clerk's Backend API) is reserved but intentionally disabled until a dedicated synthetic-user strategy is chosen.

## LLM evidence synthesis

Use:

```text
summarize <incident-id>
```

Patchy sends supplied incident details, existing analysis, recent error logs, and production status to the configured Gemini model. The model returns structured hypotheses, confidence, missing information, and one recommended read-only action. A deterministic validator rejects unsupported commands, and Patchy never executes the recommendation automatically.

When a hotfix is merged but the deployment system is not yet connected, an operator can record the missing fact with `confirm deployed <incident-id>`. This is an audit-visible assertion, not automatic deployment verification; a cloud deployment adapter should replace it when available.

## GitHub-aware test planning

Use:

```text
test guide <incident-id>
```

This guided mode keeps the current commands intact but auto-selects the next safe step in sequence:

1. `test generate <incident-id>`
2. `test approve <generated-test-id>`
3. `test plan <incident-id>`
4. `test run <test-plan-id>`
5. `test status <test-plan-id>`
6. `test analyze <test-plan-id>`

When a pending approval already exists, `test guide` resumes that approval card instead of creating duplicate work.

To avoid long-running generation hangs, guided test generation/planning uses a bounded timeout controlled by `PATCHY_LLM_TIMEOUT_SECONDS` (default `60`).

Model selection is tiered. `PATCHY_FAST_MODEL` (for example `gemini-3.5-flash-lite`) covers low-stakes structured tasks: incident evidence synthesis (`summarize`) and test-plan command selection (`test plan`). `PATCHY_CODE_MODEL` covers code-writing tasks: incident patch generation and regression test drafting (`test generate`). Both fall back to `PATCHY_REASONING_MODEL` (default `gemini-3.5-flash`) when unset. Using a lite model for the fast tier noticeably shortens the guided `test guide` chain, while code generation keeps the stronger model to avoid broken patches and wasted approval round-trips.

Use:

```text
test plan <incident-id>
```

Patchy reads the incident's configured repository and base branch through the GitHub API, finds bounded test files, and asks the LLM for focused pytest recommendations. It returns a reviewable plan only. No files are changed, no tests execute, and every proposed command must reference a test file found in the repository tree.

Once reviewed, use `test run <test-plan-id>`. Patchy creates an approval proposal that dispatches a repository-owned GitHub Actions workflow on the exact hotfix branch. Test commands use `python -m pytest` so they do not depend on a standalone `pytest` executable being on `PATH`. Approval does not run a local shell; it asks GitHub Actions to run the pinned workflow with the validated test commands.

Dispatching a workflow is not the same as passing tests. Use `test status <test-plan-id>` to read the workflow run. Once a test plan is associated with a remediation, the merge endpoint rejects the hotfix unless the persisted test plan status is `passed`.

The target repository must provide a workflow configured through `PATCHY_TEST_WORKFLOW`, with `workflow_dispatch` and a `test_commands` input. There is no default workflow filename. That workflow must check out the requested branch, run the supplied focused tests inside the repository's own CI environment, and publish its result through GitHub Actions.

To draft a regression test for the incident, use `test generate <incident-id>`. Patchy returns one new pytest file with its exact content and rationale. Review it, then use `test approve <generated-test-id>` to create the normal approval proposal. The approved commit writes only that new test file to the hotfix branch; it does not write to `main` or overwrite an existing file.

## Agentic verification flow

The first adaptive workflows are:

```text
verify bty
verify saapp
```

Patchy proceeds one approved step at a time:

1. Propose a registered health endpoint GET.
2. Wait for operator approval.
3. Execute and store health evidence.
4. If health succeeds, propose five latency samples.
5. Wait for a second operator approval.
6. Execute the samples and calculate median/max latency.
7. Correlate evidence with active incidents and recent error logs.
8. Produce a final `STABLE`, `DEGRADED`, or `UNHEALTHY` report.

Patchy selects step two from step-one evidence, but it cannot approve or execute its own proposal.

## Incident investigation flow

Use:

```text
investigate <incident-id>
```

If the incident ID is omitted, Patchy asks the operator to choose from the active incident list. A resolved or closed incident can still be reviewed by providing its ID explicitly.

Patchy creates an evidence plan for active and resolved incidents. Active incidents become investigation plans; resolved or closed incidents become resolution-review plans. Both use only allowlisted commands and advance with `next`.

The first step is always:

1. `explain <incident-id>`

After each result, Patchy chooses the next safe evidence step. For example, an analyzed SAAPP incident leads to `logs saapp error`, while an incident without analysis leads to `diagnostics`. Warning or error evidence can trigger a diagnostic reassessment. Resolved incidents stop after their review evidence.

When the evidence is sufficient, Patchy stops and prints a final investigation or resolution-review summary. It does not execute shell commands, mutate production state, or approve network actions itself.

## Production operations status

Use:

```text
ops status [all|bty|saapp]
```

This is a provider-neutral, read-only production view combining live health, active incidents, and recent error logs. Deployment fields are populated when configured with `ERRAGENT_BTY_PLATFORM`, `ERRAGENT_BTY_DEPLOYMENT_ID`, `ERRAGENT_BTY_VERSION`, `ERRAGENT_BTY_REGION` and the corresponding `ERRAGENT_SAAPP_*` variables. Until a cloud provider adapter is added, those fields report `not configured` rather than guessing deployment state.

For Render-backed deployment state, configure `RENDER_API_KEY`, `ERRAGENT_BTY_RENDER_SERVICE_ID`, and `ERRAGENT_SAAPP_RENDER_SERVICE_ID`. `render status` performs read-only Render API requests for service metadata and the latest deployment. It does not restart, redeploy, or mutate a Render service.

## Deterministic planner flow

The first planning commands are:

```text
plan verify bty stability
plan verify saapp stability
plan investigate incidents
```

Patchy stores a plan in MongoDB's `patchy_plans` collection. Plans are deterministic templates made only from existing allowlisted commands. They do not use an LLM and cannot emit shell commands.

Advance the most recent active plan with:

```text
next
```

Or use guided approval mode:

```text
guide [plan-id|incident-id]
```

`guide` proposes the next allowlisted plan step as an approval card. The operator can approve or disapprove without typing the next command manually. If you pass an incident ID, Patchy resolves it to the latest active plan for that incident. Once that incident plan is complete, `guide <incident-id>` automatically pivots into `test guide <incident-id>` so the same command can carry the full incident-to-test flow.

You can still target a specific plan with `next <plan-id>`.

Each `next` call runs exactly one allowlisted step. If that step creates a proposal, the proposal still requires normal **Approve & Run** handling before any network action executes.

## Current policy boundary

Phase one intentionally supports only:

- HTTP `GET`
- Registered BTY and SAAPP health URLs
- Maximum 30-second execution timeout
- Explicit operator approval
- One execution per proposal

Patchy cannot run shell commands, arbitrary URLs, generated scripts, chained commands, package installation, or filesystem mutations.

## API surface

```text
POST /api/v1/patchy/command
GET  /api/v1/patchy/proposals
POST /api/v1/patchy/proposals/{proposal_id}/approve
```

All routes require an authenticated errAgent operator.

## Mascot states

The compact terminal-specific recreation of Patchy reflects terminal state:

```text
idle       Terminal is ready
running    A command or approved action is executing
approval   A proposal awaits operator approval
success    The latest command succeeded
warning    The latest command found operational concerns
error      The latest command failed
```

Animations are visual status cues only and do not affect execution state.

## Next safe phases

1. Add `test ingestion` as an approved synthetic create/verify/cleanup workflow.
2. Add a persistent task-history browser.
3. Add declarative generated HTTP tests.
4. Introduce generated scripts only in a separate sandboxed worker with no production secrets.

## PenTest Sweep Commands

```text
pentest sweep <bty|saapp> [target]
Creates a pentest sweep proposal. The operator must approve the proposal before any sweep runs.

Targets
Sweeps support multiple modes:

Target	Description
public	Synthetic-only fuzzing of public endpoints. No authentication. No UI automation.
admin_leads	Authenticated Browser Agent sweep of Clerk-protected admin leads endpoints.
admin_content	Authenticated Browser Agent sweep of Clerk-protected admin content endpoints.
admin_all	Runs both admin_leads and admin_content fuzzers.
full	Runs public synthetic fuzzing and all admin fuzzers.
sonic_admin Runs sonic admin attack
sonic_user Runs user attacks
sonic_guest Runs attacks from guest session


If no target is provided, Patchy defaults to:

text
full
Example
text
pentest sweep bty admin_content
Creates a proposal to run an authenticated sweep of BTY’s Clerk-protected admin content endpoints.

Proposal Details
A pentest sweep proposal includes:

Service name and alias

Selected target

Synthetic-only flag (true only for public)

Maximum endpoints and fuzz payloads

Risk classification: pentest

No single HTTP action — sweeps run multiple bounded requests

Operator approval requirement

Execution
After approval:

Public sweeps run synthetic fuzzing against registered endpoints.

Admin sweeps launch a Browser Agent, sign in through Clerk, extract a session token, and run authenticated fuzzers.

Vulnerabilities are recorded as incidents.

Sweeps never modify application state, create real bookings, send real emails, or perform destructive actions.

Output
Patchy reports:

Sweep status (succeeded, warning)

Number of endpoints scanned

Number of vulnerabilities found

Vulnerability details (endpoint, payload, issue, response)

Sweeps complete only after all selected targets have been processed.
```

