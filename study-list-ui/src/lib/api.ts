import type { Study, StudyCounts, StudyFilters, StudyLifecycleState, StudyResponse, StudySortKey } from '@/types/study';

const ANONYMOUS_PATIENT_NAME = 'Anonymous / Unnamed';
let patientNameDisplay: unknown;

/**
 * The sub-path the app is served under: "" at the domain root, "/paxs" behind a
 * reverse proxy mounted at /paxs/ (V2-3).
 *
 * Resolution order:
 *  1. `window.PAXS_BASE_PATH` — injected by the server. Authoritative, because
 *     the browser cannot infer the prefix when a *stripping* proxy removes it.
 *  2. the document path, cut at the /studies-ui mount.
 */
export function appBasePath(): string {
  const injected = window.PAXS_BASE_PATH;
  if (typeof injected === 'string' && injected) {
    return injected.replace(/\/+$/, '');
  }
  const marker = '/studies-ui';
  const idx = window.location.pathname.indexOf(marker);
  if (idx > 0) {
    return window.location.pathname.slice(0, idx).replace(/\/+$/, '');
  }
  return '';
}

/** Fully-qualified app base (origin + sub-path) for links that must be absolute. */
export function appBaseUrl(): string {
  const injected = window.PAXS_PUBLIC_BASE_URL;
  if (typeof injected === 'string' && injected) {
    return injected.replace(/\/+$/, '');
  }
  return window.location.origin + appBasePath();
}

export function getLoginUrl(): string {
  const current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  return `${appBasePath()}/login?next=${encodeURIComponent(current || '/')}`;
}

/** Small seam for host shells and tests; production always performs navigation. */
export const authNavigation = {
  assign(url: string): void {
    window.location.assign(url);
  },
};

let authRedirectStarted = false;

function expireAuthentication(): void {
  try {
    window.localStorage.removeItem('paxs_token');
  } catch {
    // Storage can be blocked by browser privacy settings; redirect regardless.
  }
  if (authRedirectStarted) return;
  authRedirectStarted = true;
  const loginPath = `${appBasePath()}/login`;
  if (window.location.pathname !== loginPath) {
    authNavigation.assign(getLoginUrl());
  }
}

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === 'AbortError';
}

function getStoredToken(): string | null {
  try {
    return window.localStorage.getItem('paxs_token');
  } catch {
    return null;
  }
}

const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS', 'TRACE']);
const CSRF_COOKIE_NAME = 'paxs_csrf';
const CSRF_HEADER_NAME = 'X-CSRF-Token';

/**
 * The signed double-submit token minted at login into a JS-readable cookie
 * (B-19 / FS-10). It is only consulted for a **cookie-authenticated** unsafe
 * request: the server ignores it when a bearer token is presented, and rejects
 * the request outright when neither is valid.
 *
 * Without this the app could browse (GET is exempt) but every mutation —
 * delete, share, forward — failed with `403 CSRF token missing or invalid`
 * whenever the access token was not in localStorage (a fresh tab carrying only
 * the HttpOnly session cookie, or a token cleared after a 401). The dashboard
 * SPA has always sent it; this client silently did not.
 */
