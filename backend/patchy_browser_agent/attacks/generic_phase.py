# backend/patchy_browser_agent/attacks/generic_phase.py
import logging
import httpx
from urllib.parse import urlparse

logger = logging.getLogger("errAgent Logger")

SENSITIVE_KEYWORDS = [
    "admin", "config", "env", "backup", "metrics", "swagger", 
    "api-docs", "graphql", "v1", "v2", "db", "actuator"
]

REQUIRED_HEADERS = {
    "Strict-Transport-Security": "Missing HSTS header (vulnerable to MITM attacks)",
    "Content-Security-Policy": "Missing CSP header (vulnerable to XSS / injection)",
    "X-Content-Type-Options": "Missing X-Content-Type-Options header (MIME-sniffing risk)",
    "X-Frame-Options": "Missing X-Frame-Options header (vulnerable to Clickjacking)"
}

async def run_generic_phase(endpoints: list[dict], base_url: str) -> list[dict]:
    vulnerabilities = []
    base_domain = urlparse(base_url).netloc

    logger.info(f"[generic-phase] Auditing {len(endpoints)} discovered endpoints for {base_url}...")

    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True, verify=False) as client:
        # Deduplicate target URLs to audit
        target_urls = list({ep["url"] for ep in endpoints if base_domain in urlparse(ep["url"]).netloc})

        for url in target_urls:
            try:
                response = await client.get(url)
                headers = response.headers

                # 1. Check for Sensitive Endpoint Exposure (Unauthenticated 200 OK)
                url_path = urlparse(url).path.lower()
                for keyword in SENSITIVE_KEYWORDS:
                    if keyword in url_path and response.status_code == 200:
                        vulnerabilities.append({
                            "type": "Sensitive Path Exposure",
                            "severity": "MEDIUM",
                            "endpoint": url,
                            "description": f"Potentially sensitive path '{keyword}' returned HTTP 200 without authentication."
                        })
                        break

                # 2. Check for Missing HTTP Security Headers
                for header_name, issue_desc in REQUIRED_HEADERS.items():
                    if header_name not in headers:
                        vulnerabilities.append({
                            "type": "Missing Security Header",
                            "severity": "LOW",
                            "endpoint": url,
                            "description": f"{header_name}: {issue_desc}"
                        })

                # 3. Check for Permissive CORS Configurations
                cors_origin = headers.get("Access-Control-Allow-Origin")
                if cors_origin == "*":
                    vulnerabilities.append({
                        "type": "Permissive CORS Policy",
                        "severity": "MEDIUM",
                        "endpoint": url,
                        "description": "Access-Control-Allow-Origin is set to wildcard '*'."
                    })

                # 4. Check for Information Disclosure in Headers
                server_header = headers.get("Server")
                powered_by = headers.get("X-Powered-By")
                if server_header or powered_by:
                    tech_info = f"Server: {server_header}" if server_header else f"X-Powered-By: {powered_by}"
                    vulnerabilities.append({
                        "type": "Information Disclosure",
                        "severity": "INFO",
                        "endpoint": url,
                        "description": f"Leaking backend environment details ({tech_info})."
                    })

            except httpx.RequestError as req_err:
                logger.warning(f"[generic-phase] Could not probe {url}: {req_err}")

    logger.info(f"[generic-phase] Completed audit. Found {len(vulnerabilities)} vulnerabilities.")
    return vulnerabilities