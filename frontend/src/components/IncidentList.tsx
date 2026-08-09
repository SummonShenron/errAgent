import React from 'react';
import { Incident } from '../api';
import { IncidentCard } from './IncidentCard';

interface IncidentListProps {
  incidents: Incident[];
  selectedId: string | null;
  onSelectIncident: (id: string) => void;
  loading: boolean;
}

export const IncidentList: React.FC<IncidentListProps> = ({
  incidents,
  selectedId,
  onSelectIncident,
  loading,
}) => {
  if (loading) {
    return <p style={{ color: '#71717a' }}>Loading incidents from backend...</p>;
  }

  if (incidents.length === 0) {
    return <p style={{ color: '#71717a' }}>No incidents found. Trigger a crash webhook to test!</p>;
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column' }}>
      {incidents.map((inc) => (
        <IncidentCard
          key={inc._id}
          incident={inc}
          isSelected={inc._id === selectedId}
          onSelect={onSelectIncident}
        />
      ))}
    </div>
  );
};