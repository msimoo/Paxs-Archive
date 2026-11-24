export const STUDY_SORT_KEYS = [
  'date',
  'patient',
  'accession',
  'series',
  'instances',
  'modality',
  'created',
] as const;

export type StudySortKey = (typeof STUDY_SORT_KEYS)[number];
export type StudyLifecycleState = 'ONLINE' | 'ARCHIVED';
export type SpecialStudyFilter = keyof Pick<StudyFilters, 'critical' | 'reported' | 'oru' | 'his'>;

export interface Study {
  study_uid: string;
  patient_name: string | null;
  patient_id: string;
  patient_source: string;
  // C07: real demographics from the archive. Optional — render "Unknown"/"N/A"
  // when absent rather than substituting a default.
  patient_birth_date?: string | null;
  patient_sex?: string | null;
  tenant_id?: number;
  tenant_name?: string;
  study_date: string;
  study_time?: string;
  description: string;
  accession: string;
  modalities: string[];
  n_series: number;
  n_instances: number;
  created_at: string;
  lifecycle_state: StudyLifecycleState;
  thumbnail?: string;
  dictation_count?: number;
  report_id?: number;
  report_status?: 'DRAFT' | 'FINALIZED';
  oru_status?: 'PENDING' | 'SENT' | 'FAILED';
  // Reader holding a live reading claim (the viewer is open on this study).
  claimed_by?: string;
}

export interface StudyResponse {
  items: Study[];
  total: number;
  page: number;
  limit: number;
  sort: StudySortKey;
  order: 'asc' | 'desc';
  counts: StudyCounts;
}

export interface StudyCounts {
  total: number;
  online: number;
  archived: number;
  critical: number;
  reported: number;
  oru_sent: number;
  his: number;
}

export interface StudyFilters {
  page: number;
  limit: number;
  sort: StudySortKey;
  order: 'asc' | 'desc';
  search: string;
  modality: string;
  state: string;
  critical: boolean;
  reported: boolean;
  oru: boolean;
  his: boolean;
  from: string;
  to: string;
  tenant: string;
}

export type ModalityCode = 'CT' | 'MR' | 'CR' | 'DX' | 'US' | 'NM' | 'PT' | 'XA' | 'MG' | 'OT';

declare global {
  interface Window {
    PAXS_BASE_PATH?: string;
    PAXS_PUBLIC_BASE_URL?: string;
    paxsOpenReportModal?: (studyUid: string) => void;
    paxsMountDictationPlayer?: (element: HTMLElement, studyUid: string) => () => void;
  }
}

