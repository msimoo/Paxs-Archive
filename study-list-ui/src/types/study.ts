export interface Study {
  study_uid: string;
  patient_name: string;
  patient_id: string;
  patient_source: string;
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
  lifecycle_state: 'ONLINE' | 'ARCHIVED';
  thumbnail?: string;
  report_id?: number;
  report_status?: 'DRAFT' | 'FINALIZED';
  oru_status?: 'PENDING' | 'SENT' | 'FAILED';
}

export interface StudyResponse {
  items: Study[];
  total: number;
  page: number;
  limit: number;
  sort: string;
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
  sort: string;
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
