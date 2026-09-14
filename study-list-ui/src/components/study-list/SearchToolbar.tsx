import { Search } from 'lucide-react';
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

export function SearchToolbar({ filters, setSearch, setModality, setDateRange }: SearchToolbarProps) {
  return (
    <div className="flex flex-col md:flex-row items-center gap-4 w-full">
      <div className="relative w-full md:flex-1 group">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-5 w-5 text-slate-500 group-focus-within:text-cyan-400 transition-colors" />
        <Input 
          value={filters.search}
          onChange={e => setSearch(e.target.value)}
          placeholder="Search by Patient Name, ID, or Accession..." 
          className="w-full pl-10 pr-4 py-6 bg-[#0c1424]/80 border-slate-700/50 backdrop-blur-md focus-visible:ring-1 focus-visible:ring-cyan-500 focus-visible:border-cyan-500 text-slate-200 placeholder:text-slate-600 rounded-xl transition-all shadow-inner"
        />
      </div>
      
      <div className="flex flex-row gap-3 w-full md:w-auto">
        <Select value={filters.modality} onValueChange={setModality}>
          <SelectTrigger className="w-[180px] bg-[#0c1424]/80 border-slate-700/50 backdrop-blur-md text-slate-300 h-12 focus:ring-cyan-500">
            <SelectValue placeholder="All Modalities" />
          </SelectTrigger>
          <SelectContent className="bg-[#0c1424] border-slate-700 text-slate-300">
            <SelectItem value="">All Modalities</SelectItem>
            {Object.entries(MOD_LABELS).map(([key, label]) => (
              <SelectItem key={key} value={key}>{label} ({key})</SelectItem>
            ))}
          </SelectContent>
        </Select>

        <div className="flex items-center gap-2 bg-[#0c1424]/80 border border-slate-700/50 rounded-md px-3 backdrop-blur-md h-12">
          <input 
            type="date" 
            value={filters.from}
            onChange={e => setDateRange(e.target.value, filters.to)}
            className="bg-transparent text-slate-300 text-sm focus:outline-none focus:text-cyan-400 [color-scheme:dark]" 
          />
          <span className="text-slate-600">-</span>
          <input 
            type="date" 
            value={filters.to}
            onChange={e => setDateRange(filters.from, e.target.value)}
            className="bg-transparent text-slate-300 text-sm focus:outline-none focus:text-cyan-400 [color-scheme:dark]" 
          />
        </div>
      </div>
    </div>
  );
}
