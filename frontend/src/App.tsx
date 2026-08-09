import React, { useEffect, useState } from 'react';
import { SignedIn, SignedOut, SignInButton, UserButton } from '@clerk/clerk-react';
import { useAppUser } from './context/Clerk';

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || '/api/v1';

export default function App() {
  const { principal, getToken } = useAppUser();
  const [incidents, setIncidents] = useState<any[]>([]);
  const [loading, setLoading] = useState<boolean>(true);

  useEffect(() => {
    async function fetchIncidents() {
      try {
        // Fetch Clerk JWT or fallback to guest sandbox token
        const token = (await getToken()) || 'guest-sandbox-token';

        const response = await fetch(`${API_BASE_URL}/incidents`, {
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${token}`,
          },
        });

        if (response.ok) {
          const data = await response.json();
          setIncidents(data);
        }
      } catch (err) {
        console.error('Error fetching incidents:', err);
      } finally {
        setLoading(false);
      }
    }

    fetchIncidents();
  }, [getToken]);

  return (
    <div style={{ fontFamily: 'sans-serif', padding: '2rem', maxWidth: '800px', margin: '0 auto' }}>
      {/* Header & Auth Controls */}
      <header style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '2rem' }}>
        <h2>errAgent Dashboard</h2>
        <div>
          <SignedIn>
            <UserButton />
          </SignedIn>
          <SignedOut>
            <SignInButton mode="modal" />
          </SignedOut>
        </div>
      </header>

      {/* Session Info */}
      <section style={{ background: '#f4f4f5', padding: '1rem', borderRadius: '8px', marginBottom: '2rem' }}>
        <p style={{ margin: 0 }}>
          <strong>Active Session Principal:</strong> {principal || 'Guest Sandbox Mode'}
        </p>
      </section>

      {/* Incidents List Placeholder */}
      <section>
        <h3>Ingested Incidents</h3>
        {loading ? (
          <p>Loading incidents from backend...</p>
        ) : incidents.length === 0 ? (
          <p>No incidents found. Run `seed_db.py` on backend or trigger an error webhook!</p>
        ) : (
          <ul style={{ listStyle: 'none', padding: 0 }}>
            {incidents.map((inc) => (
              <li
                key={inc._id}
                style={{
                  border: '1px solid #e4e4e7',
                  padding: '1rem',
                  borderRadius: '6px',
                  marginBottom: '0.75rem',
                }}
              >
                <div style={{ fontWeight: 'bold' }}>
                  [{inc.status?.toUpperCase()}] {inc.service_name}
                </div>
                <div style={{ color: '#ef4444', fontSize: '0.9rem', marginTop: '0.25rem' }}>
                  {inc.error_message}
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}