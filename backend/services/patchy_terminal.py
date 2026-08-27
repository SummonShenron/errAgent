import asyncio
import json
import shlex
from datetime import datetime, timezone
from typing import Any
from backend.patchy_browser_agent.saapp_runner import run_sonic_discovery_suite
from backend.services.patchy_errors import PatchyCommandError
from backend.services.log_broker import LogBroker
from backend.services.patchy_hitl import PatchyProposalError, create_pentest_sweep_proposal, create_plan_step_proposal, create_probe_proposal, create_synthetic_proposal, create_verification_workflow
from backend.services.patchy_planner import PatchyPlanError, build_plan_report, create_incident_investigation_plan, create_plan, next_step, record_adaptive_step_result, get_plan, get_latest_active_plan
from backend.services.production_ops import collect_production_status, format_production_status
from backend.services.patchy_reasoning import PatchyReasoningError, synthesize_incident
from backend.services.render_ops import RenderOpsError, collect_render_status, format_render_status
from backend.services.github_service import GitHubOpsService
from backend.services.patchy_test_planner import PatchyTestPlanError, create_test_plan
from backend.services.patchy_test_runner import PatchyTestExecutionError, create_test_execution_proposal, get_test_execution_status
from backend.services.patchy_test_generator import PatchyGeneratedTestError, create_generated_test_proposal, generate_regression_test
from backend.services.synthetic_adapters import SyntheticAdapterError, create_question_proposal
from backend.services.patchy_flow_runner import PatchyFlowError, create_email_validation_proposal, create_flow_plan, create_flow_proposal, create_leakage_validation_proposal, create_validation_proposal, list_flow_plans
from backend.services.analyze_test_failure import analyze_test_failure
from backend.services.patchy_discovery import discover_endpoints_command
from backend.utils.app_utils import SERVICES, build_health_report, run_service_health_checks, serialize_mongo_doc
from backend.services.log_broker import LogEventInput

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
    ("synthetic ask sonic <question> [--production-read-only]", "Propose a Sonic Assistant question (staging by default)"),
    ("verify [bty|saapp]", "Run a two-step HITL stability verification"),
    ("plan verify [bty|saapp] stability", "Create a deterministic multi-step plan"),
    ("investigate [incident-id]", "Create an investigation or request an incident to investigate"),
    ("next [plan-id]", "Run the next pending step for a plan"),
    ("guide [plan-id|incident-id]", "Run guided approvals for plan steps and incident test workflow"),
    ("explain <incident-id>", "Show structured incident details and analysis"),
    ("summarize <incident-id>", "Use the LLM to synthesize supplied incident evidence"),
    ("confirm deployed <incident-id>", "Record operator-confirmed production deployment"),
    ("test plan <incident-id>", "Inspect GitHub tests and draft a focused test plan"),
    ("test guide <incident-id>", "Auto-select and run the next test workflow step"),
    ("test run <test-plan-id>", "Propose approved CI execution for a test plan"),
    ("test status <test-plan-id>", "Read the latest GitHub Actions test result"),
    ("test generate <incident-id>", "Draft a regression test for operator review"),
    ("test analyze <test-plan-id>", "Analyze latest CI failure and propose a fix"),
    ("flow define <bty|saapp> <name> <json-actions>", "Save a reusable HTTP flow plan"),
    ("flow list [bty|saapp]", "List saved flow plans"),
    ("flow run <flow-id>", "Propose a flow execution for approval"),
    ("probe validation <bty|saapp>", "Propose a bounded validation audit"),
    ("validate email <bty|saapp> <path>", "Probe email validation without JSON"),
    ("validate leakage <bty|saapp> <path>", "Probe for leaked database details"),
    ("pentest sweep <bty|saapp> [target]", "Propose a synthetic pentest sweep for approval"),
    ("discover endpoints <serviceAlias|url>", "Enumerate known endpoints for a service using static, synthetic, and browser discovery"),
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


_TEST_FLOW_STEPS = [
    ("generate", "Draft regression test"),
    ("approve", "Approve test commit"),
    ("plan", "Draft test plan"),
    ("run", "Approve CI execution"),
    ("status", "Check CI result"),
]


