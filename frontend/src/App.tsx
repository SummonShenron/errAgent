import React, { useEffect, useState, useCallback } from 'react';
import { SignedIn, SignedOut, SignInButton, UserButton } from '@clerk/clerk-react';
import { useAppUser } from './context/Clerk';
import { CodeDiffView } from './components/CodeDiffView';
import { useDynamicFavicon } from './hooks/useDynamicFavicon';

const isLocalHost = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';
const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL || (isLocalHost ? 'http://127.0.0.1:8000/api/v1' : '/api/v1');

type Incident = {
  _id: string;
  service_name?: string;
  status?: string;
  environment?: string;
  error_message?: string;
  stack_trace?: string;
  repository?: string;
  created_at?: string;
  metadata?: Record<string, unknown>;
};

type IncidentAnalysis = {
  root_cause?: string;
  root_cause_summary?: string;
  summary?: string;
  confidence_score?: number;
  severity?: string;
  suggested_fix?: string;
};

type IncidentRemediation = {
  _id?: string;
  status?: string;
  failure_reason?: string;
  target_repo?: string;
  base_branch?: string;
  head_branch?: string;
  pr_title?: string;
  pr_body?: string;
  code_patch?: string;
  pr_url?: string;
  pr_number?: number;
  approved_by?: string;
  updated_at?: string;
  target_file_path?: string;
};

type IncidentDetailResponse = {
  incident?: Incident;
  analysis?: IncidentAnalysis;
  remediation?: IncidentRemediation;
};

const statusTone: Record<string, string> = {
  open: 'status-open',
  analyzing: 'status-analyzing',
  fix_proposed: 'status-fix',
  resolved: 'status-resolved',
  closed: 'status-closed',
};

function PatchyBrandMark({ activeIncidentsCount }: { activeIncidentsCount: number }) {
  const hasIncidents = activeIncidentsCount > 0;
  const eyeColor = hasIncidents ? '#EF4444' : '#38C2DE';
  const glowColor = hasIncidents ? 'rgba(239, 68, 68, 0.35)' : 'rgba(56, 194, 222, 0.35)';

  return (
    <div className="brand-mark" aria-label={hasIncidents ? `${activeIncidentsCount} active incidents` : 'No active incidents'}>
      <svg width="42" height="42" viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg">
        <circle cx="32" cy="32" r="28" fill={glowColor} />
        <line x1="32" y1="14" x2="32" y2="8" stroke="#8E95A2" strokeWidth="3" strokeLinecap="round" />
        <circle cx="32" cy="6" r="4" fill={eyeColor} />
        <rect x="14" y="14" width="36" height="30" rx="10" fill="#121316" stroke="#2A2A32" strokeWidth="2" />
        <rect x="10" y="24" width="4" height="10" rx="2" fill="#8E95A2" />
        <rect x="50" y="24" width="4" height="10" rx="2" fill="#8E95A2" />
        <rect x="18" y="18" width="28" height="22" rx="6" fill="#0C1016" stroke="#1F242D" />
        <circle cx="25" cy="28" r="3.5" fill={eyeColor} />
        <circle cx="39" cy="28" r="3.5" fill={eyeColor} />
        {hasIncidents ? (
          <circle cx="32" cy="34" r="2.5" fill={eyeColor} />
        ) : (
          <path d="M26 33 Q32 38 38 33" stroke={eyeColor} strokeWidth="2" strokeLinecap="round" fill="none" />
        )}
        <path d="M22 48 H42 V58 H22 Z" fill="#121316" stroke="#2A2A32" />
        <path d="M32 50 V56 M29 53 H35" stroke={eyeColor} strokeWidth="2" strokeLinecap="round" />
      </svg>
    </div>
  );
}

