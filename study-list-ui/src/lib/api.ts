import type { StudyResponse, StudyFilters } from '@/types/study';

/**
 * The sub-path the app is served under: "" at the domain root, "/paxs" behind a
 * reverse proxy mounted at /paxs/ (V2-3).
 *
 * Resolution order:
 *  1. `window.PAXS_BASE_PATH` — injected by the server. Authoritative, because
 *     the browser cannot infer the prefix when a *stripping* proxy
 *     (`proxy_pass http://app/;`) removes it before forwarding.
 *  2. the document path, cut at the /studies-ui mount — correct for the common
 *     case where the proxy does NOT strip the prefix.
 *
 * Deriving from `location.origin` alone was the bug: it drops any path prefix,
 * so the viewer link pointed at https://host/viewer/... instead of
 * https://host/paxs/viewer/...
 */
export function appBasePath(): string {
  const injected = (window as any).PAXS_BASE_PATH;
  if (typeof injected === 'string' && injected) {
    return injected.replace(/\/+$/, '');
  }
  const marker = '/studies-ui';
  const idx = location.pathname.indexOf(marker);
  if (idx > 0) {
    return location.pathname.slice(0, idx).replace(/\/+$/, '');
  }
  return '';
}

/** Fully-qualified app base (origin + sub-path) for links that must be absolute. */
export function appBaseUrl(): string {
  const injected = (window as any).PAXS_PUBLIC_BASE_URL;
  if (typeof injected === 'string' && injected) {
    return injected.replace(/\/+$/, '');
  }
  return location.origin + appBasePath();
}

const API_BASE = `${appBasePath()}/api/v1`;

export async function fetchStudies(filters: StudyFilters, signal?: AbortSignal): Promise<StudyResponse> {
  const params = new URLSearchParams();
  params.set('page', String(filters.page));
  params.set('limit', String(filters.limit));
  if (filters.sort) params.set('sort', filters.sort);
  if (filters.order) params.set('order', filters.order);
  if (filters.state) params.set('state', filters.state);
  if (filters.critical) params.set('critical', '1');
  if (filters.reported) params.set('reported', '1');
  if (filters.oru) params.set('oru', '1');
  if (filters.his) params.set('his', '1');
  if (filters.search) params.set('search', filters.search);
  if (filters.modality) params.set('modality', filters.modality);
  if (filters.tenant) params.set('tenant', filters.tenant);
  if (filters.from) params.set('from', filters.from.replace(/-/g, ''));
  if (filters.to) params.set('to', filters.to.replace(/-/g, ''));

  const token = localStorage.getItem('paxs_token');
  const headers: Record<string, string> = {};
  if (token) headers['Authorization'] = `Bearer ${token}`;

  const res = await fetch(`${API_BASE}/studies?${params}`, { headers, signal });
  if (!res.ok) throw new Error(`Failed to load studies (${res.status})`);
  return res.json();
}

export async function deleteStudy(studyUid: string): Promise<void> {
  const token = localStorage.getItem('paxs_token');
  const headers: Record<string, string> = {};
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const res = await fetch(`${API_BASE}/studies/${encodeURIComponent(studyUid)}`, { method: 'DELETE', headers });
  if (!res.ok) throw new Error(`Failed to delete study (${res.status})`);
}

export function getViewerUrl(studyUid: string): string {
  return `${appBaseUrl()}/viewer/viewer?StudyInstanceUIDs=${encodeURIComponent(studyUid)}`;
}

export interface StudySeries {
  series_uid: string;
  modality: string | null;
  series_number: number | null;
  description: string | null;
  body_part: string | null;
  instances: number;
  thumbnail: string | null;
}

export interface StudyDetail {
  study_uid: string;
  patient: {
    name: string | null;
    id: string;
    birth_date: string | null;
    sex: string | null;
    phone: string | null;
    source: string;
  };
  study: {
    date: string | null;
    time: string | null;
    description: string | null;
    accession: string | null;
    referring_physician: string | null;
  };
  series: StudySeries[];
}

/** Real series + demographics for the detail drawer (C07). */
export async function fetchStudyDetail(studyUid: string, signal?: AbortSignal): Promise<StudyDetail> {
  const token = localStorage.getItem('paxs_token');
  const headers: Record<string, string> = {};
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const res = await fetch(`${API_BASE}/studies/${encodeURIComponent(studyUid)}`, { headers, signal });
  if (!res.ok) throw new Error(`Failed to load study detail (${res.status})`);
  return res.json();
}

export interface StudyReport {
  id: number;
  status: string;
  clinical_history: string | null;
  findings: string | null;
  impression: string | null;
  radiologist: string | null;
}

