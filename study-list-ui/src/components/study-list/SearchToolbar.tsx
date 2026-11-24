import { useEffect, useRef } from 'react';
import { X } from 'lucide-react';
import { IconModality, IconSearch } from '@/components/icons/PaxsIcons';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import type { StudyFilters } from '@/types/study';
import { MOD_LABELS } from '@/lib/constants';

interface SearchToolbarProps {
  filters: StudyFilters;
  setSearch: (s: string) => void;
  setModality: (m: string) => void;
  setDateRange: (from: string, to: string) => void;
}

/**
 * The command bar.
 *
 * Two decisions worth naming:
 *  · ⌘K / Ctrl-K focuses the search field. The hint is printed in the field, so
 *    the affordance is discoverable instead of folklore — and the listener is
 *    removed on unmount, so two mounted toolbars cannot fight over the key.
 *  · The date inputs inherit `color-scheme` from the document, so the native
 *    picker is dark in the reading room and light in clinical mode. The old
 *    `[color-scheme:dark]` hard-code made the light theme ship a black calendar.
 */
export function SearchToolbar({ filters, setSearch, setModality, setDateRange }: SearchToolbarProps) {
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        inputRef.current?.focus();
        inputRef.current?.select();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const hasSearch = filters.search.trim().length > 0;

  return (
    <div className="flex flex-col gap-1.5 lg:flex-row lg:items-center">
      <div className="group relative w-full lg:flex-1">
        <IconSearch className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2" />
        <Input
          ref={inputRef}
          value={filters.search}
          onChange={(e) => setSearch(e.target.value)}
          aria-label="Search studies"
          placeholder="Patient name, ID, or accession…"
          className="h-9 rounded-lg pl-[38px] pr-9 text-[13.5px] shadow-[inset_0_1px_0_var(--paxs-sheen)] sm:pr-16"
        />
        <div className="absolute right-2.5 top-1/2 flex -translate-y-1/2 items-center gap-1.5">
          {hasSearch ? (
            <button
              type="button"
              onClick={() => {
                setSearch('');
                inputRef.current?.focus();
              }}
              aria-label="Clear search"
              className="focus-ring rounded-md p-1 text-faint transition-colors hover:bg-raised-2 hover:text-foreground"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          ) : (
            <kbd className="hidden rounded border border-hairline-strong bg-raised-2 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-faint sm:block">
              ⌘K
            </kbd>
          )}
        </div>
      </div>

      <div className="flex flex-col gap-1.5 sm:flex-row">
        <Select
          value={filters.modality || 'all'}
          onValueChange={(v) => setModality(v === 'all' ? '' : v)}
        >
          <SelectTrigger
            aria-label="Filter by modality"
            className="h-9 w-full rounded-lg sm:w-[172px] text-[13px]"
          >
            <span className="flex items-center gap-2 truncate">
              <IconModality className="size-3.5 shrink-0" />
              <SelectValue placeholder="All modalities" />
            </span>
          </SelectTrigger>
          <SelectContent>
            {/* H17: Radix reserves "" for the placeholder, so the sentinel is "all". */}
            <SelectItem value="all">All modalities</SelectItem>
            {Object.entries(MOD_LABELS).map(([key, label]) => (
              <SelectItem key={key} value={key}>
                <span className="font-mono text-[11px] text-accent">{key}</span>
                <span className="ml-2">{label}</span>
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <div className="flex h-9 items-center justify-between gap-1.5 rounded-lg border border-hairline bg-raised/50 px-3 backdrop-blur-sm transition-colors focus-within:border-accent/50 focus-within:ring-2 focus-within:ring-accent">
          <label className="flex items-center gap-1.5">
            <span className="font-mono text-[9.5px] uppercase tracking-wider text-faint">from</span>
            <input
              type="date"
              value={filters.from}
              onChange={(e) => setDateRange(e.target.value, filters.to)}
              aria-label="Study date from"
              className="w-[102px] bg-transparent text-[12.5px] text-foreground focus:outline-none"
            />
          </label>
          <span aria-hidden="true" className="h-4 w-px bg-hairline-strong" />
          <label className="flex items-center gap-1.5">
            <span className="font-mono text-[9.5px] uppercase tracking-wider text-faint">to</span>
            <input
              type="date"
              value={filters.to}
              onChange={(e) => setDateRange(filters.from, e.target.value)}
              aria-label="Study date to"
              className="w-[102px] bg-transparent text-[12.5px] text-foreground focus:outline-none"
            />
          </label>
        </div>
      </div>
    </div>
  );
}