def _guided_test_flow(db, incident_id: str, current_key: str | None) -> dict[str, Any]:
    """Compute done/active/pending state for each guided test-workflow step."""
    completed_through = -1
    generated = db["patchy_generated_tests"].find_one(
        {"incident_id": incident_id},
        sort=[("updated_at", -1), ("created_at", -1)],
    )
    if generated:
        completed_through = 0
        if str(generated.get("status") or "").lower() == "committed":
            completed_through = 1
    plan = db["patchy_test_plans"].find_one(
        {"incident_id": incident_id},
        sort=[("updated_at", -1), ("created_at", -1)],
    )
    if plan:
        completed_through = max(completed_through, 2)
        plan_status = str(plan.get("status") or "").lower()
        if plan_status in {"running", "failed"}:
            completed_through = 3
        elif plan_status == "passed":
            completed_through = len(_TEST_FLOW_STEPS) - 1
            current_key = None
    current_index = next(
        (index for index, (key, _label) in enumerate(_TEST_FLOW_STEPS) if key == current_key),
        None,
    )
    steps = []
    for index, (key, label) in enumerate(_TEST_FLOW_STEPS):
        if index <= completed_through:
            step_status = "done"
        elif current_index is not None and index == current_index:
            step_status = "active"
        elif current_index is None and index == completed_through + 1:
            step_status = "active"
        else:
            step_status = "pending"
        steps.append({"key": key, "label": label, "status": step_status})
    return {"kind": "test_workflow", "incidentId": incident_id, "steps": steps}


def _guided_investigation_flow(plan: dict[str, Any], current_step_index: int, incident_id: str | None) -> dict[str, Any]:
    steps = []
    for step in plan.get("steps", []):
        index = int(step.get("index", 0))
        if step.get("status") == "completed":
            step_status = "done"
        elif index == current_step_index:
            step_status = "active"
        else:
            step_status = "pending"
        steps.append({"key": f"step_{index}", "label": step.get("command", f"step {index + 1}"), "status": step_status})
    return {
        "kind": "investigation",
        "incidentId": incident_id,
        "planId": plan.get("_id"),
        "steps": steps,
    }


