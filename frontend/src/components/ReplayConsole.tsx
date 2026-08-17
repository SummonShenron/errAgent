import { useEffect, useRef, useState } from "react";

interface ReplayConsoleProps {
  open: boolean;
  onClose: () => void;
  apiBaseUrl: string;
  getToken: () => Promise<string | null>;
}

interface ReplayNode {
  node: string;
  input: Record<string, unknown>;
  output: Record<string, unknown>;
  timestamp: string;
}

function formatTimestamp(timestamp: string) {
  const date = new Date(timestamp);
  return Number.isNaN(date.getTime())
    ? timestamp
    : date.toLocaleTimeString([], {
        hour12: false,
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      });
}

export function ReplayConsole({ open, onClose, apiBaseUrl, getToken }: ReplayConsoleProps) {
  const [workflowName, setWorkflowName] = useState("sonic_assistant");
  const [requestId, setRequestId] = useState("");
  const [timeline, setTimeline] = useState<ReplayNode[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const viewportRef = useRef<HTMLDivElement>(null);

  const runReplay = async () => {
    setLoading(true);
    setError("");
    setTimeline([]);

    try {
      const token = await getToken();
      const res = await fetch(`${apiBaseUrl}/replay`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ workflowName, requestId }),
      });

      if (!res.ok) {
        throw new Error(`Replay failed: ${res.status}`);
      }

      const data = await res.json();
      setTimeline(data.timeline || []);
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    viewportRef.current?.scrollTo({
      top: viewportRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [timeline]);

  if (!open) return null;

  return (
    <div
      className="console-backdrop"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <section className="live-console" role="dialog" aria-modal="true">
        <header className="console-header">
          <div>
            <p className="eyebrow">Ecosystem telemetry</p>
            <h2>Workflow Replay</h2>
          </div>
          <div className="console-header-actions">
            <button
              type="button"
              className="console-icon-button"
              onClick={onClose}
              title="Close console"
              aria-label="Close console"
            >
              ×
            </button>
          </div>
        </header>

        <div className="console-toolbar">
          <label>
            <span>Workflow</span>
            <select
              value={workflowName}
              onChange={(e) => setWorkflowName(e.target.value)}
            >
              <option value="sonic_assistant">Sonic Assistant</option>
              <option value="bty_workflow">BTY Workflow</option>
              <option value="erragent_pipeline">ErrAgent Pipeline</option>
            </select>
          </label>

          <label>
            <span>Request ID</span>
            <input
              value={requestId}
              onChange={(e) => setRequestId(e.target.value)}
              placeholder="req_2026_08_17_abc123"
            />
          </label>

          <button
            type="button"
            className="console-stream-toggle"
            onClick={runReplay}
            disabled={loading}
          >
            {loading ? "Replaying…" : "Replay"}
          </button>

          <button
            type="button"
            className="console-clear"
            onClick={() => {
              setTimeline([]);
              setError("");
            }}
          >
            Clear
          </button>
        </div>

        <div className="console-viewport" ref={viewportRef}>
          {error && <div className="console-error">{error}</div>}

          {!error && timeline.length === 0 && !loading && (
            <div className="console-empty">No replay data yet.</div>
          )}

          {loading && (
            <div className="console-empty">Replaying workflow…</div>
          )}

          {timeline.length > 0 &&
            timeline.map((node, idx) => (
              <article key={idx} className="console-line level-info">
                <time dateTime={node.timestamp}>
                  {formatTimestamp(node.timestamp)}
                </time>
                <span className="console-level">node</span>

                <div className="console-message">
                  <strong>{node.node}</strong>

                  <details>
                    <summary>Input</summary>
                    <pre>{JSON.stringify(node.input, null, 2)}</pre>
                  </details>

                  <details>
                    <summary>Output</summary>
                    <pre>{JSON.stringify(node.output, null, 2)}</pre>
                  </details>
                </div>
              </article>
            ))}
        </div>

        <footer className="console-footer">
          <span>{timeline.length} nodes replayed</span>
        </footer>
      </section>
    </div>
  );
}
