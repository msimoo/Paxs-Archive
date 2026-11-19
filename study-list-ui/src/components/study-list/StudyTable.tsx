import { displayPatientName } from '@/lib/api';
import { cn } from '@/lib/utils';
import type { Study, StudyFilters } from '@/types/study';
import { getModColor } from '@/lib/constants';
import { getViewerUrl } from '@/lib/api';
import { Disc3, Eye, Share2, Trash2, Forward, Activity, Info, FileText } from 'lucide-react';
import { Skeleton } from '@/components/ui/skeleton';
import { Badge } from '@/components/ui/badge';
import { Tooltip, TooltipContent, TooltipTrigger, TooltipProvider } from '@/components/ui/tooltip';

interface StudyTableProps {
  studies: Study[];
  loading: boolean;
  filters: StudyFilters;
  setSort: (sort: string, order: 'asc' | 'desc') => void;
  viewMode: 'table' | 'grid';
  onStudyClick: (study: Study) => void;
  onActionClick: (action: 'share' | 'forward' | 'delete' | 'burn', study: Study) => void;
}

function getInitials(pn: string) {
  if (!pn) return '??';
  if (pn.includes('^')) {
    const parts = pn.split('^');
    return `${parts[1]?.[0] || ''}${parts[0]?.[0] || ''}`.toUpperCase() || '??';
  }
  const parts = pn.split(' ');
  return `${parts[0]?.[0] || ''}${parts[1]?.[0] || ''}`.toUpperCase() || '??';
}