export default function App() {
  const { principal, getToken, isSignedIn } = useAppUser();
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [selectedIncidentId, setSelectedIncidentId] = useState<string | null>(null);
  const [selectedIncidentDetail, setSelectedIncidentDetail] = useState<IncidentDetailResponse | null>(null);
  const [detailLoading, setDetailLoading] = useState<boolean>(false);
  const [loading, setLoading] = useState<boolean>(true);
  const [isApproving, setIsApproving] = useState(false);
  const [reanalyzeInstructions, setReanalyzeInstructions] = useState('');
  const [isReanalyzing, setIsReanalyzing] = useState(false);
  const selectedIncident = incidents.find((inc) => inc._id === selectedIncidentId) || incidents[0] || null;
  const selectedIncidentFromDetail = selectedIncidentDetail?.incident || selectedIncident;
  const selectedAnalysis = selectedIncidentDetail?.analysis || null;
  const selectedRemediation = selectedIncidentDetail?.remediation || null;
  const activeIncidentsCount = incidents.filter((incident) => !['resolved', 'closed'].includes(incident.status ?? '')).length;
  const isIncidentAnalyzing = selectedIncidentFromDetail?.status === 'analyzing';
  const isPrMerged = selectedRemediation?.status === 'merged';
  useDynamicFavicon({ activeIncidentsCount });
  const [isMerging, setIsMerging] = useState(false);
  const [services, setServices] = useState<any[]>([]);
  const [healthResults, setHealthResults] = useState<any[]>([]);
  const [isCheckingHealth, setIsCheckingHealth] = useState(false);
  // --- DATA FETCHING & POLLING ---
  const fetchServices = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE_URL}/health/services`);
      if (res.ok) {
        const data = await res.json();
        setServices(data.services);
      }
    } catch (err) {
      console.error("Error fetching services:", err);
    }
  }, []);

  const runHealthCheck = async (serviceName?: string) => {
    setIsCheckingHealth(true);
    try {
      const body = serviceName ? { service: serviceName } : { service: "all" };

      const res = await fetch(`${API_BASE_URL}/health/check`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });

      if (res.ok) {
        const data = await res.json();
        setHealthResults(data.results);
      }
    } catch (err) {
      console.error("Error running health check:", err);
    } finally {
      setIsCheckingHealth(false);
    }
  };


  const fetchIncidents = useCallback(async () => {
    if (!isSignedIn) return;
    try {
      const token = await getToken();
      if (!token) return;

      const res = await fetch(`${API_BASE_URL}/incidents`, {
        headers: { Authorization: `Bearer ${token}` },
      });

      if (res.ok) {
        const data = await res.json();
        setIncidents(data);
        
        if (data.length > 0) {
          setSelectedIncidentId((prevId) => {
            const currentStillExists = data.some((inc: Incident) => inc._id === prevId);
            return currentStillExists ? prevId : data[0]._id;
          });
        }
      }
    } catch (err) {
      console.error('Error polling incidents:', err);
    } finally {
      setLoading(false);
    }
  }, [getToken, isSignedIn]);

  const fetchIncidentDetail = useCallback(async (incidentId: string) => {
    if (!isSignedIn || !incidentId) {
      setSelectedIncidentDetail(null);
      return;
    }
    try {
      setDetailLoading(true);
      const token = await getToken();
      if (!token) {
        setSelectedIncidentDetail(null);
        return;
      }
      const response = await fetch(`${API_BASE_URL}/incidents/${incidentId}`, {
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
      });

      if (response.ok) {
        const data = (await response.json()) as IncidentDetailResponse;
        setSelectedIncidentDetail(data);
      } else {
        setSelectedIncidentDetail(null);
      }
    } catch (err) {
      console.error('Error fetching incident details:', err);
      setSelectedIncidentDetail(null);
    } finally {
      setDetailLoading(false);
    }
  }, [getToken, isSignedIn]);

  useEffect(() => {
    fetchServices();
  }, [fetchServices]);

  // Real-time updates via a single SSE connection instead of polling.
  useEffect(() => {
    if (!isSignedIn) {
      setIncidents([]);
      setSelectedIncidentId(null);
      setLoading(false);
      return;
    }

    fetchIncidents();
    if (selectedIncidentId) {
      fetchIncidentDetail(selectedIncidentId);
    }

    const source = new EventSource(`${API_BASE_URL}/events`);

    source.onmessage = () => {
      fetchIncidents();
      if (selectedIncidentId) {
        fetchIncidentDetail(selectedIncidentId);
      }
    };

    source.onerror = () => {
      source.close();
    };

    return () => {
      source.close();
    };
  }, [fetchIncidents, fetchIncidentDetail, selectedIncidentId, isSignedIn]);

  // Fetch Incident Details when selection changes
  useEffect(() => {
    if (selectedIncidentId) {
      fetchIncidentDetail(selectedIncidentId);
    } else {
      setSelectedIncidentDetail(null);
    }
  }, [selectedIncidentId, fetchIncidentDetail]);

  // --- ACTIONS ---

  const handleMergeHotfix = async (incidentId: string) => {
    setIsMerging(true);
    try {
      const token = await getToken();
      if (!token) return;

      const response = await fetch(`${API_BASE_URL}/incidents/${incidentId}/merge-hotfix`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
      });
      if (response.ok) {
        alert(`Success! Hotfix merged into main branch.`);
        await fetchIncidents();
        await fetchIncidentDetail(incidentId);
      } else {
        const errData = await response.json().catch(() => ({}));
        alert(`Failed to merge: ${errData.detail || response.statusText}`);
      }
    } catch (err) {
      console.error('Error merging PR:', err);
      alert('An error occurred during merge.');
    } finally {
      setIsMerging(false);
    }
  };

  const handleReanalyzeWithInstructions = async (incidentId: string, instructions: string = '') => {
    setIsReanalyzing(true);
    try {
      const token = await getToken();
      if (!token) return;
      const response = await fetch(`${API_BASE_URL}/incidents/${incidentId}/reanalyze`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ instructions }),
      });
      if (response.ok) {
        alert(`Re-analysis triggered!`);
        setReanalyzeInstructions(''); 
        await fetchIncidents();
        await fetchIncidentDetail(incidentId);
      } else {
        const errData = await response.json().catch(() => ({}));
        alert(`Failed to trigger re-analysis: ${errData.detail || response.statusText}`);
      }
    } catch (err) {
      console.error('Error re-analyzing:', err);
      alert('An error occurred during re-analysis.');
    } finally {
      setIsReanalyzing(false);
    }
  };

  const handleRetryAnalysis = async (incidentId: string) => {
    await handleReanalyzeWithInstructions(incidentId, '');
  };

  const handleApproveHotfix = async (incidentId: string) => {
    setIsApproving(true);
    try {
      const token = await getToken();
      if (!token) return;

      const response = await fetch(`${API_BASE_URL}/incidents/${incidentId}/approve-hotfix`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
      });
      if (response.ok) {
        const data = await response.json();
        alert(`Hotfix Approved! GitHub PR Created: ${data.pr_url || 'Success'}`);   
        
        setIncidents((prev) =>
          prev.map((inc) =>
            inc._id === incidentId ? { ...inc, status: 'resolved' } : inc
          )
        );
        await fetchIncidents();
        await fetchIncidentDetail(incidentId);
      } else {
        const errData = await response.json().catch(() => ({}));
        alert(`Failed to approve hotfix: ${errData.detail || response.statusText}`);
      }
    } catch (err) {
      console.error('Error approving hotfix:', err);
      alert('An error occurred while approving the hotfix.');
    } finally {
      setIsApproving(false);
    }
  };

  const handleDismiss = async (incidentId: string, e?: React.MouseEvent) => {
    if (e) e.stopPropagation();
    try {
      const token = await getToken();
      if (!token) return;
      const response = await fetch(`${API_BASE_URL}/incidents/${incidentId}`, {
        method: 'DELETE',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
      });
      if (response.ok) {
        setIncidents((prevIncidents) => {
          const nextIncidents = prevIncidents.filter((inc) => inc._id !== incidentId); 
          if (selectedIncidentId === incidentId) {
            setSelectedIncidentId(nextIncidents.length > 0 ? nextIncidents[0]._id : null);
          }
          return nextIncidents;
        });
      } else {
        console.error('Failed to dismiss incident:', response.statusText);
      }
    } catch (err) {
      console.error('Error dismissing incident:', err);
    }
  };

  // --- RENDER ---

  if (!isSignedIn) {
    return (
      <div className="landing-shell">
        <section className="landing-card">
          <p className="eyebrow">Incident Operations Console</p>
          <h1>errAgent</h1>
          <p className="landing-copy">
            Private incident-response workspace for approved operators. Sign in with your Clerk account to review active incidents,
            AI analysis, and remediation drafts.
          </p>
          <div className="landing-actions">
            <SignInButton mode="modal">
              <button type="button" className="primary-action">Sign In</button>
            </SignInButton>
          </div>
          <p className="landing-note">Access is limited to authorized responders and reviewers.</p>
        </section>
      </div>
    );
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="topbar-brand">
          <PatchyBrandMark activeIncidentsCount={activeIncidentsCount} />
          <div>
            <p className="eyebrow">Incident Operations Console</p>
            <h1>errAgent Dashboard</h1>
          </div>
        </div>
        <div className="auth-chip">
          <SignedIn>
            <UserButton />
          </SignedIn>
          <SignedOut>
            <SignInButton mode="modal" />
          </SignedOut>
        </div>
      </header>
      <section className="session-band">
        <span className="session-label">Active Session Principal</span>
        <span className="session-value">{principal || 'Guest Sandbox Mode'}</span>
      </section>
      <div className="health-panel">
  <h2>Connected Apps Health</h2>

  <div className="health-actions">
    <button
      disabled={isCheckingHealth}
      onClick={() => runHealthCheck()}
    >
      Check All Services
    </button>
  </div>

  <div className="services-list">
    {services.map((svc) => (
      <div key={svc.name} className="service-item">
        <div className="service-info">
          <strong>{svc.name}</strong>
          <span className="service-url">{svc.url}</span>
          <span className={`service-status ${svc.status || 'unknown'}`}>
            {svc.status ? svc.status.toUpperCase() : 'UNKNOWN'}
            {svc.latency_ms != null ? ` • ${svc.latency_ms}ms` : ''}
          </span>
          {svc.last_checked_at && (
            <span className="service-last-check">
              Last check: {new Date(svc.last_checked_at).toLocaleString()}
            </span>
          )}
        </div>

        <button
          disabled={isCheckingHealth}
          onClick={() => runHealthCheck(svc.name)}
        >
          Check
        </button>
      </div>
    ))}
  </div>
    {healthResults.length > 0 && (
      <div className="health-results">
        <h3>Latest Health Check Results</h3>
        {healthResults.map((r) => (
          <div key={r.service} className={`health-result ${r.status}`}>
            <strong>{r.service}</strong> — {r.status.toUpperCase()}
            <div>Latency: {r.latency_ms ?? "N/A"} ms</div>
            <div>HTTP: {r.http_status ?? "N/A"}</div>
          </div>
        ))}
      </div>
    )}
  </div>
      <main className="dashboard-grid">
        <section className="panel">
          <div className="panel-header">
            <h2>Ingested Incidents</h2>
            <span className="count-pill">{incidents.length}</span>
          </div>
          {loading ? (
            <p className="muted">Loading incidents from backend...</p>
          ) : incidents.length === 0 ? (
            <p className="muted">No incidents found. Run seed_db.py on backend or trigger an error webhook.</p>
          ) : (
            <ul className="incident-list">
              {incidents.map((inc) => {
                const tone = statusTone[inc.status || ''] || 'status-open';
                const isActive = selectedIncident?._id === inc._id;
                return (
                  <li key={inc._id}>
                    <div className={`incident-card ${isActive ? 'active' : ''}`} onClick={() => setSelectedIncidentId(inc._id)}>
                      <div className="incident-card-top">
                        <span className={`status-chip ${tone}`}>{(inc.status || 'open').replace('_', ' ').toUpperCase()}</span>
                        <span className="service-name">{inc.service_name || 'unknown-service'}</span>         
                        <div style={{ display: 'flex', gap: '0.35rem', alignItems: 'center' }}>
                          {inc.status === 'analysis_failed' && (
                            <button
                              type="button"
                              className="retry-analysis-btn"
                              title="Retry Analysis"
                              onClick={(e) => {
                                e.stopPropagation();
                                handleRetryAnalysis(inc._id);
                              }}
                              style={{
                                padding: '0.3rem 0.7rem',
                                fontSize: '0.68rem',
                                fontWeight: 800,
                                lineHeight: 1.2,
                                letterSpacing: '0.04em',
                                textTransform: 'uppercase',
                                borderRadius: '999px',
                                border: '1px solid rgba(96, 165, 250, 0.9)',
                                background: 'rgba(37, 99, 235, 0.18)',
                                color: '#dbeafe',
                                boxShadow: 'inset 0 0 0 1px rgba(147, 197, 253, 0.18)',
                              }}
                            >
                              Retry
                            </button>
                          )}
                          <button
                            type="button"
                            className="dismiss-btn"
                            title="Dismiss Incident"
                            onClick={(e) => handleDismiss(inc._id, e)}
                          >
                            ✕
                          </button>
                        </div>
                      </div>
                      <p className="incident-error">{inc.error_message || 'Unhandled exception'}</p>
                      {inc.status === 'analysis_failed' && (
                        <p className="muted" style={{ marginTop: '0.5rem' }}>
                          Analysis failed. Retry when ready.
                        </p>
                      )}
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </section>
        <section className="panel detail-panel">
          <div className="panel-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <h2>Incident Details</h2>
            {selectedIncident && (
              <button
                type="button"
                className="dismiss-incident-btn"
                onClick={() => handleDismiss(selectedIncident._id)}
              >
                <span aria-hidden="true">✕</span>
                <span>Dismiss Incident</span>
              </button>
            )}
          </div>
          {!selectedIncident ? (
            <p className="muted">Pick an incident to see full context.</p>
          ) : (
            <>
              <div className="detail-grid">
                <div>
                  <label>Incident ID</label>
                  <p>{selectedIncidentFromDetail?._id || 'n/a'}</p>
                </div>
                <div>
                  <label>Service</label>
                  <p>{selectedIncidentFromDetail?.service_name || 'unknown-service'}</p>
                </div>
                <div>
                  <label>Environment</label>
                  <p>{selectedIncidentFromDetail?.environment || 'production'}</p>
                </div>
                <div>
                  <label>Status</label>
                  <p>{(selectedIncidentFromDetail?.status || 'open').replace('_', ' ')}</p>
                </div>
                <div>
                  <label>Repository</label>
                  <p>{selectedIncidentFromDetail?.repository || selectedRemediation?.target_repo || 'n/a'}</p>
                </div>
                <div>
                  <label>Created At</label>
                  <p>{selectedIncidentFromDetail?.created_at ? new Date(selectedIncidentFromDetail.created_at).toLocaleString() : 'n/a'}</p>
                </div>
              </div>
              <div className="detail-block">
                <label>Error Message</label>
                <p>{selectedIncidentFromDetail?.error_message || 'No message available.'}</p>
              </div>
              <div className="detail-block">
                <label>Stack Trace</label>
                <pre>{selectedIncidentFromDetail?.stack_trace || 'No stack trace captured.'}</pre>
              </div>
              {isIncidentAnalyzing && (
                <div className="analysis-thinking-banner" role="status" aria-live="polite">
                  <div className="analysis-thinking-orb" aria-hidden="true" />
                  <div>
                    <p className="analysis-thinking-title">AI is analyzing this error</p>
                    <p className="analysis-thinking-subtitle">
                      Inspecting stack trace and generating root cause
                      <span className="analysis-thinking-dots" aria-hidden="true">
                        <span />
                        <span />
                        <span />
                      </span>
                    </p>
                  </div>
                </div>
              )}
              <div className="detail-block">
                <label>AI Root Cause Analysis</label>
                <p>{selectedAnalysis?.root_cause_summary || selectedAnalysis?.root_cause || selectedAnalysis?.summary || 'No analysis generated yet.'}</p>
              </div>
              <div className="detail-grid">
                <div>
                  <label>Confidence Score</label>
                  <p>{typeof selectedAnalysis?.confidence_score === 'number' ? `${Math.round(selectedAnalysis.confidence_score * 100)}%` : 'n/a'}</p>
                </div>
                <div>
                  <label>Severity</label>
                  <p>{selectedAnalysis?.severity || 'n/a'}</p>
                </div>
              </div>
              <div className="detail-block">
                <label>Suggested Fix</label>
                <p>{selectedAnalysis?.suggested_fix || 'Waiting for LLM remediation guidance.'}</p>
              </div>
              <div className="detail-block">
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.5rem' }}>
                  <label style={{ fontSize: '1.1rem', fontWeight: 'bold', color: '#61dafb', margin: 0 }}>
                    Proposed Code Changes
                  </label>
                  {selectedRemediation?.target_file_path && (
                    <span style={{ 
                      fontSize: '0.85rem', 
                      color: '#8b949e', 
                      fontFamily: 'monospace', 
                      backgroundColor: '#161b22', 
                      padding: '0.2rem 0.5rem', 
                      borderRadius: '4px', 
                      border: '1px solid #30363d' 
                    }}>
                      {selectedRemediation.target_file_path}
                    </span>
                  )}
                </div>    
                <CodeDiffView patch={selectedRemediation?.code_patch} />
                {selectedIncident?.status === 'fix_proposed' && (
                <div className="reanalyze-container" style={{ marginTop: '1.5rem', borderTop: '1px solid #333', paddingTop: '1rem' }}>
                  <label style={{ fontSize: '1.1rem', fontWeight: 'bold', color: '#61dafb' }}>
                    Refine with AI Instructions
                  </label>
                  <textarea
                    value={reanalyzeInstructions}
                    onChange={(e) => setReanalyzeInstructions(e.target.value)}
                    placeholder="e.g., Don't use HTTPException. Wrap the division operation in a try/except ZeroDivisionError block and call report_error."
                    style={{
                      width: '100%',
                      minHeight: '80px',
                      marginTop: '0.75rem',
                      padding: '0.75rem',
                      backgroundColor: '#0d1117',
                      color: '#ffffff',
                      border: '1px solid #30363d',
                      borderRadius: '6px',
                      fontFamily: 'sans-serif',
                      resize: 'vertical',
                    }}
                  />
                  <button
                    type="button"
                    className="reanalyze-btn"
                    disabled={isReanalyzing}
                    onClick={() => handleReanalyzeWithInstructions(selectedIncident._id, reanalyzeInstructions)}
                    style={{
                      width: '100%',
                      marginTop: '0.75rem',
                      padding: '0.75rem 1.25rem',
                      backgroundColor: '#4f46e5',
                      color: '#ffffff',
                      border: 'none',
                      borderRadius: '6px',
                      fontWeight: 'bold',
                      fontSize: '1rem',
                      cursor: isReanalyzing ? 'not-allowed' : 'pointer',
                      opacity: isReanalyzing ? 0.7 : 1,
                      transition: 'background-color 0.2s ease',
                    }}
                  >
                    {isReanalyzing ? 'Re-running Gemini...' : 'Regenerate PR Draft'}
                  </button>
                </div>
              )}
                {selectedIncident?.status === 'analysis_failed' && (
                  <div className="reanalyze-container" style={{ marginTop: '1.5rem', borderTop: '1px solid #333', paddingTop: '1rem' }}>
                    <label style={{ fontSize: '1.1rem', fontWeight: 'bold', color: '#61dafb' }}>
                      Retry Failed Analysis
                    </label>
                    <p className="muted" style={{ marginTop: '0.5rem' }}>
                      Retry the incident using the original payload, or add optional instructions above before rerunning.
                    </p>
                    <button
                      type="button"
                      className="retry-failed-analysis-btn"
                      disabled={isReanalyzing}
                      onClick={() => handleRetryAnalysis(selectedIncident._id)}
                    >
                      {isReanalyzing ? 'Retrying...' : 'Retry Failed Analysis'}
                    </button>
                  </div>
                )}
                <label style={{ marginTop: '1rem' }}>Proposed PR Details</label>
                <p><strong>Title:</strong> {selectedRemediation?.pr_title || 'No PR draft yet.'}</p>
                <p><strong>Status:</strong> {(selectedRemediation?.status || 'not_created').replace('_', ' ')}</p>
                {selectedRemediation?.failure_reason ? (
                  <p><strong>Failure Reason:</strong> {selectedRemediation.failure_reason}</p>
                ) : null}
                <p><strong>Branches:</strong> {(selectedRemediation?.head_branch || 'n/a')} {'->'} {(selectedRemediation?.base_branch || 'n/a')}</p>
                <p><strong>Repository:</strong> {selectedRemediation?.target_repo || selectedIncidentFromDetail?.repository || 'n/a'}</p>
                
                <pre>{selectedRemediation?.pr_body || 'No PR body generated yet.'}</pre>
              </div>
              {selectedRemediation?.pr_url ? (
                <div className="pr-actions-container" style={{ marginTop: '1.5rem', paddingTop: '1rem', borderTop: '1px solid #333' }}>
                  <p className={`pr-status-note ${isPrMerged ? 'merged' : ''}`}>
                    {isPrMerged ? '✓ Pull Request Merged into Main' : '✓ Pull Request Open on GitHub'}
                  </p>
                  <div className="pr-actions-row">
                    <a 
                      href={selectedRemediation.pr_url} 
                      target="_blank" 
                      rel="noreferrer"
                      className="secondary-action"
                      style={{ textAlign: 'center', padding: '0.75rem', background: '#21262d', color: '#c9d1d9', borderRadius: '6px', textDecoration: 'none', fontWeight: 'bold' }}
                    >
                      Review / Checkout on GitHub ↗
                    </a>
                    {!isPrMerged ? (
                      <button
                        type="button"
                        disabled={isMerging}
                        onClick={() => handleMergeHotfix(selectedIncident._id)}
                        className="auto-merge-btn"
                      >
                        {isMerging ? 'Merging...' : 'Auto-Merge into Main'}
                      </button>
                    ) : null}
                  </div>
                </div>
              ) : (
                selectedIncident?.status === 'fix_proposed' && (
                  <div className="proposed-pr-container" style={{ marginTop: '1.5rem', paddingTop: '1rem', borderTop: '1px solid #333' }}>
                    <button
                      type="button"
                      className="approve-btn"
                      disabled={isApproving}
                      onClick={() => handleApproveHotfix(selectedIncident._id)}
                      style={{
                        width: '100%',
                        padding: '0.75rem 1.25rem',
                        backgroundColor: '#1f6feb',
                        color: '#ffffff',
                        border: 'none',
                        borderRadius: '6px',
                        fontWeight: 'bold',
                        fontSize: '1rem',
                        cursor: isApproving ? 'not-allowed' : 'pointer',
                        opacity: isApproving ? 0.7 : 1,
                        transition: 'background-color 0.2s ease',
                      }}
                    >
                      {isApproving ? 'Creating Branch & Opening PR...' : 'Approve & Create GitHub PR'}
                    </button>
                  </div>
                )
              )}
            </>
          )}
        </section>
      </main>

      <section className="footer-note">
        <p>Live incident feed connected to your backend API and Clerk session.</p>
      </section>
    </div>
  );
}