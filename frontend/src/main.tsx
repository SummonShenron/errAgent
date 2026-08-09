import React from 'react';
import ReactDOM from 'react-dom/client';
import { ClerkProvider } from '@clerk/clerk-react';
import { UserProvider } from './context/Clerk'; // Your reference context
import App from './App';
import './index.css';

// Read key from Vite env vars
const PUBLISHABLE_KEY = import.meta.env.VITE_CLERK_PUBLISHABLE_KEY;

if (!PUBLISHABLE_KEY) {
  const root = document.getElementById('root');
  if (root) {
    root.innerHTML =
      '<div style="font-family: sans-serif; padding: 24px; color: #991b1b; background: #fef2f2; border: 1px solid #fecaca; border-radius: 8px; margin: 24px;">Missing VITE_CLERK_PUBLISHABLE_KEY in environment variables.</div>';
  }
  throw new Error('Missing VITE_CLERK_PUBLISHABLE_KEY in environment variables.');
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ClerkProvider publishableKey={PUBLISHABLE_KEY}>
      <UserProvider>
        <App />
      </UserProvider>
    </ClerkProvider>
  </React.StrictMode>
);