def _parse_compact_flow_actions(tokens: list[str]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    request_types = {"GET", "POST", "PUT", "PATCH", "DELETE"}
    index = 0
    while index < len(tokens):
        token = tokens[index].upper()
        if token in request_types:
            if index + 1 >= len(tokens):
                raise PatchyCommandError(f"{token} requires a path")
            actions.append({"type": token, "url": tokens[index + 1]})
            index += 2
        elif token in {"ASSERT", "ASSERT_STATUS"}:
            if index + 1 >= len(tokens):
                raise PatchyCommandError("ASSERT requires an HTTP status code")
            try:
                status_code = int(tokens[index + 1])
            except ValueError as exc:
                raise PatchyCommandError("ASSERT requires an HTTP status code") from exc
            actions.append({"type": "assert_status", "equals": status_code})
            index += 2
        elif token == "BODY":
            if not actions or actions[-1]["type"] == "GET":
                raise PatchyCommandError("BODY must follow a non-GET request")
            if index + 1 >= len(tokens):
                raise PatchyCommandError("BODY requires a JSON object")
            try:
                actions[-1]["body"] = json.loads(tokens[index + 1])
            except json.JSONDecodeError as exc:
                raise PatchyCommandError(f"BODY must be valid JSON: {exc}") from exc
            index += 2
        else:
            raise PatchyCommandError(
                f"Unknown compact flow token: {tokens[index]}. Use GET/POST/PUT/PATCH/DELETE, ASSERT, or BODY."
            )
    if not actions:
        raise PatchyCommandError("At least one compact flow action is required")
    return actions


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

    if command == "guide":
        if len(args) > 1:
            raise PatchyCommandError("Usage: guide [plan-id|incident-id]")
        target = args[0] if args else None
        if target and not target.startswith("plan_"):
            incident_id = target
            active_plan = db["patchy_plans"].find_one(
                {"subject.incidentId": incident_id, "status": {"$ne": "completed"}},
                sort=[("updated_at", -1), ("created_at", -1)],
            )
            if not active_plan:
                latest_plan = db["patchy_plans"].find_one(
                    {"subject.incidentId": incident_id},
                    sort=[("updated_at", -1), ("created_at", -1)],
                )
                if not latest_plan:
                    bootstrap = await execute_patchy_command(f"investigate {incident_id}", db, broker, actor=actor)
                    plan = (bootstrap.get("data") or {}).get("plan") or {}
                    if not plan.get("_id"):
                        raise PatchyCommandError(
                            f"Could not start guided flow for incident: {incident_id}."
                        )
                    active_plan = db["patchy_plans"].find_one({"_id": plan["_id"]})
                elif latest_plan.get("status") == "completed":
                    guided_test = await execute_patchy_command(f"test guide {incident_id}", db, broker, actor=actor)
                    return {
                        **guided_test,
                        "title": f"Guided incident workflow · {guided_test.get('title', 'step complete')}",
                        "lines": [
                            "Investigation plan is complete. Continuing with guided test workflow.",
                            "",
                            *(guided_test.get("lines") or []),
                        ],
                        "data": serialize_mongo_doc(
                            {
                                **(guided_test.get("data") or {}),
                                "guidedIncidentId": incident_id,
                                "guidedPhase": "test_workflow",
                            }
                        ),
                    }
                else:
                    active_plan = latest_plan
            target = active_plan["_id"]
        try:
            proposal = create_plan_step_proposal(db, actor, target)
        except (PatchyPlanError, ValueError) as exc:
            raise PatchyCommandError(str(exc)) from exc
        action = proposal["action"]
        plan_doc = db["patchy_plans"].find_one({"_id": proposal["planId"]}) or {}
        guided_flow = _guided_investigation_flow(
            plan_doc,
            int(proposal["planStepIndex"]),
            (plan_doc.get("subject") or {}).get("incidentId"),
        )
        return _response(
            "approval_required",
            "Approval required for guided next step",
            [
                proposal["summary"],
                f"Plan: {proposal['planId']}",
                f"Step: {proposal['planStepIndex'] + 1}",
                f"Command: {action['command']}",
                f"Reason: {action.get('reason', 'n/a')}",
                "Risk: allowlisted Patchy command only.",
            ],
            {"proposal": proposal, "guidedFlow": guided_flow},
        )

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
        if len(args) != 2 or args[0].lower() not in {"plan", "run", "status", "generate", "approve", "guide", "analyze"}:   
            raise PatchyCommandError("Usage: test plan|run|status|generate|approve|guide|analyze <id>")
        if args[0].lower() == "guide":
            incident_id = args[1]
            generated = db["patchy_generated_tests"].find_one(
                {"incident_id": incident_id},
                sort=[("updated_at", -1), ("created_at", -1)],
            )
            if not generated:
                selected_command = f"test generate {incident_id}"
            else:
                generated_status = str(generated.get("status") or "").lower()
                if generated_status == "ready_for_review":
                    selected_command = f"test approve {generated['_id']}"
                elif generated_status == "awaiting_approval":
                    proposal_id = generated.get("proposal_id")
                    proposal = db["patchy_proposals"].find_one({"_id": proposal_id}) if proposal_id else None
                    if proposal and proposal.get("status") == "awaiting_approval":
                        return _response(
                            "approval_required",
                            "Approval required for generated test commit",
                            [
                                proposal["summary"],
                                f"Repository: {proposal['repository']}",
                                f"Hotfix branch: {proposal['action']['branch']}",
                                f"File: {proposal['action']['file']}",
                                "Risk: new regression test on the hotfix branch only.",
                                "Patchy resumed the existing pending step.",
                            ],
                            {"proposal": proposal, "guidedFlow": _guided_test_flow(db, incident_id, "approve")},
                        )
                    selected_command = f"test approve {generated['_id']}"
                elif generated_status == "committed":
                    plan = db["patchy_test_plans"].find_one(
                        {"incident_id": incident_id},
                        sort=[("updated_at", -1), ("created_at", -1)],
                    )
                    if not plan:
                        selected_command = f"test plan {incident_id}"
                    else:
                        plan_status = str(plan.get("status") or "").lower()
                        if plan_status == "ready_for_review":
                            selected_command = f"test run {plan['_id']}"
                        elif plan_status == "awaiting_execution_approval":
                            proposal_id = plan.get("proposal_id")
                            proposal = db["patchy_proposals"].find_one({"_id": proposal_id}) if proposal_id else None
                            if proposal and proposal.get("status") == "awaiting_approval":
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
                                        "Patchy resumed the existing pending step.",
                                    ],
                                    {"proposal": proposal, "guidedFlow": _guided_test_flow(db, incident_id, "run")},
                                )
                            selected_command = f"test run {plan['_id']}"
                        elif plan_status == "running":
                            selected_command = f"test status {plan['_id']}"
                        elif plan_status == "passed":
                            return _response(
                                "success",
                                "Guided test workflow complete",
                                [
                                    f"Incident: {incident_id}",
                                    f"Test plan: {plan['_id']}",
                                    "Status: passed",
                                ],
                                {
                                    "incidentId": incident_id,
                                    "testPlanId": plan["_id"],
                                    "status": "passed",
                                    "guidedFlow": _guided_test_flow(db, incident_id, None),
                                },
                            )
                        else:
                            selected_command = f"test status {plan['_id']}"
                else:
                    selected_command = f"test generate {incident_id}"

            guided = await execute_patchy_command(selected_command, db, broker, actor=actor)
            guided_lines = [f"Patchy selected next step: {selected_command}", "", *(guided.get("lines") or [])]
            current_key = selected_command.split()[1] if selected_command.startswith("test ") else None
            return {
                **guided,
                "title": f"Guided test workflow · {guided.get('title', 'step complete')}",
                "lines": guided_lines,
                "data": serialize_mongo_doc(
                    {
                        **(guided.get("data") or {}),
                        "guidedCommand": selected_command,
                        "guidedFlow": _guided_test_flow(db, incident_id, current_key),
                    }
                ),
            }
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
    if args[0].lower() == "analyze":
        test_plan_id = args[1]
        try:
            analysis = await analyze_test_failure(test_plan_id, db, broker)
        except PatchyReasoningError as exc:
            raise PatchyCommandError(str(exc)) from exc
        return _response(
            "success",
            "Patchy test failure analysis",
            analysis["lines"],
            analysis,
        )

    if command == "discover":
        if len(args) != 2 or args[0].lower() != "endpoints":
            raise PatchyCommandError("Usage: discover endpoints <serviceAlias|url>")
        
        target_url = args[1]
        
        # 1. Run discovery suite
        endpoints = await run_sonic_discovery_suite(target_url)

        # 2. Format output block for terminal rendering
        output_lines = [f"Discovered {len(endpoints)} endpoints for {target_url}:"]
        for idx, ep in enumerate(endpoints, 1):
            line = f"  [{idx:02d}] {ep['method']:<5} {ep['url']} ({ep['source']})"
            output_lines.append(line)

            # Publish to real-time stream under service="patchy"
            await broker.publish(LogEventInput(
                service="patchy",
                level="info",
                message=line
            ))

        formatted_output = "\n".join(output_lines)

        # 3. Return formatted text directly to UI
        return {
            "status": "success",
            "message": formatted_output,
            "output": formatted_output,
            "data": endpoints
        }
    
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

    if command == "validate":
        if len(args) == 3 and args[0].lower() == "leakage":
            try:
                proposal = create_leakage_validation_proposal(args[1], args[2], actor, db)
            except PatchyFlowError as exc:
                raise PatchyCommandError(str(exc)) from exc
            return _response(
                "approval_required",
                "Approval required for response-leakage probe",
                [
                    proposal["summary"],
                    f"Endpoint: {proposal['action']['url']}",
                    "Patchy will send bounded inert syntax canaries only.",
                    "It checks for database implementation details in the response and stores redacted evidence.",
                    "Risk: staging only by default; no database enumeration or data access.",
                ],
                {"proposal": proposal},
            )
        if len(args) != 3 or args[0].lower() != "email":
            raise PatchyCommandError("Usage: validate email <bty|saapp> <path>")
        try:
            proposal = create_email_validation_proposal(args[1], args[2], actor, db)
        except PatchyFlowError as exc:
            raise PatchyCommandError(str(exc)) from exc
        return _response(
            "approval_required",
            "Approval required for email validation probe",
            [
                proposal["summary"],
                f"Endpoint: {proposal['action']['url']}",
                "Patchy will try bounded malformed email values and expect HTTP 422.",
                "Risk: staging only by default; no real side effects are allowed.",
            ],
            {"proposal": proposal},
        )

    if command == "probe":
        if len(args) == 4 and args[0].lower() == "validation" and args[1].lower() == "email":
            try:
                proposal = create_email_validation_proposal(args[2], args[3], actor, db)
            except PatchyFlowError as exc:
                raise PatchyCommandError(str(exc)) from exc
            return _response(
                "approval_required",
                "Approval required for email validation probe",
                [
                    proposal["summary"],
                    f"Endpoint: {proposal['action']['url']}",
                    "Patchy will try bounded malformed email values and expect HTTP 422.",
                    "Risk: staging only by default; no real side effects are allowed.",
                ],
                {"proposal": proposal},
            )
        if len(args) == 2 and args[0].lower() == "validation":
            try:
                proposal = create_validation_proposal(args[1], actor, db)
            except PatchyFlowError as exc:
                raise PatchyCommandError(str(exc)) from exc
            return _response(
                "approval_required",
                "Approval required for validation audit",
                [
                    proposal["summary"],
                    f"Service: {proposal['service']}",
                    f"Endpoint: {proposal['action']['url']}",
                    f"Fuzz flows: {proposal['action']['flowCount']}",
                    "Risk: bounded staging-only invalid-input probes; stops on first unexpected success or server error.",
                ],
                {"proposal": proposal},
            )
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

    if command == "flow":
        if not args:
            raise PatchyCommandError("Usage: flow define|list|run ...")
        subcommand = args[0].lower()
        if subcommand == "list":
            alias = args[1].lower() if len(args) > 1 else None
            try:
                flows = list_flow_plans(db, alias)
            except PatchyFlowError as exc:
                raise PatchyCommandError(str(exc)) from exc
            lines = [
                f"{flow['_id']} | {flow['service']} | {flow['name']} | {flow['status']} | {len(flow['steps'])} steps"
                for flow in flows
            ]
            return _response("success", f"Flow plans ({len(flows)})", lines or ["No flow plans saved yet."], flows)
        if subcommand == "define":
            if len(args) < 4:
                raise PatchyCommandError(
                    "Usage: flow define <bty|saapp> <name> simple <actions> | <json-actions> [--auth <json-auth>]"
                )
            alias = args[1].lower()
            name = args[2]
            action_parts = args[3:]
            auth = None
            if "--auth" in action_parts:
                flag_index = action_parts.index("--auth")
                auth_parts = action_parts[flag_index + 1:]
                action_parts = action_parts[:flag_index]
                if not auth_parts:
                    raise PatchyCommandError("--auth requires a JSON object")
                try:
                    auth = json.loads(" ".join(auth_parts))
                except json.JSONDecodeError as exc:
                    raise PatchyCommandError(f"Auth must be valid JSON: {exc}") from exc
            if not action_parts:
                raise PatchyCommandError("Actions JSON is required.")
            if action_parts[0].lower() == "simple":
                try:
                    actions = _parse_compact_flow_actions(action_parts[1:])
                except PatchyCommandError:
                    raise
            else:
                try:
                    actions = json.loads(" ".join(action_parts))
                except json.JSONDecodeError as exc:
                    raise PatchyCommandError(f"Actions must be valid JSON, or use 'simple': {exc}") from exc
            if not isinstance(actions, list):
                raise PatchyCommandError("Actions JSON must be an array of step objects.")
            try:
                flow = create_flow_plan(alias, name, actions, actor, db, auth=auth)
            except PatchyFlowError as exc:
                raise PatchyCommandError(str(exc)) from exc
            lines = [
                f"Flow ID: {flow['_id']}",
                f"Service: {flow['service']} ({flow['base_url']})",
                f"Auth: {(flow.get('auth') or {}).get('type', 'none')}",
                "Steps:",
                *[f"{i + 1}. {step['type']}{' ' + step.get('url', '') if step.get('url') else ''}" for i, step in enumerate(flow["steps"])],
                "",
                f"Run: flow run {flow['_id']}",
            ]
            return _response("success", "Patchy saved a flow plan", lines, {"flow": flow})
        if subcommand == "run":
            if len(args) != 2:
                raise PatchyCommandError("Usage: flow run <flow-id>")
            try:
                proposal = create_flow_proposal(args[1], actor, db)
            except PatchyFlowError as exc:
                raise PatchyCommandError(str(exc)) from exc
            action = proposal["action"]
            return _response(
                "approval_required",
                "Approval required for synthetic flow",
                [
                    proposal["summary"],
                    f"Base URL: {action['url']}",
                    f"Steps: {action['stepCount']}",
                    "Risk: multi-step HTTP flow against a registered service; GET/POST only to site-relative paths.",
                ],
                {"proposal": proposal},
            )
        raise PatchyCommandError("Usage: flow define|list|run ...")

    if command == "pentest":
        if len(args) < 2 or args[0].lower() != "sweep":
            raise PatchyCommandError("Usage: pentest sweep <bty|saapp> [target]")

        alias = args[1].lower()
        target = args[2].lower() if len(args) >= 3 else "full"

        try:
            proposal = create_pentest_sweep_proposal(alias, actor, db, target)
        except PatchyProposalError as exc:
            raise PatchyCommandError(str(exc)) from exc

        lines = [
            proposal["summary"],
            f"Service: {proposal['action']['serviceName']}",
            f"Alias: {proposal['action']['serviceAlias']}",
            f"Target: {proposal['action']['target']}",
            "Risk: synthetic or authenticated admin endpoints depending on target.",
            "Run: approve this proposal to start the sweep.",
        ]

        return _response(
            "approval_required",
            "Approval required for pentest sweep",
            lines,
            {"proposal": proposal},
        )



    if command == "clear":
        return _response("success", "Screen cleared", [])

    raise PatchyCommandError(f"Unknown command: {command}. Type 'help' for available commands.")