export function StudyTable({ studies, loading, filters, setSort, viewMode, onStudyClick, onActionClick }: StudyTableProps) {
  
  const handleSort = (key: string) => {
    if (filters.sort === key) {
      setSort(key, filters.order === 'asc' ? 'desc' : 'asc');
    } else {
      setSort(key, 'desc');
    }
  };

  const SortHeader = ({ label, sortKey }: { label: string, sortKey: string }) => {
    const isActive = filters.sort === sortKey;
    return (
      <button 
        onClick={() => handleSort(sortKey)}
        className={cn(
          "flex items-center gap-1 text-xs uppercase tracking-wider font-mono hover:text-cyan-400 transition-colors",
          isActive ? "text-cyan-400 font-semibold" : "text-slate-500"
        )}
      >
        {label}
        {isActive && <span>{filters.order === 'asc' ? '▲' : '▼'}</span>}
      </button>
    );
  };

  if (viewMode === 'grid') {
    return (
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
        {loading ? (
           Array.from({ length: 8 }).map((_, i) => (
             <div key={i} className="bg-[#0c1424]/40 border border-slate-700/50 rounded-xl p-4 flex flex-col gap-4">
               <div className="flex items-center gap-3">
                 <Skeleton className="h-12 w-12 rounded-xl bg-slate-800" />
                 <div className="space-y-2 flex-1"><Skeleton className="h-4 w-3/4 bg-slate-800"/><Skeleton className="h-3 w-1/2 bg-slate-800"/></div>
               </div>
               <Skeleton className="h-20 w-full bg-slate-800 rounded-md" />
               <div className="flex justify-between items-center mt-auto">
                 <Skeleton className="h-6 w-16 bg-slate-800 rounded-md" />
                 <div className="flex gap-2"><Skeleton className="h-8 w-8 rounded-full bg-slate-800"/></div>
               </div>
             </div>
           ))
        ) : studies.length === 0 ? (
          <div className="col-span-full flex flex-col items-center justify-center py-24 text-slate-500">
            <Activity className="h-16 w-16 mb-4 text-slate-700" />
            <h3 className="text-lg font-medium text-slate-300">No studies found</h3>
          </div>
        ) : (
          studies.map(study => {
            const primaryMod = study.modalities[0] || 'OT';
            const modColor = getModColor(primaryMod);
            
            return (
              <div 
                key={study.study_uid}
                className="group relative bg-[#0c1424]/40 backdrop-blur-xl border border-slate-700/50 rounded-xl p-4 flex flex-col gap-3 hover:bg-[#111c30]/80 transition-all duration-200 cursor-pointer hover:-translate-y-[2px] hover:shadow-[0_8px_24px_rgba(3,7,24,0.6)] hover:border-slate-600 focus-within:ring-2 focus-within:ring-cyan-500/50"
                onClick={() => onStudyClick(study)}
              >
                <div 
                  className="absolute top-0 left-0 right-0 h-1 rounded-t-xl opacity-50 group-hover:opacity-100 transition-opacity" 
                  style={{ backgroundColor: modColor }}
                />
                <div className="flex items-start gap-3">
                  <div className="relative h-12 w-12 shrink-0 rounded-xl overflow-hidden bg-[#070c18] border border-slate-700/50 flex items-center justify-center">
                    {study.thumbnail ? (
                      <img src={study.thumbnail} alt="" className="h-full w-full object-cover" />
                    ) : (
                      <span className="text-sm font-bold" style={{ color: modColor }}>{getInitials(study.patient_name)}</span>
                    )}
                  </div>
                  <div className="flex flex-col min-w-0">
                    <span className="text-sm font-semibold text-slate-200 truncate">{displayPatientName(study.patient_name)}</span>
                    <span className="text-xs font-mono text-slate-500 mt-0.5">{study.patient_id}</span>
                  </div>
                  <Badge variant="secondary" className="ml-auto text-[9px] h-5" style={{ backgroundColor: modColor, color: '#070c18' }}>
                    {primaryMod}
                  </Badge>
                </div>
                
                <div className="text-xs text-slate-400 mt-1 line-clamp-2 min-h-[32px]">
                  {study.description || 'No Description'}
                </div>
                
                <div className="grid grid-cols-2 gap-2 text-xs text-slate-500 mt-2 bg-[#070c18]/50 p-2 rounded-lg border border-slate-800/50">
                  <div>
                    <div className="font-mono">{study.study_date}</div>
                  </div>
                  <div className="text-right font-mono">
                    {study.n_series}s × {study.n_instances}i
                  </div>
                </div>

                <div className="flex items-center justify-between mt-auto pt-2 border-t border-slate-800/50">
                   <div className="flex gap-1" onClick={e => e.stopPropagation()}>
                    <TooltipProvider>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <button className="p-1.5 rounded-full text-emerald-400 hover:bg-emerald-950/30 transition-colors" onClick={() => (window as any).paxsOpenReportModal?.(study.study_uid)} title="كتابة/تعديل التقرير">
                            <FileText className="w-4 h-4" />
                          </button>
                        </TooltipTrigger>
                        <TooltipContent>كتابة/تعديل التقرير (Diagnostic Report)</TooltipContent>
                      </Tooltip>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <button className="p-1.5 rounded-full text-slate-400 hover:text-cyan-400 hover:bg-cyan-950/30 transition-colors" onClick={() => window.open(getViewerUrl(study.study_uid), '_blank')}>
                            <Eye className="w-4 h-4" />
                          </button>
                        </TooltipTrigger>
                        <TooltipContent>Open Viewer</TooltipContent>
                      </Tooltip>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <button className="p-1.5 rounded-full text-slate-400 hover:text-indigo-400 hover:bg-indigo-950/30 transition-colors" onClick={() => onActionClick('share', study)}>
                            <Share2 className="w-4 h-4" />
                          </button>
                        </TooltipTrigger>
                        <TooltipContent>Share</TooltipContent>
                      </Tooltip>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <button type="button" aria-label="Burn study to CD/DVD" className="p-1.5 rounded-full text-slate-400 hover:text-cyan-400 hover:bg-cyan-950/30 transition-colors" onClick={() => onActionClick('burn', study)}>
                            <Disc3 className="w-4 h-4" />
                          </button>
                        </TooltipTrigger>
                        <TooltipContent>Burn study to CD/DVD</TooltipContent>
                      </Tooltip>
                    </TooltipProvider>
                   </div>
                   <button 
                     className="text-[10px] uppercase font-bold tracking-wider text-cyan-400 hover:text-cyan-300 flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity"
                     onClick={(e) => { e.stopPropagation(); onStudyClick(study); }}
                   >
                     <Info className="w-3 h-3" /> Inspect
                   </button>
                </div>
              </div>
            );
          })
        )}
      </div>
    )
  }

  return (
    <div className="w-full flex flex-col rounded-xl overflow-hidden border border-slate-700/50 bg-[#0c1424]/40 backdrop-blur-xl shadow-[0_10px_32px_rgba(3,7,24,0.45)]">
      {/* Header Row */}
      <div className="grid grid-cols-[64px_2fr_3fr_150px_120px_100px_215px] gap-4 px-6 py-4 bg-[#070c18]/80 border-b border-slate-800">
        <div></div>
        <SortHeader label="Patient" sortKey="patient_name" />
        <SortHeader label="Study" sortKey="date" />
        <SortHeader label="Accession" sortKey="accession" />
        <div className="text-xs uppercase tracking-wider font-mono text-slate-500">Series</div>
        <div className="text-xs uppercase tracking-wider font-mono text-slate-500">Mod</div>
        <div className="text-xs uppercase tracking-wider font-mono text-slate-500 text-center">Actions</div>
      </div>

      {/* Body */}
      <div className="flex flex-col">
        {loading ? (
          Array.from({ length: 8 }).map((_, i) => (
            <div key={i} className="grid grid-cols-[64px_2fr_3fr_150px_120px_100px_215px] gap-4 px-6 py-4 border-b border-slate-800/50 items-center">
              <Skeleton className="h-12 w-12 rounded-xl bg-slate-800" />
              <div className="space-y-2"><Skeleton className="h-4 w-32 bg-slate-800"/><Skeleton className="h-3 w-24 bg-slate-800"/></div>
              <div className="space-y-2"><Skeleton className="h-4 w-48 bg-slate-800"/><Skeleton className="h-3 w-32 bg-slate-800"/></div>
              <Skeleton className="h-6 w-24 bg-slate-800 rounded-full" />
              <Skeleton className="h-6 w-20 bg-slate-800 rounded-full" />
              <Skeleton className="h-6 w-16 bg-slate-800 rounded-md" />
              <div className="flex gap-2"><Skeleton className="h-8 w-8 rounded-full bg-slate-800"/><Skeleton className="h-8 w-8 rounded-full bg-slate-800"/></div>
            </div>
          ))
        ) : studies.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-24 text-slate-500">
            <Activity className="h-16 w-16 mb-4 text-slate-700" />
            <h3 className="text-lg font-medium text-slate-300">No studies found</h3>
            <p className="text-sm mt-1">Try adjusting your search or filters</p>
          </div>
        ) : (
          studies.map(study => {
            const primaryMod = study.modalities[0] || 'OT';
            const modColor = getModColor(primaryMod);
            
            return (
              <div 
                key={study.study_uid} 
                className="group relative grid grid-cols-[64px_2fr_3fr_150px_120px_100px_215px] gap-4 px-6 py-4 border-b border-slate-800/30 items-center bg-[#0c1424]/20 hover:bg-[#111c30]/80 transition-all duration-200 cursor-pointer hover:-translate-y-[1px] hover:shadow-lg hover:border-slate-700/80 focus-within:ring-1 focus-within:ring-cyan-500/50"
                onClick={() => onStudyClick(study)}
              >
                <div 
                  className="absolute left-0 top-0 bottom-0 w-1 opacity-50 group-hover:opacity-100 transition-opacity" 
                  style={{ backgroundColor: modColor }}
                />
                
                {/* Thumbnail */}
                <div className="relative h-12 w-12 rounded-xl overflow-hidden bg-[#070c18] border border-slate-700/50 flex items-center justify-center">
                  {study.thumbnail ? (
                    <img src={study.thumbnail} alt="" className="h-full w-full object-cover" />
                  ) : (
                    <span className="text-sm font-bold" style={{ color: modColor }}>{getInitials(study.patient_name)}</span>
                  )}
                  <div className="absolute inset-0 bg-cyan-900/40 opacity-0 group-hover:opacity-100 flex items-center justify-center transition-opacity backdrop-blur-[1px]">
                    <span className="text-[10px] font-bold text-cyan-50">VIEW</span>
                  </div>
                  <div className="absolute bottom-0 left-0 px-1 py-0.5 text-[9px] font-mono font-bold text-[#070c18]" style={{ backgroundColor: modColor }}>
                    {primaryMod}
                  </div>
                </div>

                {/* Patient */}
                <div className="flex flex-col min-w-0 pr-4">
                  <span className="text-sm font-semibold text-slate-200 truncate">{displayPatientName(study.patient_name)}</span>
                  <div className="flex items-center gap-2 mt-1">
                    <span className="text-xs font-mono text-slate-500">{study.patient_id}</span>
                    {study.patient_source && (
                      <Badge variant="outline" className="text-[9px] h-4 px-1 py-0 border-teal-700 text-teal-400 bg-teal-950/30">HIS</Badge>
                    )}
                  </div>
                </div>

                {/* Study */}
                <div className="flex flex-col min-w-0 pr-4">
                  <span className="text-sm text-slate-300 truncate">{study.description || 'No Description'}</span>
                  <div className="flex items-center gap-2 mt-1">
                    <span className="text-xs text-slate-500">{study.study_date} {study.study_time}</span>
                    <Badge variant="secondary" className={cn(
                      "text-[9px] h-4 px-1.5 py-0",
                      study.lifecycle_state === 'ONLINE' ? "bg-emerald-950/40 text-emerald-400 hover:bg-emerald-950/40" : "bg-amber-950/40 text-amber-400 hover:bg-amber-950/40"
                    )}>
                      {study.lifecycle_state}
                    </Badge>
                    {study.report_status && (
                      <Badge variant="outline" className="text-[9px] h-4 px-1.5 py-0 border-cyan-800 text-cyan-400">
                        {study.report_status}
                      </Badge>
                    )}
                  </div>
                </div>

                {/* Accession */}
                <div className="flex items-center">
                  <span className="text-xs font-mono bg-[#070c18] px-2 py-1 rounded text-slate-400 border border-slate-800">{study.accession}</span>
                </div>

                {/* Series/Images */}
                <div className="flex items-center">
                  <span className="text-xs font-mono bg-[#16233c] px-2 py-1 rounded-full text-slate-300 border border-slate-700/50">
                    {study.n_series}s × {study.n_instances}i
                  </span>
                </div>

                {/* Modalities */}
                <div className="flex items-center gap-1 flex-wrap">
                  {study.modalities.map(mod => (
                    <span key={mod} className="text-[10px] font-mono px-1.5 py-0.5 rounded text-[#070c18] font-bold" style={{ backgroundColor: getModColor(mod) }}>
                      {mod}
                    </span>
                  ))}
                </div>

                {/* Actions */}
                <div className="flex items-center gap-1" onClick={e => e.stopPropagation()}>
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <button className="p-2 rounded-full text-emerald-400 hover:bg-emerald-950/30 transition-colors" onClick={() => (window as any).paxsOpenReportModal?.(study.study_uid)} title="كتابة/تعديل التقرير">
                          <FileText className="w-4 h-4" />
                        </button>
                      </TooltipTrigger>
                      <TooltipContent>كتابة/تعديل التقرير (Diagnostic Report)</TooltipContent>
                    </Tooltip>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <button className="p-2 rounded-full text-slate-400 hover:text-cyan-400 hover:bg-cyan-950/30 transition-colors" onClick={() => onStudyClick(study)}>
                          <Info className="w-4 h-4" />
                        </button>
                      </TooltipTrigger>
                      <TooltipContent>Quick Inspect</TooltipContent>
                    </Tooltip>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <button className="p-2 rounded-full text-slate-400 hover:text-teal-400 hover:bg-teal-950/30 transition-colors" onClick={() => onActionClick('forward', study)}>
                          <Forward className="w-4 h-4" />
                        </button>
                      </TooltipTrigger>
                      <TooltipContent>Forward to Node</TooltipContent>
                    </Tooltip>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <button className="p-2 rounded-full text-slate-400 hover:text-indigo-400 hover:bg-indigo-950/30 transition-colors" onClick={() => onActionClick('share', study)}>
                          <Share2 className="w-4 h-4" />
                        </button>
                      </TooltipTrigger>
                      <TooltipContent>Share Study</TooltipContent>
                    </Tooltip>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <button type="button" aria-label="Burn study to CD/DVD" className="p-1.5 rounded-full text-slate-400 hover:text-cyan-400 hover:bg-cyan-950/30 transition-colors" onClick={() => onActionClick('burn', study)}>
                          <Disc3 className="w-4 h-4" />
                        </button>
                      </TooltipTrigger>
                      <TooltipContent>Burn study to CD/DVD</TooltipContent>
                    </Tooltip>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <button className="p-2 rounded-full text-slate-400 hover:text-rose-400 hover:bg-rose-950/30 transition-colors" onClick={() => onActionClick('delete', study)}>
                          <Trash2 className="w-4 h-4" />
                        </button>
                      </TooltipTrigger>
                      <TooltipContent>Delete Study</TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
