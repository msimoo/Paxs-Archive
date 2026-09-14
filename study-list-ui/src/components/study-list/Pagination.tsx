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

  return (
    <div className="flex items-center justify-between px-2 py-4">
      <div className="text-sm text-slate-500 font-medium">
        Page <span className="text-slate-300 font-mono">{page}</span> of <span className="text-slate-300 font-mono">{totalPages}</span> 
        <span className="mx-2 text-slate-700">·</span> 
        <span className="text-cyan-400 font-mono">{total}</span> studies
      </div>
      <div className="flex items-center gap-2">
        <button
          onClick={() => setPage(page - 1)}
          disabled={isFirst}
          className={cn(
            "flex items-center justify-center p-2 rounded-full backdrop-blur-md border transition-all",
            isFirst 
              ? "bg-[#0c1424]/40 border-slate-800 text-slate-700 cursor-not-allowed" 
              : "bg-[#0c1424] border-slate-700 text-slate-300 hover:border-cyan-500/50 hover:text-cyan-400 hover:shadow-[0_0_10px_rgba(34,211,238,0.2)]"
          )}
        >
          <ChevronLeft className="w-4 h-4" />
        </button>
        <button
          onClick={() => setPage(page + 1)}
          disabled={isLast}
          className={cn(
            "flex items-center justify-center p-2 rounded-full backdrop-blur-md border transition-all",
            isLast 
              ? "bg-[#0c1424]/40 border-slate-800 text-slate-700 cursor-not-allowed" 
              : "bg-[#0c1424] border-slate-700 text-slate-300 hover:border-cyan-500/50 hover:text-cyan-400 hover:shadow-[0_0_10px_rgba(34,211,238,0.2)]"
          )}
        >
          <ChevronRight className="w-4 h-4" />
        </button>
      </div>
    </div>
  );
}