function csrfToken(): string {
  const match = document.cookie.match(/(?:^|;\s*)paxs_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : '';
}

/** Shared request boundary: adds auth, translates network failures, and handles expired tokens. */
export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  const token = getStoredToken();
  if (token && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`);
  }
  const method = (init.method ?? 'GET').toUpperCase();
  if (!SAFE_METHODS.has(method) && !headers.has(CSRF_HEADER_NAME)) {
    const csrf = csrfToken();
    if (csrf) headers.set(CSRF_HEADER_NAME, csrf);
  }

  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      headers,
      credentials: init.credentials ?? 'same-origin',
    });
  } catch (error: unknown) {
    if (isAbortError(error)) throw error;
    throw new Error('Network error while contacting PAXS. Check the server connection and try again.');
  }

  if (response.status === 401) expireAuthentication();
  return response;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

async function readBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

function responseDetail(payload: unknown): string | null {
  if (typeof payload === 'string' && payload.trim()) return payload.trim();
  if (!isRecord(payload)) return null;
  const detail = payload.detail ?? payload.message;
  if (typeof detail === 'string' && detail.trim()) return detail.trim();
  if (Array.isArray(detail)) {
    const messages = detail.flatMap((item) => {
      if (!isRecord(item)) return [];
      return typeof item.msg === 'string' ? [item.msg] : [];
    });
    return messages.length ? messages.join('; ') : null;
  }
  return null;
}

async function throwHttpError(response: Response): Promise<never> {
  const detail = responseDetail(await readBody(response));
  if (response.status >= 500) {
    throw new ApiError(
      detail
        ? `Server error (${response.status}): ${detail}`
        : `Server error (${response.status}). Retry shortly or contact an administrator if it continues.`,
      response.status,
    );
  }
  throw new ApiError(detail ? `Request failed (${response.status}): ${detail}` : `Request failed (${response.status}).`, response.status);
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(path, init);
  if (!response.ok) return throwHttpError(response);
  const body = await readBody(response);
  if (body === null) throw new ApiError('The server returned an empty response.', response.status);
  if (typeof body === 'string') throw new ApiError('The server returned invalid JSON.', response.status);
  return body as T;
}

export interface DiscBurnStatus {
  phase: 'queued' | 'downloading' | 'preparing' | 'writing' | 'verifying' | 'verified' | 'failed';
  count: number;
  target_kind?: 'disc' | 'usb' | 'folder';
  drive?: string;
  output_path?: string | null;
  error: string | null;
}

export interface OpticalRecorder {
  drive: string;
  model: string;
}

export interface UsbDrive {
  drive: string;
  label: string;
  filesystem: string;
  free_bytes: number;
  total_bytes: number;
}

export interface ServerMediaDevices {
  platform: string;
  recorders: OpticalRecorder[];
  recorder_error: string | null;
  usb_drives: UsbDrive[];
  disc_burn_enabled: boolean;
  default_disc_drive: string;
}

export interface DiscMediaReadiness {
  drive: string;
  media_type: string;
  available_bytes: number;
  staging_free_bytes: number;
}

export function fetchServerDevices(): Promise<ServerMediaDevices> {
  return requestJson(`${appBasePath()}/api/v1/export/dicomdir/server/devices`);
}

export function checkServerDiscMedia(drive?: string): Promise<DiscMediaReadiness> {
  const qs = drive ? `?drive=${encodeURIComponent(drive)}` : '';
  return requestJson(`${appBasePath()}/api/v1/export/dicomdir/server/disc/media${qs}`);
}

export function startDiscBurn(id: string, studyUid: string, drive?: string): Promise<DiscBurnStatus> {
  return requestJson(`${appBasePath()}/api/v1/export/dicomdir/disc/${id}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ study_uid: studyUid, ...(drive ? { drive } : {}) }),
  });
}

