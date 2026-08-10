import React, { useEffect, useState } from 'react';
import { SignedIn, SignedOut, SignInButton, UserButton } from '@clerk/clerk-react';
import { useAppUser } from './context/Clerk';
import { CodeDiffView } from './components/CodeDiffView';

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
  status?: string;
  target_repo?: string;
  base_branch?: string;
  head_branch?: string;
  pr_title?: string;
  pr_body?: string;
  code_patch?: string;
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

export default function App() {
  const { principal, getToken, isSignedIn } = useAppUser();
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [selectedIncidentId, setSelectedIncidentId] = useState<string | null>(null);
  const [selectedIncidentDetail, setSelectedIncidentDetail] = useState<IncidentDetailResponse | null>(null);
  const [detailLoading, setDetailLoading] = useState<boolean>(false);
  const [loading, setLoading] = useState<boolean>(true);
  const selectedIncident = incidents.find((inc) => inc._id === selectedIncidentId) || incidents[0] || null;
  const selectedIncidentFromDetail = selectedIncidentDetail?.incident || selectedIncident;
  const selectedAnalysis = selectedIncidentDetail?.analysis || null;
  const selectedRemediation = selectedIncidentDetail?.remediation || null;
  const [isApproving, setIsApproving] = useState(false);
  const [reanalyzeInstructions, setReanalyzeInstructions] = useState('');
  const [isReanalyzing, setIsReanalyzing] = useState(false);
  const handleReanalyzeWithInstructions = async (incidentId: string) => {
    if (!reanalyzeInstructions.trim()) {
      alert("Please provide instructions first!");
      return;
    }
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
        body: JSON.stringify({ instructions: reanalyzeInstructions }),
      });
      if (response.ok) {
        alert(`Re-analysis triggered! Check back in a few seconds.`);
        setReanalyzeInstructions(''); // Clear input
        // Optionally reset view or status locally
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
        // Update local state to reflect resolved status
        setIncidents((prev) =>
          prev.map((inc) =>
            inc._id === incidentId ? { ...inc, status: 'resolved' } : inc
          )
        );
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
    // Prevent event bubbling if clicked inside incident card button
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
          // If we dismissed the currently active incident, automatically shift selection
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

  useEffect(() => {
    async function fetchIncidents() {
      if (!isSignedIn) {
        setIncidents([]);
        setSelectedIncidentId(null);
        setLoading(false);
        return;
      }
      try {
        const token = await getToken();
        if (!token) {
          setLoading(false);
          return;
        }
        const response = await fetch(`${API_BASE_URL}/incidents`, {
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${token}`,
          },
        });
        if (response.ok) {
          const data = await response.json();
          setIncidents(data);
          if (data.length > 0) {
            setSelectedIncidentId(data[0]._id);
          }
        }
      } catch (err) {
        console.error('Error fetching incidents:', err);
      } finally {
        setLoading(false);
      }
    }
    fetchIncidents();
  }, [getToken, isSignedIn]);

  useEffect(() => {
    async function fetchIncidentDetail() {
      if (!isSignedIn || !selectedIncidentId) {
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
        const response = await fetch(`${API_BASE_URL}/incidents/${selectedIncidentId}`, {
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
    }
    fetchIncidentDetail();
  }, [selectedIncidentId, getToken, isSignedIn]);

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
        <div>
          <p className="eyebrow">Incident Operations Console</p>
          <h1>errAgent Dashboard</h1>
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
                        {/* Dismiss Button */}
                        <button
                          type="button"
                          className="dismiss-btn"
                          title="Dismiss Incident"
                          onClick={(e) => handleDismiss(inc._id, e)}
                        >
                          ✕
                        </button>
                      </div>
                      <p className="incident-error">{inc.error_message || 'Unhandled exception'}</p>
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
                className="secondary-action"
                style={{ padding: '0.4rem 0.8rem', fontSize: '0.85rem', cursor: 'pointer' }}
                onClick={() => handleDismiss(selectedIncident._id)}
              >
                Dismiss Incident
              </button>
            )}
          </div>
          {!selectedIncident ? (
            <p className="muted">Pick an incident to see full context.</p>
          ) : (
            <>
              {detailLoading && <p className="muted">Refreshing analysis and remediation data...</p>}
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
                <label style={{ fontSize: '1.1rem', fontWeight: 'bold', color: '#61dafb' }}>
                  Proposed Code Changes
                </label>    
                {/* Renders the diff with GitHub-like red/green highlights */}
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
                    onClick={() => handleReanalyzeWithInstructions(selectedIncident._id)}
                    style={{
                      width: '100%',
                      marginTop: '0.75rem',
                      padding: '0.75rem 1.25rem',
                      backgroundColor: '#4f46e5', // Indigo
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
                    {isReanalyzing ? '⏳ Re-running Gemini...' : '🤖 Regenerate PR Draft'}
                  </button>
                </div>
              )}
                <label style={{ marginTop: '1rem' }}>Proposed PR Details</label>
                <p><strong>Title:</strong> {selectedRemediation?.pr_title || 'No PR draft yet.'}</p>
                <p><strong>Status:</strong> {(selectedRemediation?.status || 'not_created').replace('_', ' ')}</p>
                <p><strong>Branches:</strong> {(selectedRemediation?.head_branch || 'n/a')} {'->'} {(selectedRemediation?.base_branch || 'n/a')}</p>
                <p><strong>Repository:</strong> {selectedRemediation?.target_repo || selectedIncidentFromDetail?.repository || 'n/a'}</p>
                
                <pre>{selectedRemediation?.pr_body || 'No PR body generated yet.'}</pre>
              </div>
              {selectedIncident?.status === 'fix_proposed' && (
                <div className="proposed-pr-container" style={{ marginTop: '1.5rem', paddingTop: '1rem', borderTop: '1px solid #333' }}>
                  <button
                    type="button"
                    className="approve-btn"
                    disabled={isApproving}
                    onClick={() => handleApproveHotfix(selectedIncident._id)}
                    style={{
                      width: '100%',
                      padding: '0.75rem 1.25rem',
                      backgroundColor: '#10b981', // Success green
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
                    {isApproving ? 'Opening GitHub Pull Request...' : 'Approve & Create GitHub PR'}
                  </button>
                </div>
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