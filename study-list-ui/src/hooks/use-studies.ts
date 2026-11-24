import { useState, useEffect, useCallback, useRef } from 'react';
import { fetchStudies } from '@/lib/api';
import type { Study, StudyFilters, StudyCounts, StudySortKey } from '@/types/study';

const defaultFilters: StudyFilters = {
  page: 1,
  limit: 20,
  sort: 'date',
  order: 'desc',
  search: '',
  modality: '',
  state: '',
  critical: false,
  reported: false,
  oru: false,
  his: false,
  from: '',
  to: '',
  tenant: ''
};

export function useStudies(initialFilters?: Partial<StudyFilters>) {
  const [filters, setFiltersState] = useState<StudyFilters>({ ...defaultFilters, ...initialFilters });
  const [studies, setStudies] = useState<Study[]>([]);
  const [total, setTotal] = useState(0);
  const [counts, setCounts] = useState<StudyCounts>({ total: 0, online: 0, archived: 0, critical: 0, reported: 0, oru_sent: 0, his: 0 });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const abortControllerRef = useRef<AbortController | null>(null);
  const requestIdRef = useRef(0);

  const updateFilters = useCallback((newFilters: Partial<StudyFilters>) => {
    setFiltersState(prev => ({ ...prev, ...newFilters, page: newFilters.page || (newFilters.page === undefined ? 1 : prev.page) }));
  }, []);

  const setSearch = (search: string) => updateFilters({ search });
  const setModality = (modality: string) => updateFilters({ modality });
  const setSort = (sort: StudySortKey, order: 'asc' | 'desc') => updateFilters({ sort, order });
  const setPage = (page: number) => updateFilters({ page });
  const setStatusFilter = (state: string) => updateFilters({ state });
  const toggleSpecialFilter = (key: keyof Pick<StudyFilters, 'critical' | 'reported' | 'oru' | 'his'>) => {
    setFiltersState(prev => ({ ...prev, [key]: !prev[key], page: 1 }));
  };
  const setDateRange = (from: string, to: string) => updateFilters({ from, to });

  const loadData = useCallback(async () => {
    abortControllerRef.current?.abort();
    const controller = new AbortController();
    abortControllerRef.current = controller;
    const requestId = ++requestIdRef.current;

    setLoading(true);
    setError(null);
    try {
      const data = await fetchStudies(filters, controller.signal);
      if (requestId !== requestIdRef.current) return;
      setStudies(data.items);
      setTotal(data.total);
      setCounts(data.counts);
    } catch (error: unknown) {
      if (requestId !== requestIdRef.current || (error instanceof Error && error.name === 'AbortError')) return;
      setError(error instanceof Error ? error.message : 'Unable to load studies. Please retry.');
    } finally {
      if (requestId === requestIdRef.current) setLoading(false);
    }
  }, [filters]);

  const removeStudy = useCallback((studyUid: string) => {
    if (!studies.some((study) => study.study_uid === studyUid)) return;
    abortControllerRef.current?.abort();
    requestIdRef.current += 1;
    const removed = studies.find((study) => study.study_uid === studyUid);
    setStudies((current) => current.filter((study) => study.study_uid !== studyUid));
    setTotal((current) => Math.max(0, current - 1));
    if (removed) {
      setCounts((current) => ({
        ...current,
        total: Math.max(0, current.total - 1),
        online: Math.max(0, current.online - (removed.lifecycle_state === 'ONLINE' ? 1 : 0)),
        archived: Math.max(0, current.archived - (removed.lifecycle_state === 'ARCHIVED' ? 1 : 0)),
        reported: Math.max(0, current.reported - (removed.report_status === 'FINALIZED' ? 1 : 0)),
        oru_sent: Math.max(0, current.oru_sent - (removed.oru_status === 'SENT' ? 1 : 0)),
        his: Math.max(0, current.his - (removed.patient_source === 'his' ? 1 : 0)),
      }));
    }
    if (studies.length === 1 && filters.page > 1) {
      setFiltersState((current) => ({ ...current, page: current.page - 1 }));
    }
  }, [filters.page, studies]);

  useEffect(() => {
    const timer = setTimeout(() => {
      void loadData();
    }, 300); // debounce
    return () => {
      clearTimeout(timer);
      abortControllerRef.current?.abort();
    };
  }, [filters, loadData]);

  return {
    studies,
    total,
    counts,
    loading,
    error,
    filters,
    setSearch,
    setModality,
    setSort,
    setPage,
    setStatusFilter,
    toggleSpecialFilter,
    setDateRange,
    removeStudy,
    refresh: loadData
  };
}
