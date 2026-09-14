import type { StudyResponse, StudyFilters } from '@/types/study';

const API_BASE = '/api/v1';

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
  const base = (window as any).PAXS_PUBLIC_BASE_URL || location.origin;
  return `${base.replace(/\/+$/, '')}/viewer/viewer?StudyInstanceUIDs=${encodeURIComponent(studyUid)}`;
}
