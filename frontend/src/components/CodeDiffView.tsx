export const CodeDiffView = ({ patch }: { patch?: string }) => {
  if (!patch) return <p style={{ color: '#888' }}>No code patch available.</p>;

  const lines = patch.split('\n');

  return (
    <pre style={{
      backgroundColor: '#0d1117',
      color: '#c9d1d9',
      padding: '1rem',
      borderRadius: '6px',
      fontSize: '0.875rem',
      fontFamily: 'monospace',
      overflowX: 'auto',
      lineHeight: '1.45',
      border: '1px solid #30363d'
    }}>
      {lines.map((line, idx) => {
        let color = '#c9d1d9'; // Default text color
        let bg = 'transparent';

        if (line.startsWith('+') && !line.startsWith('+++')) {
          color = '#3fb950'; // Green text
          bg = 'rgba(46, 160, 67, 0.15)'; // Green diff highlight
        } else if (line.startsWith('-') && !line.startsWith('---')) {
          color = '#f85149'; // Red text
          bg = 'rgba(218, 54, 51, 0.15)'; // Red diff highlight
        } else if (line.startsWith('@@')) {
          color = '#d2a8ff'; // Purple hunk header
        }

        return (
          <div key={idx} style={{ backgroundColor: bg, color, padding: '0 4px' }}>
            {line}
          </div>
        );
      })}
    </pre>
  );
};