export function startServerUsbExport(
  id: string,
  studyUid: string,
  drive: string,
  folderName?: string,
): Promise<DiscBurnStatus> {
  return requestJson(`${appBasePath()}/api/v1/export/dicomdir/usb/${id}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      study_uid: studyUid,
      drive,
      ...(folderName ? { folder_name: folderName } : {}),
    }),
  });
}

export function getDiscBurn(id: string, signal: AbortSignal): Promise<DiscBurnStatus> {
  return requestJson(`${appBasePath()}/api/v1/export/dicomdir/disc/${id}`, { signal });
}

export interface ExporterLauncherResponse {
  launched: boolean;
  method: 'local-exe' | 'protocol';
  session_id: string;
  protocol_uri: string;
}

export interface ExporterSessionStatus {
  session_id: string;
  study_uid: string;
  mode: 'media' | 'dicom-zip';
  phase: string;
  percent: number;
  message: string;
  target_label: string;
  output_path: string | null;
  files_done: number;
  files_total: number;
  bytes_written: number;
  speed_mbps: number;
  microdicom_verified: boolean;
  error: string | null;
}

export function openMediaExporterLauncher(
  studyUid: string,
  mode: 'media' | 'dicom-zip',
): Promise<ExporterLauncherResponse> {
  return requestJson(`${appBasePath()}/api/v1/export/launcher/open`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ study_uid: studyUid, mode }),
  });
}

export function getMediaExporterSession(
  sessionId: string,
  signal?: AbortSignal,
): Promise<ExporterSessionStatus> {
  return requestJson(
    `${appBasePath()}/api/v1/export/launcher/sessions/${encodeURIComponent(sessionId)}`,
    { signal },
  );
}

function stringValue(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function nullableString(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

function numberValue(value: unknown): number {
  const parsed = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
}

function normalizeDate(value: unknown): string {
  const raw = stringValue(value).trim();
  return /^[0-9]{8}$/.test(raw)
    ? `${raw.slice(0, 4)}-${raw.slice(4, 6)}-${raw.slice(6, 8)}`
    : raw;
}

function normalizeLifecycleState(value: unknown): StudyLifecycleState {
  return value === 'ARCHIVED' ? 'ARCHIVED' : 'ONLINE';
}

function isStudySortKey(value: unknown): value is StudySortKey {
  return typeof value === 'string' && ['date', 'patient', 'accession', 'series', 'instances', 'modality', 'created'].some((key) => key === value);
}

function normalizeSortKey(value: unknown): StudySortKey {
  return isStudySortKey(value) ? value : 'date';
}

function normalizeCounts(value: unknown): StudyCounts {
  const counts = isRecord(value) ? value : {};
  return {
    total: numberValue(counts.total),
    online: numberValue(counts.online),
    archived: numberValue(counts.archived),
    critical: numberValue(counts.critical),
    reported: numberValue(counts.reported),
    oru_sent: numberValue(counts.oru_sent),
    his: numberValue(counts.his),
  };
}

function normalizeStudy(value: unknown): Study {
  if (!isRecord(value) || typeof value.study_uid !== 'string' || !value.study_uid) {
    throw new ApiError('The study list response contains an invalid study record.', 200);
  }
  const modalities = Array.isArray(value.modalities)
    ? value.modalities.filter((item): item is string => typeof item === 'string')
    : [];
  const reportStatus = value.report_status === 'DRAFT' || value.report_status === 'FINALIZED'
    ? value.report_status
    : undefined;
  const oruStatus = value.oru_status === 'PENDING' || value.oru_status === 'SENT' || value.oru_status === 'FAILED'
    ? value.oru_status
    : undefined;

  return {
    study_uid: value.study_uid,
    patient_name: nullableString(value.patient_name),
    patient_id: stringValue(value.patient_id),
    patient_source: stringValue(value.patient_source),
    patient_birth_date: nullableString(value.patient_birth_date),
    patient_sex: nullableString(value.patient_sex),
    ...(typeof value.tenant_id === 'number' ? { tenant_id: value.tenant_id } : {}),
    ...(typeof value.tenant_name === 'string' ? { tenant_name: value.tenant_name } : {}),
    study_date: normalizeDate(value.study_date),
    ...(typeof value.study_time === 'string' ? { study_time: value.study_time } : {}),
    description: stringValue(value.description),
    accession: stringValue(value.accession),
    modalities,
    n_series: numberValue(value.n_series),
    n_instances: numberValue(value.n_instances),
    created_at: stringValue(value.created_at),
    lifecycle_state: normalizeLifecycleState(value.lifecycle_state),
    ...(typeof value.thumbnail === 'string' ? { thumbnail: value.thumbnail } : {}),
    ...(typeof value.dictation_count === 'number' ? { dictation_count: value.dictation_count } : {}),
    ...(typeof value.report_id === 'number' ? { report_id: value.report_id } : {}),
    ...(reportStatus ? { report_status: reportStatus } : {}),
    ...(oruStatus ? { oru_status: oruStatus } : {}),
    ...(typeof value.claimed_by === 'string' && value.claimed_by ? { claimed_by: value.claimed_by } : {}),
  };
}

function normalizeStudyResponse(value: unknown): StudyResponse {
  if (!isRecord(value) || !Array.isArray(value.items)) {
    throw new ApiError('The server returned an invalid study list response.', 200);
  }
  return {
    items: value.items.map(normalizeStudy),
    total: numberValue(value.total),
    page: Math.max(1, numberValue(value.page) || 1),
    limit: Math.max(1, numberValue(value.limit) || 20),
    sort: normalizeSortKey(value.sort),
    order: value.order === 'asc' ? 'asc' : 'desc',
    counts: normalizeCounts(value.counts),
  };
}

function apiUrl(path: string): string {
  return `${appBasePath()}/api/v1${path}`;
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

/**
 * Roles that are offered the delete control in this UI.
 *
 * The API's own gate (`DESTRUCTIVE_ROLES` in `app/core/roles.py`) also admits
 * RADIOLOGIST. Hiding the control from everyone but an ADMIN is the narrower
 * product decision: a non-admin is never shown a button whose only outcome is a
 * 403, which is exactly how the delete action used to "not work".
 */
export const DELETE_STUDY_ROLES: readonly string[] = ['ADMIN'];

/** True when ``role`` may be offered the delete control (fail closed). */
export function canDeleteStudies(role: string | null | undefined): boolean {
  return typeof role === 'string' && DELETE_STUDY_ROLES.includes(role.toUpperCase());
}

export async function fetchCurrentUser(signal?: AbortSignal): Promise<unknown> {
  const user = await requestJson<unknown>(apiUrl('/auth/me'), { signal });
  patientNameDisplay = isRecord(user) ? user.patient_name_display : undefined;
  return user;
}

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

  const data: unknown = await requestJson<unknown>(apiUrl(`/studies?${params}`), { signal });
  return normalizeStudyResponse(data);
}

export async function deleteStudy(studyUid: string): Promise<void> {
  const response = await apiFetch(apiUrl(`/studies/${encodeURIComponent(studyUid)}`), { method: 'DELETE' });
  if (!response.ok) return throwHttpError(response);
}

export type ViewerWorkflow = 'basic' | 'segmentation' | 'diagnostic';

/**
 * The OHIF `routeName` of each workflow, not its label: the viewer matches the
 * path segment, so `diagnostic` is the PAXS mode registered by
 * viewer-src/modes/paxs-diagnostic/src/composition.json.
 */
const WORKFLOW_ROUTES: Record<ViewerWorkflow, string> = {
  basic: 'viewer',
  segmentation: 'segmentation',
  diagnostic: 'diagnostic',
};

/** Resolve installed OHIF modes while retaining the existing basic-viewer link. */
export function getViewerUrl(studyUid: string, workflow: ViewerWorkflow = 'basic'): string {
  const route = WORKFLOW_ROUTES[workflow] ?? 'viewer';
  return `${appBaseUrl()}/viewer/${route}?StudyInstanceUIDs=${encodeURIComponent(studyUid)}`;
}

/** Create a desktop launch link; it carries no credential or session token. */
export function getDesktopViewerUri(studyUid: string): string {
  const query = new URLSearchParams({ server: appBaseUrl(), study: studyUid });
  return `paxsviewer://open?${query.toString()}`;
}

/** Launch an explicitly selected workflow; stays in-window inside PAXS-Archive.exe. */
export function openViewer(studyUid: string, workflow: ViewerWorkflow = 'basic'): void {
  const url = getViewerUrl(studyUid, workflow);
  if (typeof window !== 'undefined' && (window as { pywebview?: unknown }).pywebview) {
    window.location.assign(url);
    return;
  }
  const tab = window.open('about:blank', '_blank');
  if (tab) {
    tab.opener = null;
    tab.document.title = 'PAXS Viewer';
    const status = tab.document.createElement('p');
    status.textContent = 'Preparing the image viewer…';
    tab.document.body.appendChild(status);
  }
  if (tab && !tab.closed) tab.location.replace(url);
  else window.location.assign(url);
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
  return requestJson<StudyDetail>(apiUrl(`/studies/${encodeURIComponent(studyUid)}`), { signal });
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
  const response = await apiFetch(apiUrl(`/reports/study/${encodeURIComponent(studyUid)}`), { signal });
  if (response.status === 404) return null;
  if (!response.ok) return throwHttpError(response);
  const body: unknown = await readBody(response);
  if (!isRecord(body) || !isRecord(body.report)) return null;
  return body.report as unknown as StudyReport;
}

export interface DicomNode {
  ae_title: string;
  host: string;
  port: number;
  description: string | null;
  enabled: boolean;
  web_url: string | null;
}

export async function fetchNodes(signal?: AbortSignal): Promise<DicomNode[]> {
  return requestJson<DicomNode[]>(apiUrl('/nodes'), { signal });
}

export interface ForwardInstanceResult {
  sop_uid?: string;
  ok: boolean;
  status?: string;
  reason?: string;
}

/** The synchronous C-STORE endpoint reports completed per-instance results. */
export interface ForwardResult {
  ae_title: string;
  ok: boolean;
  sent: number;
  failed: number;
  reason?: string;
  transport?: string;
  results: ForwardInstanceResult[];
}

export async function forwardStudy(
  studyUid: string,
  aeTitle: string,
  signal?: AbortSignal,
): Promise<ForwardResult> {
  const response = await apiFetch(apiUrl(`/nodes/${encodeURIComponent(aeTitle)}/send`), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ study_uids: [studyUid] }),
    signal,
  });
  if (!response.ok) return throwHttpError(response);
  const body: unknown = await readBody(response);
  if (!isRecord(body)) throw new ApiError('The forwarding endpoint returned an invalid result.', response.status);
  const results = Array.isArray(body.results)
    ? body.results.flatMap((item): ForwardInstanceResult[] => isRecord(item) ? [{
      ...(typeof item.sop_uid === 'string' ? { sop_uid: item.sop_uid } : {}),
      ok: item.ok === true,
      ...(typeof item.status === 'string' ? { status: item.status } : {}),
      ...(typeof item.reason === 'string' ? { reason: item.reason } : {}),
    }] : [])
    : [];
  return {
    ae_title: stringValue(body.ae_title, aeTitle),
    ok: body.ok === true,
    sent: numberValue(body.sent),
    failed: numberValue(body.failed),
    ...(typeof body.reason === 'string' ? { reason: body.reason } : {}),
    ...(typeof body.transport === 'string' ? { transport: body.transport } : {}),
    results,
  };
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
  return requestJson<ShareResult>(apiUrl('/share'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ study_uid: studyUid, expiry_days: expiryDays ?? null }),
    signal,
  });
}

export function clearAuthToken(): void {
  try {
    window.localStorage.removeItem('paxs_token');
  } catch {
    // Storage can be blocked by browser privacy settings.
  }
}
