import asyncio
import shlex
from datetime import datetime, timezone
from typing import Any

from backend.services.log_broker import LogBroker
from backend.services.patchy_hitl import create_probe_proposal, create_verification_workflow
from backend.services.patchy_hitl import create_probe_proposal, create_synthetic_proposal, create_verification_workflow
from backend.services.patchy_planner import PatchyPlanError, build_plan_report, create_incident_investigation_plan, create_plan, next_step, record_adaptive_step_result, get_plan, get_latest_active_plan
from backend.services.production_ops import collect_production_status, format_production_status
from backend.services.patchy_reasoning import PatchyReasoningError, synthesize_incident
from backend.services.render_ops import RenderOpsError, collect_render_status, format_render_status
from backend.services.github_service import GitHubOpsService
from backend.services.patchy_test_planner import PatchyTestPlanError, create_test_plan
from backend.services.patchy_test_runner import PatchyTestExecutionError, create_test_execution_proposal, get_test_execution_status
from backend.services.patchy_test_generator import PatchyGeneratedTestError, create_generated_test_proposal, generate_regression_test
from backend.services.synthetic_adapters import SyntheticAdapterError, create_question_proposal
from backend.utils.app_utils import SERVICES, build_health_report, run_service_health_checks, serialize_mongo_doc


COMMAND_HELP = (
    ("help", "List available Patchy commands"),
    ("health [all|bty|saapp]", "Run live service health checks"),
    ("ops status [all|bty|saapp]", "Inspect read-only production service state"),
    ("render status [all|bty|saapp]", "Inspect Render service and latest deployment"),
    ("incidents", "Show active incidents"),
    ("list incidents [all|open|resolved]", "List incidents by lifecycle status"),
    ("logs [all|bty|saapp|erragent] [info|warn|error]", "Show recent in-memory logs"),
    ("diagnostics", "Run health, incident, and log checks together"),
    ("probe [bty|saapp]", "Propose a read-only HTTP probe for approval"),
        ("synthetic [bty|saapp]", "Propose a registered synthetic HTTP assertion"),
        ("synthetic ask sonic <question>", "Propose a staging Sonic Assistant question"),
    ("verify [bty|saapp]", "Run a two-step HITL stability verification"),
    ("plan verify [bty|saapp] stability", "Create a deterministic multi-step plan"),
    ("investigate [incident-id]", "Create an investigation or request an incident to investigate"),
    ("next [plan-id]", "Run the next pending step for a plan"),
    ("explain <incident-id>", "Show structured incident details and analysis"),
    ("summarize <incident-id>", "Use the LLM to synthesize supplied incident evidence"),
    ("confirm deployed <incident-id>", "Record operator-confirmed production deployment"),
    ("test plan <incident-id>", "Inspect GitHub tests and draft a focused test plan"),
    ("test run <test-plan-id>", "Propose approved CI execution for a test plan"),
    ("test status <test-plan-id>", "Read the latest GitHub Actions test result"),
    ("test generate <incident-id>", "Draft a regression test for operator review"),
    ("clear", "Clear the terminal screen locally"),
)

_SERVICE_ALIASES = {
    "bty": "BTY Fitness",
    "saapp": "SAAPP Widget",
}

_LOG_SERVICE_ALIASES = {
    "bty": "BTY",
    "saapp": "SAAPP",
    "erragent": "errAgent",
}


class PatchyCommandError(ValueError):
    pass


