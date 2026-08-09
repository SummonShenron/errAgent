import React, { useEffect, useState } from 'react';
import { SignedIn, SignedOut, SignInButton, UserButton } from '@clerk/clerk-react';
import { useAppUser } from './context/Clerk';
import { getIncidents, Incident } from './api';
import { IncidentList } from './components/IncidentList';
import { IncidentDetail } from './components/IncidentDetail';

export default function App() {
  const { principal, getToken } = useAppUser();
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(true);

  useEffect(() => {
    async function init() {
      try {
        const userToken = await getToken();
        setToken(userToken);
        const data = await getIncidents(userToken);
        setIncidents(data);
        if (data.length > 0) {
          setSelectedId(data[0]._id); // Pre-select first incident
        }
      } catch (err) {
        console.error('Failed to load incidents:', err);
      } finally {
        setLoading(false);
      }
    }

    init();
  }, [getToken]);

  return (
    <div style={{ fontFamily: 'sans-serif', minHeight: '100vh', backgroundColor: '#fafafa' }}>
      {/* Top Header */}
      <header
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          padding: '1rem 2rem',
          backgroundColor: '#18181b',
          color: '#ffffff',
          borderBottom: '1px solid #27272a',
        }}
      >
        <h2 style={{ margin: 0, fontSize: '1.25rem', color: '#38C2DE' }}>⚡ errAgent Dashboard</h2>
        <div>
          <SignedIn>
            <UserButton />
          </SignedIn>
          <SignedOut>
            <SignInButton mode="modal" />
          </SignedOut>
        </div>
      </header>

      {/* Main Container */}
      <div style={{ padding: '2rem', maxWidth: '1200px', margin: '0 auto' }}>
        {/* Session Status Banner */}
        <section
          style={{
            backgroundColor: '#ffffff',
            border: '1px solid #e4e4e7',
            padding: '0.75rem 1.25rem',
            borderRadius: '8px',
            marginBottom: '1.5rem',
            fontSize: '0.9rem',
          }}
        >
          <strong>Active Session:</strong> {principal || 'Guest Sandbox Mode'}
        </section>

        {/* 2-Column Split View */}
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1.5fr', gap: '2rem' }}>
          {/* Left Column: Incidents List */}
          <div>
            <h3 style={{ marginTop: 0, marginBottom: '1rem' }}>Ingested Incidents</h3>
            <IncidentList
              incidents={incidents}
              selectedId={selectedId}
              onSelectIncident={(id) => setSelectedId(id)}
              loading={loading}
            />
          </div>

          {/* Right Column: Incident Detail & Hotfix Action */}
          <div style={{ backgroundColor: '#ffffff', borderRadius: '8px', border: '1px solid #e4e4e7' }}>
            <IncidentDetail incidentId={selectedId} token={token} />
          </div>
        </div>
      </div>
    </div>
  );
}