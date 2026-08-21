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
investigate [incident-id]
verify [bty|saapp]
plan verify [bty|saapp] stability
plan investigate incidents
next [plan-id]
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