def _response(status: str, title: str, lines: list[str], data: Any = None) -> dict[str, Any]:
    payload = {
        "status": status,
        "title": title,
        "lines": lines,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if data is not None:
        payload["data"] = serialize_mongo_doc(data)
    return payload


def _active_incidents(db, limit: int = 20) -> list[dict[str, Any]]:
    return list(
        db["incidents"].find(
            {"status": {"$nin": ["resolved", "closed"]}},
            {
                "_id": 1,
                "service_name": 1,
                "status": 1,
                "error_message": 1,
                "created_at": 1,
            },
        ).sort("created_at", -1).limit(limit)
    )


def _list_incidents(db, scope: str, limit: int = 50) -> list[dict[str, Any]]:
    incidents = list(db["incidents"].find({}).sort("created_at", -1).limit(limit))
    if scope == "all":
        return incidents
    if scope == "open":
        return [incident for incident in incidents if incident.get("status") not in {"resolved", "closed"}]
    if scope == "resolved":
        return [incident for incident in incidents if incident.get("status") in {"resolved", "closed"}]
    raise PatchyCommandError("Usage: list incidents [all|open|resolved]")


def _format_incident_lines(incidents: list[dict[str, Any]]) -> list[str]:
    return [
        f"{incident['_id']} | {incident.get('service_name', 'unknown')} | {incident.get('status', 'open')} | {incident.get('error_message', 'No message')}"
        for incident in incidents
    ]


async def _run_health(target: str) -> dict[str, Any]:
    results = await asyncio.to_thread(run_service_health_checks)
    if target != "all":
        service_name = _SERVICE_ALIASES.get(target)
        if not service_name:
            raise PatchyCommandError("Usage: health [all|bty|saapp]")
        results = [result for result in results if result.get("service") == service_name]

    report = build_health_report(results)
    lines = [
        f"{item['service']}: {item['status'].upper()} | latency={item.get('latency_ms') or 'n/a'}ms | http={item.get('http_status') or 'n/a'}"
        for item in results
    ]
    return _response(
        "success" if report["overall_status"] == "OK" else "warning",
        f"Health check: {report['overall_status']}",
        lines or ["No matching service found."],
        report,
    )


async def _run_logs(broker: LogBroker, args: list[str]) -> dict[str, Any]:
    service_arg = args[0].lower() if args else "all"
    level = args[1].lower() if len(args) > 1 else None
    if service_arg not in {"all", *_LOG_SERVICE_ALIASES.keys()}:
        raise PatchyCommandError("Usage: logs [all|bty|saapp|erragent] [info|warn|error]")
    if level not in {None, "info", "warn", "error"}:
        raise PatchyCommandError("Log level must be info, warn, or error")

    service = None if service_arg == "all" else _LOG_SERVICE_ALIASES[service_arg]
    entries = await broker.get_history(service=service, level=level, limit=50)
    lines = [
        f"{entry['timestamp']} [{entry['level'].upper()}] {entry['service']}: {entry['message'].splitlines()[0]}"
        for entry in entries
    ]
    return _response(
        "success",
        f"Recent logs ({len(entries)})",
        lines or ["No matching logs in the in-memory buffer."],
        entries,
    )


async def execute_patchy_command(
    command_text: str,
    db,
    broker: LogBroker,
    actor: str = "operator",
) -> dict[str, Any]:
    try:
        parts = shlex.split(command_text.strip())
    except ValueError as exc:
        raise PatchyCommandError(f"Could not parse command: {exc}") from exc

    if not parts:
        raise PatchyCommandError("Enter a command or type 'help'.")

    command = parts[0].lower()
    args = parts[1:]

    if command == "help":
        return _response("success", "Patchy command reference", [f"{name:<42} {description}" for name, description in COMMAND_HELP])

    if command == "plan":
        if not args:
            raise PatchyCommandError("Usage: plan verify [bty|saapp] stability")
        try:
            plan = create_plan(" ".join(args), actor, db)
        except PatchyPlanError as exc:
            raise PatchyCommandError(str(exc)) from exc
        lines = [
            f"Plan ID: {plan['_id']}",
            f"Goal: {plan['goal']}",
            "Steps:",
            *[f"{step['index'] + 1}. {step['command']} — {step['reason']}" for step in plan["steps"]],
            "Run: next",
        ]
        return _response("success", "Patchy drafted a plan", lines, {"plan": plan})

    if command == "investigate":
        if not args:
            incidents = _active_incidents(db, limit=8)
            if not incidents:
                return _response(
                    "warning",
                    "No active incidents to investigate",
                    ["Provide a resolved incident ID to review its resolution."],
                )
            options = [
                {
                    "label": f"{incident['_id']} · {incident.get('service_name', 'unknown service')}",
                    "value": f"investigate {incident['_id']}",
                }
                for incident in incidents
            ]
            return _response(
                "clarification_required",
                "Patchy needs one detail",
                [
                    "Which active incident should I investigate?",
                    "I found these active incidents and will use only the selected incident ID.",
                ],
                {
                    "clarification": {
                        "id": "investigation_incident",
                        "question": "Choose an active incident",
                        "options": options,
                    },
                },
            )
        if len(args) != 1:
            raise PatchyCommandError("Usage: investigate <incident-id>")
        try:
            plan = create_incident_investigation_plan(args[0], actor, db)
        except PatchyPlanError as exc:
            raise PatchyCommandError(str(exc)) from exc
        lines = [
            f"Plan ID: {plan['_id']}",
            f"Goal: {plan['goal']}",
            "Steps:",
            *[f"{step['index'] + 1}. {step['command']} — {step['reason']}" for step in plan["steps"]],
            "Run: next",
        ]
        return _response("success", "Patchy drafted an investigation plan", lines, {"plan": plan})

    if command == "next":
        if len(args) > 1:
            raise PatchyCommandError("Usage: next [plan-id]")
        try:
            plan = get_plan(db, args[0]) if args else get_latest_active_plan(db, actor)
            plan_id = plan["_id"]
            step = next_step(plan)
            result = await execute_patchy_command(step["command"], db, broker, actor=actor)
            updated_plan = record_adaptive_step_result(db, plan_id, step["index"], result)
        except PatchyPlanError as exc:
            raise PatchyCommandError(str(exc)) from exc

        lines = [
            f"Step {step['index'] + 1}: {step['command']}",
            f"Reason: {step['reason']}",
            f"Result: {result['title']}",
        ]
        if result.get("lines"):
            lines.extend(["", *result["lines"]])
        if updated_plan["status"] == "completed":
            lines.append("Plan complete.")
        else:
            lines.append("Next: next")
        report = build_plan_report(updated_plan)
        data = {"plan": updated_plan, "stepResult": result}
        if report:
            data["planReport"] = report
            lines.extend(["", *report["lines"]])
        return _response(report["status"] if report else result["status"], report["title"] if report else f"Plan step {step['index'] + 1} complete", lines, data)

    if command == "health":
        return await _run_health(args[0].lower() if args else "all")

    if command == "ops":
        if not args or args[0].lower() != "status" or len(args) > 2:
            raise PatchyCommandError("Usage: ops status [all|bty|saapp]")
        target = args[1].lower() if len(args) > 1 else "all"
        try:
            report = await collect_production_status(target, db, broker)
        except ValueError as exc:
            raise PatchyCommandError(str(exc)) from exc
        status = "error" if report["overallStatus"] == "down" else "warning" if report["overallStatus"] == "degraded" else "success"
        return _response(status, "Production operations status", format_production_status(report), report)

    if command == "render":
        if not args or args[0].lower() != "status" or len(args) > 2:
            raise PatchyCommandError("Usage: render status [all|bty|saapp]")
        target = args[1].lower() if len(args) > 1 else "all"
        try:
            report = await collect_render_status(target)
        except RenderOpsError as exc:
            raise PatchyCommandError(str(exc)) from exc
        status = "success" if report["status"] == "ok" else "warning" if report["status"] == "not_configured" else "error"
        return _response(status, "Render operations status", format_render_status(report), report)

    if command == "incidents":
        incidents = _active_incidents(db)
        lines = _format_incident_lines(incidents)
        return _response("warning" if incidents else "success", f"Active incidents ({len(incidents)})", lines or ["No active incidents."], incidents)

    if command == "list":
        if len(args) != 2 or args[0].lower() != "incidents":
            raise PatchyCommandError("Usage: list incidents [all|open|resolved]")
        scope = args[1].lower()
        incidents = _list_incidents(db, scope)
        title_scope = {"all": "All", "open": "Open", "resolved": "Resolved"}.get(scope)
        lines = _format_incident_lines(incidents)
        return _response(
            "warning" if scope == "open" and incidents else "success",
            f"{title_scope} incidents ({len(incidents)})",
            lines or [f"No {scope} incidents."],
            incidents,
        )

    if command == "logs":
        return await _run_logs(broker, args)

    if command == "explain":
        if len(args) != 1:
            raise PatchyCommandError("Usage: explain <incident-id>")
        incident_id = args[0]
        incident = db["incidents"].find_one({"_id": incident_id})
        if not incident:
            raise PatchyCommandError(f"Incident not found: {incident_id}")
        analysis = db["analyses"].find_one({"incident_id": incident_id}, sort=[("updated_at", -1), ("created_at", -1)]) or {}
        lines = [
            f"Service: {incident.get('service_name', 'unknown')}",
            f"Status: {incident.get('status', 'open')}",
            f"Error: {incident.get('error_message', 'No message')}",
            f"Root cause: {analysis.get('root_cause_summary') or analysis.get('root_cause') or 'Not analyzed yet'}",
            f"Suggested fix: {analysis.get('suggested_fix') or 'Not available'}",
        ]
        return _response("success", f"Incident {incident_id}", lines, {"incident": incident, "analysis": analysis})

    if command == "summarize":
        if len(args) != 1:
            raise PatchyCommandError("Usage: summarize <incident-id>")
        try:
            result = await synthesize_incident(args[0], db, broker)
        except PatchyReasoningError as exc:
            raise PatchyCommandError(str(exc)) from exc
        synthesis = result["synthesis"]
        missing_lines = [f"- {item}" for item in synthesis.get("missing_information", [])] or ["- None identified"]
        lines = [
            synthesis["summary"],
            "",
            "Hypotheses:",
            *[
                f"- {item['claim']} ({item['confidence']:.0%}) | Evidence: {', '.join(item['evidence']) or 'none listed'}"
                for item in synthesis.get("hypotheses", [])
            ],
            "",
            "Missing information:",
            *missing_lines,
            "",
            f"Recommended next action: {synthesis['recommended_action']}",
        ]
        if synthesis.get("should_ask_operator"):
            lines.append("Patchy recommends collecting the missing information before continuing.")
        return _response("success", "Patchy evidence synthesis", lines, result)

    if command == "confirm":
        if len(args) != 2 or args[0].lower() != "deployed":
            raise PatchyCommandError("Usage: confirm deployed <incident-id>")
        incident_id = args[1]
        incident = db["incidents"].find_one({"_id": incident_id})
        if not incident:
            raise PatchyCommandError(f"Incident not found: {incident_id}")
        remediation = db["remediations"].find_one({"incident_id": incident_id}, sort=[("updated_at", -1), ("created_at", -1)])
        if not remediation:
            raise PatchyCommandError("No remediation record exists for this incident")
        now = datetime.now(timezone.utc)
        db["remediations"].update_one(
            {"_id": remediation.get("_id")} if remediation.get("_id") else {"incident_id": incident_id},
            {"$set": {
                "deployment_status": "confirmed",
                "deployment_confirmed_by": actor,
                "deployment_confirmed_at": now,
                "updated_at": now,
            }},
        )
        return _response(
            "success",
            "Deployment confirmation recorded",
            [
                f"Incident: {incident_id}",
                f"Confirmed by: {actor}",
                "Patchy will treat the merged fix as deployed in future evidence summaries.",
            ],
            {"incidentId": incident_id, "deploymentStatus": "confirmed", "confirmedBy": actor, "confirmedAt": now},
        )

    if command == "test":
        if len(args) != 2 or args[0].lower() not in {"plan", "run", "status", "generate", "approve"}:
            raise PatchyCommandError("Usage: test plan|run|status|generate|approve <id>")
        if args[0].lower() == "approve":
            try:
                proposal = create_generated_test_proposal(args[1], actor, db)
            except PatchyGeneratedTestError as exc:
                raise PatchyCommandError(str(exc)) from exc
            return _response(
                "approval_required",
                "Approval required for generated test commit",
                [
                    proposal["summary"],
                    f"Repository: {proposal['repository']}",
                    f"Hotfix branch: {proposal['action']['branch']}",
                    f"File: {proposal['action']['file']}",
                    "Risk: new regression test on the hotfix branch only.",
                ],
                {"proposal": proposal},
            )
        if args[0].lower() == "generate":
            try:
                generated = await generate_regression_test(args[1], db, GitHubOpsService())
            except PatchyGeneratedTestError as exc:
                raise PatchyCommandError(str(exc)) from exc
            return _response(
                "success",
                "Patchy drafted a regression test",
                [
                    f"Generated test ID: {generated['_id']}",
                    f"Repository: {generated['repository']}",
                    f"Hotfix branch: {generated['test_branch']}",
                    f"File: {generated['test_file']}",
                    f"Test: {generated['test_name']}",
                    f"Rationale: {generated['rationale']}",
                    "Review the exact content in the panel, then run:",
                    f"test approve {generated['_id']}",
                ],
                {"generatedTest": generated},
            )
        if args[0].lower() == "status":
            try:
                result = await get_test_execution_status(args[1], db, GitHubOpsService())
            except PatchyTestExecutionError as exc:
                raise PatchyCommandError(str(exc)) from exc
            status = "success" if result["status"] == "passed" else "error" if result["status"] == "failed" else "warning"
            run = result.get("workflowRun", {})
            return _response(status, "Patchy test execution status", [
                f"Status: {result['status']}",
                f"Workflow run: {run.get('status', 'queued')} / {run.get('conclusion', 'pending')}",
                f"Run URL: {run.get('html_url', 'not available')}",
            ], result)
        if args[0].lower() == "run":
            try:
                proposal = create_test_execution_proposal(args[1], actor, db)
            except PatchyTestExecutionError as exc:
                raise PatchyCommandError(str(exc)) from exc
            action = proposal["action"]
            return _response(
                "approval_required",
                "Approval required for test execution",
                [
                    proposal["summary"],
                    f"Repository: {proposal['repository']}",
                    f"Branch: {action['branch']}",
                    f"Workflow: {action['url']}",
                    "Commands:",
                    *[f"- {command}" for command in action["commands"]],
                    "Risk: repository CI only; no local shell execution.",
                ],
                {"proposal": proposal},
            )
        try:
            result = await create_test_plan(args[1], db, GitHubOpsService())
        except PatchyTestPlanError as exc:
            raise PatchyCommandError(str(exc)) from exc
        plan = result["plan"]
        lines = [
            f"Test plan ID: {result['testPlanId']}",
            f"Repository: {result['repository']} ({result['branch']})",
            plan["summary"],
            "",
            "Recommended tests:",
            *[
                f"- {item['command']} ({item['confidence']:.0%}) | {item['rationale']}"
                for item in plan.get("recommendations", [])
            ],
            "",
            "Missing information:",
            *([f"- {item}" for item in plan.get("missing_information", [])] or ["- None identified"]),
            "",
            f"Review, then run: test run {result['testPlanId']}",
        ]
        return _response("success", "Patchy test plan", lines, result)

    if command == "diagnostics":
        health_result = await _run_health("all")
        incidents = _active_incidents(db, limit=10)
        error_logs = await broker.get_history(level="error", limit=10)
        status = "warning" if health_result["status"] == "warning" or incidents or error_logs else "success"
        lines = [
            f"Health: {health_result['title']}",
            f"Active incidents: {len(incidents)}",
            f"Recent error logs: {len(error_logs)}",
            "Diagnostic sequence complete.",
        ]
        return _response(status, "Patchy diagnostics", lines, {"health": health_result.get("data"), "incidents": incidents, "errorLogs": error_logs})

    if command == "probe":
        if not args:
            return _response(
                "clarification_required",
                "Patchy needs one detail",
                [
                    "Which registered production service should I probe?",
                    "I will not guess an endpoint or accept an unregistered URL.",
                ],
                {
                    "clarification": {
                        "id": "probe_service",
                        "question": "Choose a registered service",
                        "options": [
                            {"label": "BTY Fitness", "value": "probe bty"},
                            {"label": "SAAPP Widget", "value": "probe saapp"},
                        ],
                    },
                },
            )
        if len(args) != 1:
            raise PatchyCommandError("Usage: probe [bty|saapp]")
        proposal = create_probe_proposal(args[0], actor, db)
        action = proposal["action"]
        return _response(
            "approval_required",
            "Approval required",
            [
                proposal["summary"],
                f"Method: {action['method']}",
                f"URL: {action['url']}",
                f"Timeout: {action['timeoutSeconds']}s",
                "Risk: read-only",
            ],
            {"proposal": proposal},
        )
    if command == "synthetic":
        if len(args) >= 3 and args[0].lower() == "ask":
            if args[1].lower() != "sonic":
                raise PatchyCommandError("Usage: synthetic ask sonic <question> [--production-read-only]")
            production_read_only = args[-1].lower() == "--production-read-only"
            question_args = args[2:-1] if production_read_only else args[2:]
            if not question_args:
                raise PatchyCommandError("A question is required")
            try:
                proposal = create_question_proposal(
                    args[1],
                    " ".join(question_args),
                    actor,
                    db,
                    allow_production=production_read_only,
                )
            except SyntheticAdapterError as exc:
                raise PatchyCommandError(str(exc)) from exc
            action = proposal["action"]
            return _response(
                "approval_required",
                "Approval required for staging question",
                [
                    proposal["summary"],
                    f"Environment: {action['environment']}",
                    f"URL: {action['url']}",
                    f"Question: {action['question']}",
                    *[f"Assertion: {assertion}" for assertion in action["assertions"]],
                    f"Risk: {proposal['risk']}",
                ],
                {"proposal": proposal},
            )
        if len(args) != 1:
            raise PatchyCommandError("Usage: synthetic [bty|saapp] or synthetic ask sonic <question> [--production-read-only]")
        try:
            proposal = create_synthetic_proposal(args[0], actor, db)
        except PatchyProposalError as exc:
            raise PatchyCommandError(str(exc)) from exc
        action = proposal["action"]
        return _response(
            "approval_required",
            "Approval required for synthetic test",
            [
                proposal["summary"],
                f"Method: {action['method']}",
                f"URL: {action['url']}",
                *[f"Assertion: {assertion}" for assertion in action["assertions"]],
                "Risk: registered read-only endpoint; no staging sandbox is implied.",
            ],
            {"proposal": proposal},
        )

    if command == "verify":
        if len(args) != 1:
            raise PatchyCommandError("Usage: verify [bty|saapp]")
        proposal = create_verification_workflow(args[0], actor, db)
        action = proposal["action"]
        return _response(
            "approval_required",
            proposal["workflow"]["goal"],
            [
                "Step 1/2: Confirm the registered health endpoint responds.",
                f"Method: {action['method']}",
                f"URL: {action['url']}",
                "Patchy will propose latency sampling after this evidence is approved and collected.",
            ],
            {"proposal": proposal},
        )

    if command == "clear":
        return _response("success", "Screen cleared", [])

    raise PatchyCommandError(f"Unknown command: {command}. Type 'help' for available commands.")
