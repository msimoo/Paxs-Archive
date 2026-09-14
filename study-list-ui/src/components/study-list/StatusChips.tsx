import { cn } from '@/lib/utils';
import type { StudyFilters, StudyCounts } from '@/types/study';

interface StatusChipsProps {
  filters: StudyFilters;
  counts: StudyCounts;
  setStatusFilter: (state: string) => void;
  toggleSpecialFilter: (key: keyof Pick<StudyFilters, 'critical' | 'reported' | 'oru' | 'his'>) => void;
}

export function StatusChips({ filters, counts, setStatusFilter, toggleSpecialFilter }: StatusChipsProps) {
  const chips = [
    { label: 'All', value: '', count: counts.total || 0, active: filters.state === '' },
    { label: 'Online', value: 'ONLINE', count: counts.online || 0, active: filters.state === 'ONLINE' },
    { label: 'Archived', value: 'ARCHIVED', count: counts.archived || 0, active: filters.state === 'ARCHIVED' },
    { label: '🚨 Critical', value: 'critical', count: counts.critical || 0, active: filters.critical, isSpecial: true },
    { label: '📄 Reported', value: 'reported', count: counts.reported || 0, active: filters.reported, isSpecial: true },
    { label: '➤ EMR', value: 'oru', count: counts.oru_sent || 0, active: filters.oru, isSpecial: true },
    { label: '🏥 HIS', value: 'his', count: counts.his || 0, active: filters.his, isSpecial: true },
  ];

  return (
    <div className="flex flex-wrap items-center gap-3">
      {chips.map(chip => (
        <button
          key={chip.label}
          onClick={() => {
            if (chip.isSpecial) {
              toggleSpecialFilter(chip.value as any);
            } else {
              setStatusFilter(chip.value);
            }
          }}
          className={cn(
            "group flex items-center gap-2 px-3 py-1.5 rounded-full border transition-all duration-300 backdrop-blur-sm",
            chip.active
              ? "bg-gradient-to-r from-cyan-950/80 to-[#0c1424] border-cyan-500/50 text-cyan-50 shadow-[0_0_15px_rgba(34,211,238,0.15)]"
              : "bg-[#0c1424]/60 border-slate-500/20 text-slate-400 hover:border-slate-500/40 hover:text-slate-300"
          )}
        >
          <span className="text-sm font-medium">{chip.label}</span>
          <span className={cn(
            "font-mono text-xs px-2 py-0.5 rounded-full",
            chip.active ? "bg-cyan-500/20 text-cyan-200" : "bg-[#111c30] text-slate-500 group-hover:bg-[#16233c]"
          )}>
            {chip.count}
          </span>
        </button>
      ))}
    </div>
  );
}
