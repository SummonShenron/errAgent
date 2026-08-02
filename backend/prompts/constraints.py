# backend/prompts/incident_prompts.py

INCIDENT_ANALYSIS_PROMPT = """
You are an expert Reliability Engineer & Incident Post-Mortem Analyst.

Analyze the following incoming error log and git diff to determine the root cause of the system failure.

ERROR STACK TRACE:
{stack_trace}

RECENT GIT COMMIT DIFFS:
{git_diffs}

INSTRUCTIONS:
1. Identify the exact line of code or logic change in the git diff that caused the stack trace.
2. Formulate a concise root cause explanation.
3. Suggest an immediate code fix.
4. Assign a confidence score between 0.0 and 1.0.

Respond strictly in JSON matching this schema:
{{
  "root_cause": "string",
  "suspect_commit": {{
    "commit_sha": "string",
    "author": "string",
    "message": "string",
    "diff_snippet": "string"
  }},
  "suggested_fix": "string",
  "confidence_score": 0.95
}}
"""