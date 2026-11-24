import type { ReactNode } from 'react';
import { cn } from '@/lib/utils';
import type { StudyCounts } from '@/types/study';
import { IconArchived, IconCritical, IconOnline, IconStudies } from '@/components/icons/PaxsIcons';

interface VitalsStripProps {
  counts: StudyCounts;
  className?: string;
}

/**
 * The archive's vitals — one line, in the top bar.
 *
 * This used to be a four-card dashboard that pushed the study list ~200px down
 * the page. Every one of those cards repeated a number the filter chips already
 * show, so the panel was paying a screenful of height for no new information.
 * The facts worth keeping — how big the archive is, how much of it is hot, and
 * whether anything is flagged STAT — now live in the header as three compact
 * readings. Nothing is invented: every value is a count the API returned.
 */
export function VitalsStrip({ counts, className }: VitalsStripProps) {
  const onlinePct = counts.total ? Math.round((counts.online / counts.total) * 100) : 0;

  return (
    <div
      role="group"
      aria-label="Archive vitals"
      className={cn(
        'flex min-w-0 items-center gap-1 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden',
        className,
      )}
    >
      <Reading
        icon={<IconStudies className="size-3.5" />}
        value={counts.total.toLocaleString()}
        label="indexed"
        title={`${counts.total.toLocaleString()} studies indexed`}
      />
      <Reading
        icon={<IconOnline className="size-3.5" />}
        value={counts.online.toLocaleString()}
        label={`online · ${onlinePct}%`}
        title={`${counts.online.toLocaleString()} of ${counts.total.toLocaleString()} studies online (${onlinePct}%)`}
      />
      <Reading
        icon={<IconArchived className="size-3.5" />}
        value={counts.archived.toLocaleString()}
        label="archived"
        title={`${counts.archived.toLocaleString()} studies in the cold tier`}
        className="hidden sm:inline-flex"
      />
      {counts.critical > 0 && (
        <Reading
          icon={<IconCritical className="size-3.5" />}
          value={counts.critical.toLocaleString()}
          label="stat"
          title={`${counts.critical} critical unread — needs a radiologist now`}
          className="border-danger/45 bg-danger/10 text-danger"
          valueClassName="text-danger"
          pulse
        />
      )}
    </div>
  );
}

function Reading({
  icon,
  value,
  label,
  title,
  className,
  valueClassName,
  pulse = false,
}: {
  icon: ReactNode;
  value: string;
  label: string;
  title: string;
  className?: string;
  valueClassName?: string;
  pulse?: boolean;
}) {
  return (
    <span
      title={title}
      className={cn(
        'inline-flex shrink-0 items-center gap-1.5 rounded-full border border-hairline bg-panel/55 py-[3px] pl-1 pr-2',
        'transition-colors duration-200 hover:border-hairline-strong hover:bg-raised/70',
        className,
      )}
    >
      <span className="grid size-[19px] place-items-center rounded-full bg-ink/60 ring-1 ring-inset ring-white/[0.06]">
        {icon}
      </span>
      <span className={cn('font-mono text-[11.5px] font-bold leading-none tabular-nums text-foreground', valueClassName)}>
        {value}
      </span>
      <span className="whitespace-nowrap text-[9px] font-semibold uppercase leading-none tracking-[0.13em] text-faint">
        {label}
      </span>
      {pulse && (
        <span className="relative flex size-1.5" aria-hidden="true">
          <span className="absolute inline-flex size-full animate-ping rounded-full bg-danger opacity-70" />
          <span className="relative inline-flex size-1.5 rounded-full bg-danger" />
        </span>
      )}
    </span>
  );
}
