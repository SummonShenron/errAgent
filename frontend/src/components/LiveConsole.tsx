import { useEffect, useMemo, useRef, useState } from 'react';

type LogLevel = 'info' | 'warn' | 'error';
type LevelFilter = 'all' | LogLevel;
type ConnectionState = 'connecting' | 'live' | 'paused' | 'error';

type LogEntry = {
  id: string;
  service: string;
  level: LogLevel;
  message: string;
  timestamp: string;
  context?: Record<string, unknown>;
};

type StreamMessage =
  | { type: 'history'; entries: LogEntry[] }
  | { type: 'log'; entry: LogEntry };

type LiveConsoleProps = {
  open: boolean;
  onClose: () => void;
  apiBaseUrl: string;
  getToken: () => Promise<string | null>;
};

const MAX_RENDERED_LOGS = 1000;

function buildWebSocketUrl(apiBaseUrl: string, service: string, level: LevelFilter) {
  const url = new URL(apiBaseUrl, window.location.origin);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.pathname = `${url.pathname.replace(/\/$/, '')}/live-logs`;
  url.search = new URLSearchParams({
    service,
    history: '500',
    ...(level === 'all' ? {} : { level }),
  }).toString();
  return url.toString();
}

function formatTimestamp(timestamp: string) {
  const date = new Date(timestamp);
  return Number.isNaN(date.getTime())
    ? timestamp
    : date.toLocaleTimeString([], { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

export function LiveConsole({ open, onClose, apiBaseUrl, getToken }: LiveConsoleProps) {
  const [service, setService] = useState('SAAPP');
  const [level, setLevel] = useState<LevelFilter>('all');
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [paused, setPaused] = useState(false);
  const [connectionState, setConnectionState] = useState<ConnectionState>('connecting');
  const viewportRef = useRef<HTMLDivElement>(null);
  const getTokenRef = useRef(getToken);

  useEffect(() => {
    getTokenRef.current = getToken;
  }, [getToken]);

  const statusLabel = useMemo(() => {
    if (paused) return 'Paused';
    if (connectionState === 'live') return 'Live';
    if (connectionState === 'error') return 'Disconnected';
    return 'Connecting';
  }, [connectionState, paused]);

  useEffect(() => {
    if (!open || paused) {
      setConnectionState(paused ? 'paused' : 'connecting');
      return;
    }

    let disposed = false;
    let socket: WebSocket | null = null;

    const connect = async () => {
      setConnectionState('connecting');
      const token = await getTokenRef.current();
      if (disposed) return;
      if (!token) {
        setConnectionState('error');
        return;
      }

      socket = new WebSocket(buildWebSocketUrl(apiBaseUrl, service, level));
      socket.onopen = () => {
        socket?.send(JSON.stringify({ type: 'auth', token }));
      };
      socket.onmessage = (event) => {
        const message = JSON.parse(event.data) as StreamMessage;
        setConnectionState('live');
        if (message.type === 'history') {
          setLogs(message.entries.slice(-MAX_RENDERED_LOGS));
        } else if (message.type === 'log') {
          setLogs((current) => [...current, message.entry].slice(-MAX_RENDERED_LOGS));
        }
      };
      socket.onerror = () => setConnectionState('error');
      socket.onclose = () => {
        if (!disposed) setConnectionState('error');
      };
    };

    void connect();
    return () => {
      disposed = true;
      socket?.close();
    };
  }, [apiBaseUrl, level, open, paused, service]);

  useEffect(() => {
    if (!paused) {
      viewportRef.current?.scrollTo({ top: viewportRef.current.scrollHeight });
    }
  }, [logs, paused]);

  useEffect(() => {
    if (!open) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [onClose, open]);

  if (!open) return null;

  return (
    <div className="console-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className="live-console" role="dialog" aria-modal="true" aria-labelledby="live-console-title">
        <header className="console-header">
          <div>
            <p className="eyebrow">Ecosystem telemetry</p>
            <h2 id="live-console-title">Live Console</h2>
          </div>
          <div className="console-header-actions">
            <span className={`console-connection ${connectionState}`}>{statusLabel}</span>
            <button type="button" className="console-icon-button" onClick={onClose} title="Close console" aria-label="Close console">×</button>
          </div>
        </header>

        <div className="console-toolbar">
          <label>
            <span>Service</span>
            <select value={service} onChange={(event) => setService(event.target.value)}>
              <option value="SAAPP">SAAPP</option>
              <option value="BTY">BTY</option>
            </select>
          </label>

          <div className="console-levels" role="group" aria-label="Log level">
            {(['all', 'error', 'warn', 'info'] as LevelFilter[]).map((option) => (
              <button
                key={option}
                type="button"
                className={level === option ? 'active' : ''}
                onClick={() => setLevel(option)}
              >
                {option}
              </button>
            ))}
          </div>

          <button
            type="button"
            className="console-stream-toggle"
            onClick={() => setPaused((current) => !current)}
            title={paused ? 'Resume stream' : 'Pause stream'}
          >
            <span aria-hidden="true">{paused ? '▶' : 'Ⅱ'}</span>
            {paused ? 'Resume' : 'Pause'}
          </button>

          <button type="button" className="console-clear" onClick={() => setLogs([])}>Clear</button>
        </div>

        <div className="console-viewport" ref={viewportRef} aria-live={paused ? 'off' : 'polite'}>
          {logs.length === 0 ? (
            <div className="console-empty">Waiting for {service} logs...</div>
          ) : (
            logs.map((entry) => (
              <article key={entry.id} className={`console-line level-${entry.level}`}>
                <time dateTime={entry.timestamp}>{formatTimestamp(entry.timestamp)}</time>
                <span className="console-level">{entry.level}</span>
                <div className="console-message">
                  <span>{entry.message}</span>
                  {entry.context && Object.keys(entry.context).length > 0 && (
                    <details>
                      <summary>Context</summary>
                      <pre>{JSON.stringify(entry.context, null, 2)}</pre>
                    </details>
                  )}
                </div>
              </article>
            ))
          )}
        </div>

        <footer className="console-footer">
          <span>{logs.length} buffered lines</span>
          <span>Last 500 loaded on connect</span>
        </footer>
      </section>
    </div>
  );
}
