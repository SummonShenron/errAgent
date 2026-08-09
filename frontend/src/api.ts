// src/services/api.ts

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000/api/v1';

export interface Incident {
  _id: string;
  service_name: string;
  environment: string;
  error_message: string;
  stack_trace?: string;
  repository?: string;
  status: 'open' | 'investigating' | 'resolved';
  metadata?: Record<string, any>;
  created_at: string;
  updated_at: string;
}

export interface Analysis {
  incident_id?: string;
  root_cause?: string;
  summary?: string;
  severity?: string;
}

export interface Remediation {
  incident_id?: string;
  status?: string;
  target_repo?: string;
  pr_title?: string;
  pr_body?: string;
  head_branch?: string;
  base_branch?: string;
  pr_url?: string;
}

export interface IncidentDetailResponse {
  incident: Incident;
  analysis: Analysis;
  remediation: Remediation;
}

export interface HotfixResponse {
  status: string;
  message: string;
  pr_url?: string;
}

/**
 * Generic fetch wrapper to attach Clerk JWT token and handle errors
 */
async function fetchWithAuth<T>(
  endpoint: string,
  token: string | null,
  options: RequestInit = {}
): Promise<T> {
  const authToken = token || 'guest-sandbox-token';

  const response = await fetch(`${API_BASE_URL}${endpoint}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${authToken}`,
      ...options.headers,
    },
  });

  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(errorData.detail || `API Request failed with status ${response.status}`);
  }

  return response.json();
}

// --- API Methods Matching FastAPI Router ---

/** GET /api/v1/incidents */
export async function getIncidents(token: string | null): Promise<Incident[]> {
  return fetchWithAuth<Incident[]>('/incidents', token);
}

/** GET /api/v1/incidents/{incident_id} */
export async function getIncidentDetail(
  incidentId: string,
  token: string | null
): Promise<IncidentDetailResponse> {
  return fetchWithAuth<IncidentDetailResponse>(`/incidents/${incidentId}`, token);
}

/** POST /api/v1/incidents/{incident_id}/approve-hotfix */
export async function approveHotfix(
  incidentId: string,
  token: string | null
): Promise<HotfixResponse> {
  return fetchWithAuth<HotfixResponse>(`/incidents/${incidentId}/approve-hotfix`, token, {
    method: 'POST',
  });
}