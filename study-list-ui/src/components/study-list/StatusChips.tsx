import type { ReactElement } from 'react';
import { cn } from '@/lib/utils';
import type { SpecialStudyFilter, StudyFilters, StudyCounts } from '@/types/study';
import {
  IconArchived,
  IconCritical,
  IconHospital,
  IconOnline,
  IconReport,
  IconSend,
  IconStudies,
  type IconTone,
} from '@/components/icons/PaxsIcons';

interface StatusChipsProps {
  filters: StudyFilters;
  counts: StudyCounts;
  setStatusFilter: (state: string) => void;
  toggleSpecialFilter: (key: SpecialStudyFilter) => void;
}

type ChipIcon = (props: { className?: string; tone?: IconTone }) => ReactElement;

interface FilterChip {
  label: string;
  value: string;
  count: number;
  active: boolean;
  kind: 'state' | 'special';
  icon: ChipIcon;
  tone: IconTone;
}

/**
 * One rail, two groups: lifecycle on the left (mutually exclusive), clinical
 * flags on the right (independent toggles). The split is visible — a hairline
 * and a gap — because mixing an exclusive filter with additive ones in one row
 * of identical pills is how a user ends up filtering by "Online + Archived" and
 * seeing nothing.
 *
 * Counts are always rendered: a filter that would return zero should say so
 * before it is clicked.
 *
 * The glyphs carry their own gradient paint, so the chip must NOT re-colour its
 * `<svg>` on hover/active the way the old monochrome icons were recoloured —
 * that would flatten every icon to one flat tint and lose the duotone read.
 */
export function StatusChips({ filters, counts, setStatusFilter, toggleSpecialFilter }: StatusChipsProps) {
  const lifecycle: FilterChip[] = [
    { label: 'All', value: '', count: counts.total, active: filters.state === '', kind: 'state', icon: IconStudies, tone: 'aqua' },
    { label: 'Online', value: 'ONLINE', count: counts.online, active: filters.state === 'ONLINE', kind: 'state', icon: IconOnline, tone: 'emerald' },
    { label: 'Archived', value: 'ARCHIVED', count: counts.archived, active: filters.state === 'ARCHIVED', kind: 'state', icon: IconArchived, tone: 'amber' },
  ];

  const specials: FilterChip[] = [
    { label: 'Critical', value: 'critical', count: counts.critical, active: filters.critical, kind: 'special', icon: IconCritical, tone: 'rose' },
    { label: 'Reported', value: 'reported', count: counts.reported, active: filters.reported, kind: 'special', icon: IconReport, tone: 'violet' },
    { label: 'EMR', value: 'oru', count: counts.oru_sent, active: filters.oru, kind: 'special', icon: IconSend, tone: 'sky' },
    { label: 'HIS', value: 'his', count: counts.his, active: filters.his, kind: 'special', icon: IconHospital, tone: 'violet' },
  ];

  const tones: Record<IconTone, string> = {
    aqua: 'data-[on=true]:border-accent/45 data-[on=true]:bg-accent/10 data-[on=true]:text-accent',
    emerald: 'data-[on=true]:border-ok/45 data-[on=true]:bg-ok/10 data-[on=true]:text-ok',
    amber: 'data-[on=true]:border-warn/45 data-[on=true]:bg-warn/10 data-[on=true]:text-warn',
    rose: 'data-[on=true]:border-danger/45 data-[on=true]:bg-danger/10 data-[on=true]:text-danger',
    violet: 'data-[on=true]:border-[#a78bfa]/50 data-[on=true]:bg-[#a78bfa]/10 data-[on=true]:text-[#a78bfa]',
    sky: 'data-[on=true]:border-teal/45 data-[on=true]:bg-teal/10 data-[on=true]:text-teal',
  };

  const render = (chip: FilterChip) => {
    const Icon = chip.icon;
    return (
      <button
        key={`${chip.kind}-${chip.label}`}
        type="button"
        aria-pressed={chip.active}
        data-on={chip.active}
        onClick={() => {
          if (chip.kind === 'special') toggleSpecialFilter(chip.value as SpecialStudyFilter);
          else setStatusFilter(chip.value as string);
        }}
        className={cn(
          'focus-ring group inline-flex items-center gap-1.5 rounded-full border border-transparent px-2 py-[3px]',
          'text-[11.5px] font-medium text-muted transition-all duration-200',
          'hover:border-hairline-strong hover:bg-raised/60 hover:text-foreground',
          tones[chip.tone],
        )}
      >
        <Icon className="size-3.5 opacity-80 transition-opacity group-hover:opacity-100" />
        <span>{chip.label}</span>
        <span
          className={cn(
            'min-w-[19px] rounded-full px-1.5 py-px text-center font-mono text-[10px] font-semibold tabular-nums',
            'bg-raised-2 text-muted transition-colors',
            chip.active && 'bg-[color-mix(in_srgb,currentColor_16%,transparent)] text-current',
          )}
        >
          {chip.count.toLocaleString()}
        </span>
      </button>
    );
  };

  return (
    <div
      role="group"
      aria-label="Filter studies"
      className="glass-panel flex flex-wrap items-center gap-0.5 self-start rounded-full px-1 py-0.5"
    >
      {lifecycle.map(render)}
      <span aria-hidden="true" className="mx-1 hidden h-4 w-px bg-hairline-strong sm:block" />
      {specials.map(render)}
    </div>
  );
}
