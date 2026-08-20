import { FormEvent, useEffect, useRef, useState } from 'react';
import { PatchyTerminalMascot, type PatchyTerminalActivity } from './PatchyTerminalMascot';

type PatchyTerminalProps = {
  open: boolean;
  onClose: () => void;
  apiBaseUrl: string;
  getToken: () => Promise<string | null>;
};

type CommandStatus = 'success' | 'warning' | 'error' | 'running' | 'approval_required' | 'clarification_required';

type PatchyClarification = {
  id: string;
  question: string;
  options: Array<{ label: string; value: string }>;
};

type PatchyTestPlan = {
  summary: string;
  recommendations: Array<{
    test_file: string;
    test_name: string;
    rationale: string;
    command: string;
    confidence: number;
  }>;
  missing_information: string[];
  should_ask_operator: boolean;
};

type PatchyProposal = {
  _id: string;
  kind?: string;
  status: string;
  summary: string;
  risk: string;
  created_at?: string;
  action: {
    method: string;
    url: string;
    timeoutSeconds: number;
    samples?: number;
    branch?: string;
    file?: string;
    content?: string;
    question?: string;
    environment?: string;
  };
  workflow?: {
    id: string;
    goal: string;
    step: number;
    totalSteps: number;
  };
};

type TerminalEntry = {
  id: string;
  command?: string;
  status: CommandStatus;
  title: string;
  lines: string[];
  timestamp: string;
  proposal?: PatchyProposal;
  clarification?: PatchyClarification;
  generatedTest?: {
    _id: string;
    test_file: string;
    test_name: string;
    rationale: string;
    content: string;
    test_branch: string;
  };
  testPlan?: PatchyTestPlan;
  plan?: {
    _id: string;
    status: string;
    nextStepIndex: number;
    steps: Array<{
      index: number;
      command: string;
      reason: string;
      status: string;
      result?: {
        title?: string;
        lines?: string[];
      };
    }>;
  };
};

const QUICK_COMMANDS = ['plan verify bty stability', 'verify bty', 'incidents', 'list incidents resolved', 'probe', 'render status all', 'ops status all', 'help'];

