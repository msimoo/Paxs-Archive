import { useState, useEffect, useCallback, useRef } from 'react';
import { fetchStudies } from '@/lib/api';
import type { Study, StudyFilters, StudyCounts } from '@/types/study';

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

  const updateFilters = useCallback((newFilters: Partial<StudyFilters>) => {
    setFiltersState(prev => ({ ...prev, ...newFilters, page: newFilters.page || (newFilters.page === undefined ? 1 : prev.page) }));
  }, []);

  const setSearch = (search: string) => updateFilters({ search });
  const setModality = (modality: string) => updateFilters({ modality });
  const setSort = (sort: string, order: 'asc' | 'desc') => updateFilters({ sort, order });
  const setPage = (page: number) => updateFilters({ page });
  const setStatusFilter = (state: string) => updateFilters({ state });
  const toggleSpecialFilter = (key: keyof Pick<StudyFilters, 'critical' | 'reported' | 'oru' | 'his'>) => {
    setFiltersState(prev => ({ ...prev, [key]: !prev[key], page: 1 }));
  };
  const setDateRange = (from: string, to: string) => updateFilters({ from, to });

  const loadData = useCallback(async () => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }
    abortControllerRef.current = new AbortController();

    setLoading(true);
    setError(null);
    try {
      const data = await fetchStudies(filters, abortControllerRef.current.signal);
      setStudies(data.items);
      setTotal(data.total);
      if (data.counts) setCounts(data.counts);
    } catch (err: any) {
      if (err.name !== 'AbortError') {
        setError(err.message || 'Failed to fetch studies');
      }
    } finally {
      setLoading(false);
    }
  }, [filters]);

  useEffect(() => {
    const timer = setTimeout(() => {
      loadData();
    }, 300); // debounce
    return () => clearTimeout(timer);
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
    refresh: loadData
  };
}
