// Server-side only. Never expose ERRAGENT_INGEST_SECRET in browser code.
export type ErrAgentLogLevel = "info" | "warn" | "error";

export type ErrAgentLog = {
  level: ErrAgentLogLevel;
  message: string;
  context?: Record<string, unknown>;
  timestamp?: number | string;
};

type ServerRuntime = typeof globalThis & {
  process?: { env?: Record<string, string | undefined> };
};

const env = (globalThis as ServerRuntime).process?.env ?? {};
const errAgentUrl = env.ERRAGENT_URL?.replace(/\/$/, "");
const ingestSecret = env.ERRAGENT_INGEST_SECRET;
const service = env.ERRAGENT_SERVICE;

export async function emitErrAgentLog(log: ErrAgentLog): Promise<boolean> {
  if (!errAgentUrl || !ingestSecret || !service) return false;

  try {
    const response = await fetch(`${errAgentUrl}/api/v1/logs`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-ingest-secret": ingestSecret,
      },
      body: JSON.stringify({
        service,
        level: log.level,
        message: log.message,
        context: log.context ?? {},
        timestamp: log.timestamp ?? Date.now(),
      }),
      signal: AbortSignal.timeout(3000),
    });

    return response.ok;
  } catch {
    // Observability must never break the application workflow it observes.
    return false;
  }
}