export function PatchyTerminal({ open, onClose, apiBaseUrl, getToken }: PatchyTerminalProps) {
  const [command, setCommand] = useState('');
  const [running, setRunning] = useState(false);
  const [entries, setEntries] = useState<TerminalEntry[]>([
    {
      id: 'welcome',
      status: 'success',
      title: 'Patchy Terminal ready',
      lines: ["Type 'help' to list diagnostic commands. Arbitrary shell execution is disabled."],
      timestamp: new Date().toISOString(),
    },
  ]);
  const [history, setHistory] = useState<string[]>([]);
  const [historyIndex, setHistoryIndex] = useState(-1);
  const viewportRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const tokenPromiseRef = useRef<Promise<string | null> | null>(null);
  const tokenExpiresAtRef = useRef(0);
  const latestStatus = entries.length ? entries[entries.length - 1].status : 'success';
  const patchyActivity: PatchyTerminalActivity = running
    ? 'running'
    : latestStatus === 'approval_required'
      ? 'approval'
      : latestStatus === 'clarification_required'
        ? 'approval'
      : latestStatus === 'error'
        ? 'error'
        : latestStatus === 'warning'
          ? 'warning'
          : latestStatus === 'success'
            ? 'success'
            : 'idle';
  const patchyLabel = {
    idle: 'standing by',
    running: 'diagnosing',
    approval: 'awaiting approval',
    success: 'all clear',
    warning: 'concern detected',
    error: 'needs attention',
  }[patchyActivity];

  useEffect(() => {
    viewportRef.current?.scrollTo({ top: viewportRef.current.scrollHeight, behavior: 'smooth' });
  }, [entries]);

  useEffect(() => {
    if (!open) return;
    inputRef.current?.focus();
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [onClose, open]);

  const getOperatorToken = () => {
    if (!tokenPromiseRef.current || Date.now() >= tokenExpiresAtRef.current) {
      tokenExpiresAtRef.current = Date.now() + 30_000;
      tokenPromiseRef.current = getToken().catch((error) => {
        tokenPromiseRef.current = null;
        tokenExpiresAtRef.current = 0;
        throw error;
      });
    }
    return tokenPromiseRef.current;
  };

  const executeCommand = async (commandText: string) => {
    const nextCommand = commandText.trim();
    if (!nextCommand || running) return;

    if (nextCommand.toLowerCase() === 'clear') {
      setEntries([]);
      setCommand('');
      setHistory((current) => [...current, nextCommand]);
      setHistoryIndex(-1);
      return;
    }

    const entryId = crypto.randomUUID();
    setEntries((current) => [...current, {
      id: entryId,
      command: nextCommand,
      status: 'running',
      title: 'Running command',
      lines: ['Patchy is checking the system...'],
      timestamp: new Date().toISOString(),
    }]);
    setHistory((current) => [...current, nextCommand]);
    setHistoryIndex(-1);
    setCommand('');
    setRunning(true);

    try {
      const token = await getOperatorToken();
      if (!token) throw new Error('Operator session unavailable.');
      const response = await fetch(`${apiBaseUrl}/patchy/command`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ command: nextCommand }),
      });
      const body = await response.json().catch(() => ({}));
      if (response.status === 401) {
        tokenPromiseRef.current = null;
        tokenExpiresAtRef.current = 0;
      }
      if (!response.ok) throw new Error(body.detail || `Command failed: ${response.status}`);

      setEntries((current) => current.map((entry) => entry.id === entryId ? {
        ...entry,
        status: body.status || 'success',
        title: body.title || 'Command complete',
        lines: Array.isArray(body.lines) ? body.lines : [],
        timestamp: body.timestamp || new Date().toISOString(),
        proposal: body.data?.proposal,
        clarification: body.data?.clarification,
        generatedTest: body.data?.generatedTest,
        plan: Array.isArray(body.data?.plan?.steps) ? body.data.plan : undefined,
        testPlan: body.data?.plan?.recommendations ? body.data.plan : undefined,
      } : entry));
    } catch (error) {
      setEntries((current) => current.map((entry) => entry.id === entryId ? {
        ...entry,
        status: 'error',
        title: 'Command failed',
        lines: [String(error)],
        timestamp: new Date().toISOString(),
      } : entry));
    } finally {
      setRunning(false);
      window.setTimeout(() => inputRef.current?.focus(), 0);
    }
  };

  const submitCommand = (event: FormEvent) => {
    event.preventDefault();
    void executeCommand(command);
  };

  const approveProposal = async (entryId: string, proposalId: string) => {
    if (running) return;
    setRunning(true);
    setEntries((current) => current.map((entry) => entry.id === entryId ? {
      ...entry,
      status: 'running',
      title: 'Approved · running probe',
      lines: [...entry.lines, 'Operator approved execution.'],
    } : entry));

    try {
      const token = await getOperatorToken();
      if (!token) throw new Error('Operator session unavailable.');
      const response = await fetch(`${apiBaseUrl}/patchy/proposals/${proposalId}/approve`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}` },
      });
      const body = await response.json().catch(() => ({}));
      if (response.status === 401) {
        tokenPromiseRef.current = null;
        tokenExpiresAtRef.current = 0;
      }
      if (!response.ok) throw new Error(body.detail || `Approval failed: ${response.status}`);
      const result = body.result || {};
      const resultLines = body.status === 'succeeded'
        ? result.question
          ? [
              `HTTP ${result.httpStatus}`,
              `Answer: ${result.answer || result.body?.answer || JSON.stringify(result.body)}`,
            ]
          : result.samples
          ? [
              `Samples: ${result.samples.join(', ')}ms`,
              `Median: ${result.medianMs}ms`,
              `Maximum: ${result.maxMs}ms`,
            ]
          : [
              `HTTP ${result.httpStatus}`,
              `Elapsed: ${result.elapsedMs}ms`,
              `Response: ${JSON.stringify(result.body)}`,
            ]
        : [`Execution failed: ${result.error || `HTTP ${result.httpStatus || 'unknown'}`}`];
      setEntries((current) => current.map((entry) => entry.id === entryId ? {
        ...entry,
        status: body.status === 'succeeded' ? 'success' : 'error',
        title: body.status === 'succeeded'
          ? entry.proposal?.kind === 'synthetic_question' ? 'Synthetic question succeeded' : 'Approved probe succeeded'
          : entry.proposal?.kind === 'synthetic_question' ? 'Synthetic question failed' : 'Approved probe failed',
        lines: resultLines,
        proposal: { ...entry.proposal!, status: body.status },
        timestamp: body.completed_at || new Date().toISOString(),
      } : entry));

      if (body.nextProposal) {
        const next = body.nextProposal as PatchyProposal;
        setEntries((current) => [...current, {
          id: crypto.randomUUID(),
          command: `${next.workflow?.goal || 'verification'} · step ${next.workflow?.step || 2}`,
          status: 'approval_required',
          title: 'Next diagnostic step requires approval',
          lines: [
            next.summary,
            `Method: ${next.action.method}`,
            `URL: ${next.action.url}`,
            `Samples: ${next.action.samples || 1}`,
            'Patchy selected this step from the previous health evidence.',
          ],
          timestamp: next.created_at || new Date().toISOString(),
          proposal: next,
        }]);
      } else if (body.workflowReport) {
        setEntries((current) => [...current, {
          id: crypto.randomUUID(),
          status: body.workflowReport.status || 'warning',
          title: body.workflowReport.title || 'Verification complete',
          lines: body.workflowReport.lines || [],
          timestamp: new Date().toISOString(),
        }]);
      }
    } catch (error) {
      setEntries((current) => current.map((entry) => entry.id === entryId ? {
        ...entry,
        status: 'error',
        title: 'Approval failed',
        lines: [String(error)],
      } : entry));
    } finally {
      setRunning(false);
      window.setTimeout(() => inputRef.current?.focus(), 0);
    }
  };

  if (!open) return null;

  return (
    <div className="console-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className="live-console patchy-terminal" role="dialog" aria-modal="true" aria-labelledby="patchy-terminal-title">
        <header className="console-header">
          <div>
            <p className="eyebrow">Allowlisted diagnostics</p>
            <h2 id="patchy-terminal-title">Patchy Terminal</h2>
          </div>
          <div className="console-header-actions">
            <span className={`console-connection ${running ? 'connecting' : 'live'}`}>{running ? 'Running' : 'Ready'}</span>
            <button type="button" className="console-icon-button" onClick={onClose} title="Close Patchy Terminal" aria-label="Close Patchy Terminal">×</button>
          </div>
        </header>

        <div className="patchy-terminal-layout">
          <aside className={`patchy-terminal-mascot ${patchyActivity}`} aria-label={`Patchy is ${patchyLabel}`}>
            <PatchyTerminalMascot size={108} activity={patchyActivity} />
            <strong>PATCHY</strong>
            <span>{patchyLabel}</span>
          </aside>

          <div className="patchy-terminal-main">
            <div className="patchy-terminal-quick" aria-label="Quick commands">
              {QUICK_COMMANDS.map((item) => (
                <button key={item} type="button" onClick={() => void executeCommand(item)} disabled={running}>{item}</button>
              ))}
            </div>

            <div className="patchy-terminal-output" ref={viewportRef} aria-live="polite">
              {entries.length === 0 && <div className="console-empty">Terminal cleared.</div>}
              {entries.map((entry) => (
                <article key={entry.id} className={`patchy-command-result ${entry.status}`}>
                  {entry.command && <div className="patchy-command-input"><span>patchy&gt;</span> {entry.command}</div>}
                  <div className="patchy-command-title">
                    <span className="patchy-command-status" />
                    <strong>{entry.title}</strong>
                    <time>{new Date(entry.timestamp).toLocaleTimeString([], { hour12: false })}</time>
                  </div>
                  {entry.lines.length > 0 && <pre>{entry.lines.join('\n')}</pre>}
                  {entry.status === 'clarification_required' && entry.clarification && (
                    <div className="patchy-clarification-panel">
                      <strong>{entry.clarification.question}</strong>
                      <div>
                        {entry.clarification.options.map((option) => (
                          <button
                            key={option.value}
                            type="button"
                            onClick={() => void executeCommand(option.value)}
                            disabled={running}
                          >
                            {option.label}
                          </button>
                        ))}
                      </div>
                    </div>
                  )}
                  {entry.plan && (
                    <div className="patchy-plan-panel">
                      {entry.plan.steps.map((step) => (
                        <div key={step.index} className={`patchy-plan-step ${step.status}`}>
                          <span>{step.index + 1}</span>
                          <code>{step.command}</code>
                          <small>{step.reason}</small>
                          {step.result?.lines?.length ? (
                            <pre>{[step.result.title, ...step.result.lines].filter(Boolean).join('\n')}</pre>
                          ) : null}
                        </div>
                      ))}
                    </div>
                  )}
                  {entry.testPlan && (
                    <div className="patchy-test-plan-panel">
                      <strong>{entry.testPlan.summary}</strong>
                      {entry.testPlan.recommendations.map((recommendation) => (
                        <div key={recommendation.command} className="patchy-test-plan-item">
                          <code>{recommendation.command}</code>
                          <small>{Math.round(recommendation.confidence * 100)}% confidence · {recommendation.rationale}</small>
                        </div>
                      ))}
                    </div>
                  )}
                  {entry.generatedTest && (
                    <div className="patchy-generated-test-panel">
                      <div><strong>{entry.generatedTest.test_file}</strong><small>{entry.generatedTest.test_name} · {entry.generatedTest.test_branch}</small></div>
                      <p>{entry.generatedTest.rationale}</p>
                      <pre>{entry.generatedTest.content}</pre>
                    </div>
                  )}
                  {entry.status === 'approval_required' && entry.proposal && (
                    <div className="patchy-approval-panel">
                      <div>
                        <span>Exact action</span>
                        <code>{entry.proposal.action.method} {entry.proposal.action.url}</code>
                      </div>
                      <div>
                        <span>Risk</span>
                        <strong>{entry.proposal.risk.replace('_', ' ')}</strong>
                      </div>
                      {entry.proposal.action.content && (
                        <pre className="patchy-proposal-content">{entry.proposal.action.content}</pre>
                      )}
                      {entry.proposal.action.question && (
                        <div className="patchy-question-preview">
                          <strong>Question:</strong> {entry.proposal.action.question}
                          {entry.proposal.action.environment && <small>Environment: {entry.proposal.action.environment}</small>}
                        </div>
                      )}
                      <button
                        type="button"
                        onClick={() => void approveProposal(entry.id, entry.proposal!._id)}
                        disabled={running}
                      >
                        Approve &amp; Run
                      </button>
                    </div>
                  )}
                </article>
              ))}
            </div>

            <form className="patchy-terminal-input" onSubmit={submitCommand}>
              <span aria-hidden="true">patchy&gt;</span>
              <input
                ref={inputRef}
                value={command}
                onChange={(event) => setCommand(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'ArrowUp' && history.length) {
                    event.preventDefault();
                    const nextIndex = historyIndex < 0 ? history.length - 1 : Math.max(0, historyIndex - 1);
                    setHistoryIndex(nextIndex);
                    setCommand(history[nextIndex]);
                  } else if (event.key === 'ArrowDown' && historyIndex >= 0) {
                    event.preventDefault();
                    const nextIndex = historyIndex + 1;
                    if (nextIndex >= history.length) {
                      setHistoryIndex(-1);
                      setCommand('');
                    } else {
                      setHistoryIndex(nextIndex);
                      setCommand(history[nextIndex]);
                    }
                  }
                }}
                placeholder="health all"
                autoComplete="off"
                disabled={running}
              />
              <button type="submit" disabled={running || !command.trim()}>Run</button>
            </form>
          </div>
        </div>

        <footer className="console-footer">
          <span>Secure command allowlist enabled</span>
          <span>↑/↓ command history · Esc closes</span>
        </footer>
      </section>
    </div>
  );
}
