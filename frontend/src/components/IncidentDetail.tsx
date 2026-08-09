import React, { useEffect, useState } from 'react';
import { getIncidentDetail, approveHotfix, IncidentDetailResponse } from '../api';
import { PostMortemCard } from './PostMortemCard';
import { StackTraceViewer } from './StackTraceViewer';

interface IncidentDetailProps {
  incidentId: string | null;
  token: string | null;
}

export const IncidentDetail: React.FC<IncidentDetailProps> = ({ incidentId, token }) => {
  const [detail, setDetail] = useState<IncidentDetailResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [executing, setExecuting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!incidentId) return;

    async function fetchDetails() {
      setLoading(true);
      setError(null);
      try {
        const data = await getIncidentDetail(incidentId!, token);
        setDetail(data);
      } catch (err: any) {
        setError(err.message || 'Failed to fetch incident details');
      } finally {
        setLoading(false);
      }
    }

    fetchDetails();
  }, [incidentId, token]);

  const handleApprove = async () => {
    if (!incidentId) return;
    setExecuting(true);
    try {
      const res = await approveHotfix(incidentId, token);
      if (detail && res.pr_url) {
        setDetail({
          ...detail,
          remediation: {
            ...detail.remediation,
            status: 'executed',
            pr_url: res.pr_url,
          },
          incident: {
            ...detail.incident,
            status: 'resolved',
          },
        });
      }
    } catch (err: any) {
      alert(`Approval error: ${err.message}`);
    } finally {
      setExecuting(false);
    }
  };

  if (!incidentId) {
    return (
      <div style={{ padding: '2rem', textAlign: 'center', color: '#71717a' }}>
        Select an incident from the list to inspect details and AI remediation.
      </div>
    );
  }

  if (loading) return <p style={{ color: '#71717a' }}>Loading details for {incidentId}...</p>;
  if (error) return <p style={{ color: '#ef4444' }}>Error: {error}</p>;
  if (!detail) return null;

  return (
    <div style={{ padding: '1rem', borderLeft: '1px solid #e4e4e7' }}>
      <h2 style={{ marginTop: 0 }}>{detail.incident.service_name} Incident</h2>
      <p style={{ color: '#ef4444', fontWeight: 'bold' }}>{detail.incident.error_message}</p>

      {/* AI Post-Mortem and Hotfix Section */}
      <PostMortemCard
        analysis={detail.analysis}
        remediation={detail.remediation}
        onApproveHotfix={handleApprove}
        isExecuting={executing}
      />

      {/* Raw Stack Trace */}
      <StackTraceViewer stackTrace={detail.incident.stack_trace} />
    </div>
  );
};