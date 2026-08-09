import React from 'react';
import { Analysis, Remediation } from '../api';

interface PostMortemCardProps {
  analysis?: Analysis;
  remediation?: Remediation;
  onApproveHotfix: () => void;
  isExecuting: boolean;
}

export const PostMortemCard: React.FC<PostMortemCardProps> = ({
  analysis,
  remediation,
  onApproveHotfix,
  isExecuting,
}) => {
  return (
    <div
      style={{
        border: '1px solid #3f3f46',
        backgroundColor: '#18181b',
        color: '#f4f4f5',
        borderRadius: '8px',
        padding: '1.25rem',
        marginBottom: '1.5rem',
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
        <h3 style={{ margin: 0, fontSize: '1.1rem', color: '#38C2DE' }}>🤖 AI Post-Mortem & Remediation</h3>
        {analysis?.severity && (
          <span
            style={{
              fontSize: '0.75rem',
              fontWeight: 'bold',
              textTransform: 'uppercase',
              padding: '0.2rem 0.5rem',
              borderRadius: '4px',
              backgroundColor: analysis.severity.toLowerCase() === 'high' ? '#7f1d1d' : '#3f3f46',
              color: '#fef2f2',
            }}
          >
            {analysis.severity} Severity
          </span>
        )}
      </div>

      {/* Root Cause Section */}
      <div style={{ marginBottom: '1rem' }}>
        <strong style={{ fontSize: '0.85rem', color: '#a1a1aa' }}>Root Cause Analysis:</strong>
        <p style={{ margin: '0.25rem 0 0 0', fontSize: '0.95rem', lineHeight: '1.5' }}>
          {analysis?.root_cause || analysis?.summary || 'AI analysis pending or unavailable.'}
        </p>
      </div>

      {/* Hotfix Action Box */}
      {remediation && (
        <div
          style={{
            borderTop: '1px solid #27272a',
            paddingTop: '1rem',
            marginTop: '1rem',
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
          }}
        >
          <div>
            <div style={{ fontWeight: 'bold', fontSize: '0.9rem' }}>
              Target Repo: <span style={{ color: '#38C2DE' }}>{remediation.target_repo || 'N/A'}</span>
            </div>
            <div style={{ fontSize: '0.8rem', color: '#a1a1aa', marginTop: '0.2rem' }}>
              Branch: {remediation.head_branch || 'feature/hotfix'} → {remediation.base_branch || 'main'}
            </div>
          </div>

          {remediation.status === 'executed' || remediation.pr_url ? (
            <a
              href={remediation.pr_url}
              target="_blank"
              rel="noopener noreferrer"
              style={{
                backgroundColor: '#15803d',
                color: '#fff',
                padding: '0.5rem 1rem',
                borderRadius: '6px',
                textDecoration: 'none',
                fontWeight: 'bold',
                fontSize: '0.85rem',
              }}
            >
              View Pull Request ↗
            </a>
          ) : (
            <button
              onClick={onApproveHotfix}
              disabled={isExecuting}
              style={{
                backgroundColor: '#38C2DE',
                color: '#09090b',
                border: 'none',
                padding: '0.6rem 1.2rem',
                borderRadius: '6px',
                fontWeight: 'bold',
                cursor: isExecuting ? 'not-allowed' : 'pointer',
                opacity: isExecuting ? 0.7 : 1,
              }}
            >
              {isExecuting ? 'Creating PR...' : 'Approve & Create Hotfix PR'}
            </button>
          )}
        </div>
      )}
    </div>
  );
};