/** The study's report, or null when none exists yet. */
export async function fetchStudyReport(studyUid: string, signal?: AbortSignal): Promise<StudyReport | null> {
  const token = localStorage.getItem('paxs_token');
  const headers: Record<string, string> = {};
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const res = await fetch(`${API_BASE}/reports/study/${encodeURIComponent(studyUid)}`, { headers, signal });
  if (!res.ok) return null;
  const body = await res.json();
  return body?.report ?? null;
}

// --------------------------------------------------------------------------- #
// H01: real actions for the study-list modals.
//
// These replace the simulated success paths (setTimeout + optimistic toast) and
// the fabricated share URL/QR. Every result here comes from the server.
// --------------------------------------------------------------------------- #
export interface DicomNode {
  ae_title: string;
  host: string;
  port: number;
  description: string | null;
  enabled: boolean;
  web_url: string | null;
}

export async function fetchNodes(signal?: AbortSignal): Promise<DicomNode[]> {
  const token = localStorage.getItem('paxs_token');
  const headers: Record<string, string> = {};
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const res = await fetch(`${API_BASE}/nodes`, { headers, signal });
  if (!res.ok) throw new Error(`Failed to load DICOM nodes (${res.status})`);
  return res.json();
}

export interface ForwardResult {
  /** What the server actually reports, verbatim — never assume "delivered". */
  [key: string]: unknown;
}

export async function forwardStudy(
  studyUid: string,
  aeTitle: string,
  signal?: AbortSignal,
): Promise<ForwardResult> {
  const token = localStorage.getItem('paxs_token');
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const res = await fetch(`${API_BASE}/nodes/${encodeURIComponent(aeTitle)}/send`, {
    method: 'POST',
    headers,
    body: JSON.stringify({ study_uids: [studyUid] }),
    signal,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = (body as { detail?: string }).detail;
    throw new Error(detail || `Forward failed (${res.status})`);
  }
  return body as ForwardResult;
}

export interface ShareResult {
  token: string;
  url: string;
  study_uid: string;
  expires_at: string;
  qr_base64: string;
}

/** Real patient-share creation; the URL and QR both come from the server. */
export async function createShare(
  studyUid: string,
  expiryDays?: number,
  signal?: AbortSignal,
): Promise<ShareResult> {
  const token = localStorage.getItem('paxs_token');
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const res = await fetch(`${API_BASE}/share`, {
    method: 'POST',
    headers,
    body: JSON.stringify({ study_uid: studyUid, expiry_days: expiryDays ?? null }),
    signal,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = (body as { detail?: string }).detail;
    throw new Error(detail || `Could not create the share link (${res.status})`);
  }
  return body as ShareResult;
}

const ANONYMOUS_PATIENT_NAME = 'Anonymous / Unnamed';
let patientNameDisplay: unknown;

export function setPatientNameDisplay(mode: unknown): void {
  patientNameDisplay = mode;
}

export function displayPatientName(value: string | null | undefined): string {
  const stored = value?.trim();
  if (!stored) return ANONYMOUS_PATIENT_NAME;
  if (patientNameDisplay === 'source') return stored;
  if (patientNameDisplay === 'remove_carets') {
    return stored.replace(/\^/g, '').trim() || ANONYMOUS_PATIENT_NAME;
  }
  if (patientNameDisplay === 'structured') {
    const group = stored.split('=').find((part) => part.replace(/\^/g, '').trim());
    if (!group) return ANONYMOUS_PATIENT_NAME;
    return group.split('^').map((part) => part.trim()).filter(Boolean).join(', ');
  }
  const formatted = value?.replace(/\^/g, ' ').trim();
  return formatted || ANONYMOUS_PATIENT_NAME;
}

export interface DiscBurnStatus {
  phase: 'queued' | 'preparing' | 'writing' | 'verifying' | 'verified' | 'failed';
  count: number;
  error: string | null;
}

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function requestDisc(id: string, init: RequestInit): Promise<DiscBurnStatus> {
  const headers = new Headers(init.headers);
  const token = localStorage.getItem('paxs_token');
  if (token) headers.set('Authorization', `Bearer ${token}`);
  const response = await fetch(`${API_BASE}/export/dicomdir/disc/${id}`, {
    ...init, headers, credentials: 'same-origin',
  });
  const data = await response.json().catch(() => null);
  if (!response.ok || !data) {
    const detail = data?.detail;
    throw new ApiError(typeof detail === 'string' ? detail : `Disc request failed (${response.status}).`, response.status);
  }
  return data as DiscBurnStatus;
}

export function startDiscBurn(id: string, studyUid: string): Promise<DiscBurnStatus> {
  return requestDisc(id, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ study_uid: studyUid }),
  });
}

export function getDiscBurn(id: string, signal: AbortSignal): Promise<DiscBurnStatus> {
  return requestDisc(id, { signal });
}
