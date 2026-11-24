import { ChevronLeft, ChevronRight } from 'lucide-react';
import { cn } from '@/lib/utils';

interface PaginationProps {
  page: number;
  limit: number;
  total: number;
  setPage: (p: number) => void;
}

export function Pagination({ page, limit, total, setPage }: PaginationProps) {
  const totalPages = Math.max(1, Math.ceil(total / limit));
  const isFirst = page === 1;
  const isLast = page >= totalPages;
  const firstRow = total === 0 ? 0 : (page - 1) * limit + 1;
  const lastRow = Math.min(page * limit, total);

  const stepButton = (dir: 'prev' | 'next') => {
    const disabled = dir === 'prev' ? isFirst : isLast;
    return (
      <button
        type="button"
        aria-label={dir === 'prev' ? 'Previous page' : 'Next page'}
        disabled={disabled}
        onClick={() => setPage(dir === 'prev' ? page - 1 : page + 1)}
        className={cn(
          'focus-ring grid h-8 w-8 place-items-center rounded-lg border transition-all duration-150',
          disabled
            ? 'cursor-not-allowed border-hairline bg-raised/30 text-faint/50'
            : 'border-hairline-strong bg-panel/70 text-muted hover:border-accent/50 hover:text-accent hover:shadow-[0_0_14px_-4px_var(--paxs-accent)]',
        )}
      >
        {dir === 'prev' ? <ChevronLeft className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
      </button>
    );
  };

  return (
    <div className="flex shrink-0 flex-col items-center justify-between gap-2 px-1 py-0.5 sm:flex-row">
      <p className="font-mono text-[10.5px] text-faint">
        <span className="text-foreground/85">{firstRow.toLocaleString()}–{lastRow.toLocaleString()}</span>
        <span className="mx-1.5 text-hairline-strong">/</span>
        <span className="text-foreground/85">{total.toLocaleString()}</span> studies
        <span className="mx-2 text-hairline-strong">·</span>
        page <span className="text-foreground/85">{page}</span> of{' '}
        <span className="text-foreground/85">{totalPages}</span>
      </p>
      <div className="flex items-center gap-1.5">
        {stepButton('prev')}
        {stepButton('next')}
      </div>
    </div>
  );
}
