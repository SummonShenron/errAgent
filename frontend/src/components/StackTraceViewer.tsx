import React from 'react';

interface StackTraceViewerProps {
  stackTrace?: string;
}

export const StackTraceViewer: React.FC<StackTraceViewerProps> = ({ stackTrace }) => {
  if (!stackTrace) {
    return <p style={{ color: '#71717a', fontSize: '0.875rem' }}>No stack trace available for this incident.</p>;
  }

  return (
    <div style={{ marginTop: '1rem' }}>
      <h4 style={{ margin: '0 0 0.5rem 0', fontSize: '0.9rem', color: '#3f3f46' }}>Stack Trace</h4>
      <pre
        style={{
          backgroundColor: '#1e1e24',
          color: '#f4f4f5',
          padding: '1rem',
          borderRadius: '6px',
          overflowX: 'auto',
          fontSize: '0.85rem',
          lineHeight: '1.4',
          fontFamily: 'monospace',
          maxHeight: '300px',
        }}
      >
        <code>{stackTrace}</code>
      </pre>
    </div>
  );
};