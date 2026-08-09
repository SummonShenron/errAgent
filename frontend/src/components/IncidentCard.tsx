import React from 'react';
import { Incident } from '../api';

interface IncidentCardProps {
  incident: Incident;
  onSelect: (id: string) => void;
  isSelected: boolean;
}

export const IncidentCard: React.FC<IncidentCardProps> = ({ incident, onSelect, isSelected }) => {
  const getStatusColor = (status: string) => {
    switch (status.toLowerCase()) {
      case 'open':
        return '#ef4444';
      case 'resolved':
        return '#22c55e';
      default:
        return '#eab308';
    }
  };

  return (
    <div
      onClick={() => onSelect(incident._id)}
      style={{
        border: isSelected ? '2px solid #38C2DE' : '1px solid #e4e4e7',
        padding: '1rem',
        borderRadius: '8px',
        marginBottom: '0.75rem',
        cursor: 'pointer',
        backgroundColor: isSelected ? '#f0fdf4' : '#ffffff',
        transition: 'all 0.2s ease',
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span style={{ fontWeight: 'bold', fontSize: '1rem', color: '#18181b' }}>
          {incident.service_name}
        </span>
        <span
          style={{
            color: getStatusColor(incident.status),
            fontSize: '0.75rem',
            fontWeight: 'bold',
            textTransform: 'uppercase',
            padding: '0.15rem 0.4rem',
            borderRadius: '4px',
            backgroundColor: '#f4f4f5',
          }}
        >
          {incident.status}
        </span>
      </div>

      <p style={{ color: '#dc2626', fontSize: '0.875rem', margin: '0.5rem 0 0.25rem 0', fontWeight: '500' }}>
        {incident.error_message}
      </p>

      <div style={{ fontSize: '0.75rem', color: '#71717a', marginTop: '0.5rem' }}>
        Env: <strong>{incident.environment}</strong> | ID: {incident._id}
      </div>
    </div>
  );
};