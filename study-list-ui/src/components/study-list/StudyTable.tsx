import { cn } from '@/lib/utils';
import type { ReactNode } from 'react';
import type { Study, StudyFilters, StudySortKey } from '@/types/study';
import { getModColor } from '@/lib/constants';
import { displayPatientName, getDesktopViewerUri } from '@/lib/api';
import { Download, Disc3, Share2, Trash2, Forward, Info, FileText, ChevronUp, ChevronDown, MonitorPlay } from 'lucide-react';
import { IconStudies } from '@/components/icons/PaxsIcons';
import { LaunchWorkflow } from './LaunchWorkflow';
import { DictationAction } from './DictationAction';
import { Skeleton } from '@/components/ui/skeleton';
import { Badge } from '@/components/ui/badge';
import { Tooltip, TooltipContent, TooltipTrigger, TooltipProvider } from '@/components/ui/tooltip';

interface StudyTableProps {
  studies: Study[];
  loading: boolean;
  filters: StudyFilters;
  setSort: (sort: StudySortKey, order: 'asc' | 'desc') => void;
  viewMode: 'table' | 'grid';
  onStudyClick: (study: Study) => void;
  onActionClick: (action: 'share' | 'forward' | 'delete' | 'burn' | 'export-zip', study: Study) => void;
  /** Delete is administrator-only; the control is not rendered otherwise. */
  canDelete?: boolean;
}

/** The one grid both the header and every row share, so columns stay aligned. */
const ROW_GRID =
  'grid-cols-[48px_minmax(150px,1.2fr)_minmax(230px,2.6fr)_114px_148px_84px_276px]';

function getInitials(pn: string | null) {
  if (!pn?.trim()) return '??';
  if (pn.includes('^')) {
    const parts = pn.split('^');
    return `${parts[1]?.[0] || ''}${parts[0]?.[0] || ''}`.toUpperCase() || '??';
  }
  const parts = pn.split(' ');
  return `${parts[0]?.[0] || ''}${parts[1]?.[0] || ''}`.toUpperCase() || '??';
}

/** An icon action on a row. One shape for five verbs, so the row stays calm. */
function RowAction({
  label,
  hint,
  tone,
  onClick,
  href,
  children,
}: {
  label: string;
  hint: string;
  tone: string;
  onClick?: () => void;
  href?: string;
  children: ReactNode;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        {href ? (
          <a
            href={href}
            aria-label={label}
            title={hint}
            className={cn(
              'focus-ring grid h-8 w-8 place-items-center rounded-lg text-muted transition-all duration-150',
              'hover:bg-raised-2 hover:text-foreground',
              tone,
            )}
          >
            {children}
          </a>
        ) : (
          <button
            type="button"
            aria-label={label}
            onClick={onClick}
            className={cn(
              'focus-ring grid h-8 w-8 place-items-center rounded-lg text-muted transition-all duration-150',
              'hover:bg-raised-2 hover:text-foreground',
              tone,
            )}
          >
            {children}
          </button>
        )}
      </TooltipTrigger>
      <TooltipContent>{hint}</TooltipContent>
    </Tooltip>
  );
}

function StudyMediaButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      aria-label="Export study to CD/DVD or USB"
      title="Export study to CD/DVD or USB"
      className="focus-ring grid size-7 shrink-0 place-items-center rounded text-accent hover:bg-raised-2"
      onClick={(event) => {
        event.stopPropagation();
        onClick();
      }}
    >
      <Disc3 className="size-4" aria-hidden="true" />
    </button>
  );
}

function StudyZipButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      aria-label="Export study (DICOM/ZIP)"
      title="Export study (DICOM/ZIP) to chosen directory"
      className="focus-ring grid size-7 shrink-0 place-items-center rounded text-accent hover:bg-raised-2"
      onClick={(event) => {
        event.stopPropagation();
        onClick();
      }}
    >
      <Download className="size-4" aria-hidden="true" />
    </button>
  );
}

export function StudyTable({ studies, loading, filters, setSort, viewMode, onStudyClick, onActionClick, canDelete = false }: StudyTableProps) {

  const handleSort = (key: StudySortKey) => {
    if (filters.sort === key) {
      setSort(key, filters.order === 'asc' ? 'desc' : 'asc');
    } else {
      setSort(key, 'desc');
    }
  };

  const SortHeader = ({ label, sortKey, className }: { label: string; sortKey: StudySortKey; className?: string }) => {
    const isActive = filters.sort === sortKey;
    return (
      <button
        type="button"
        onClick={() => handleSort(sortKey)}
        aria-label={label}
        className={cn(
          'focus-ring group inline-flex items-center gap-1 rounded font-mono text-[10px] font-semibold uppercase tracking-[0.12em] transition-colors',
          isActive ? 'text-accent' : 'text-faint hover:text-foreground',
          className,
        )}
      >
        {label}
        {isActive
          ? (filters.order === 'asc'
            ? <ChevronUp className="h-3 w-3" aria-hidden="true" />
            : <ChevronDown className="h-3 w-3" aria-hidden="true" />)
          : <ChevronDown className="h-3 w-3 opacity-0 transition-opacity group-hover:opacity-40" aria-hidden="true" />}
      </button>
    );
  };

  if (viewMode === 'grid') {
    return (
      <div className="min-h-0 flex-1 overflow-auto pr-0.5">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {loading ? (
          Array.from({ length: 8 }).map((_, i) => (
            <div key={i} className="rounded-card border border-hairline bg-panel/50 p-4">
              <div className="flex items-center gap-3">
                <Skeleton className="h-12 w-12 rounded-xl" />
                <div className="flex-1 space-y-2"><Skeleton className="h-3.5 w-3/4" /><Skeleton className="h-3 w-1/2" /></div>
              </div>
              <Skeleton className="mt-4 h-16 w-full" />
            </div>
          ))
        ) : studies.length === 0 ? (
          <div className="col-span-full flex flex-col items-center justify-center gap-2 py-20 text-center">
            <IconStudies className="size-10 opacity-70" />
            <h3 className="text-base font-semibold text-foreground">No studies found</h3>
            <p className="text-sm text-muted">Try a broader date range, or clear a filter.</p>
          </div>
        ) : (
          studies.map((study) => {
            const primaryMod = study.modalities[0] || 'OT';
            const modColor = getModColor(primaryMod);
            return (
              <div
                key={study.study_uid}
                className="group relative cursor-pointer overflow-hidden rounded-card border border-hairline bg-panel/60 p-4 transition-all duration-200 hover:-translate-y-0.5 hover:border-hairline-strong hover:bg-panel hover:shadow-[var(--paxs-shadow-soft)]"
                onClick={() => onStudyClick(study)}
              >
                <span aria-hidden="true" className="mod-rail" style={{ backgroundColor: modColor }} />
                <div className="flex items-start gap-3">
                  <div className="thumb-well scan-sweep relative grid h-12 w-12 shrink-0 place-items-center overflow-hidden rounded-xl border border-hairline-strong">
                    {study.thumbnail ? (
                      <img src={study.thumbnail} alt="" className="h-full w-full object-cover" />
                    ) : (
                      <span className="text-[13px] font-bold" style={{ color: modColor }}>{getInitials(study.patient_name)}</span>
                    )}
                  </div>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-[13.5px] font-semibold text-foreground">{displayPatientName(study.patient_name)}</p>
                    <p className="mt-0.5 truncate font-mono text-[11px] text-faint">{study.patient_id}</p>
                  </div>
                  <StudyMediaButton onClick={() => onActionClick('burn', study)} />
                  <StudyZipButton onClick={() => onActionClick('export-zip', study)} />
                  <span
                    className="shrink-0 rounded px-1.5 py-0.5 font-mono text-[10px] font-bold"
                    style={{ backgroundColor: modColor, color: '#04121f' }}
                  >
                    {primaryMod}
                  </span>
                </div>

                <p className="mt-3 line-clamp-2 min-h-[32px] text-[12.5px] leading-relaxed text-muted">
                  {study.description || 'No description'}
                </p>

                <div className="mt-3 flex items-center justify-between border-t border-hairline pt-2.5 font-mono text-[10.5px] text-faint">
                  <span>{study.study_date}</span>
                  <span>{study.n_series}s · {study.n_instances}i</span>
                </div>

                <div className="mt-2 flex items-center justify-between">
                  <div className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
                    <TooltipProvider>
                      {study.dictation_count !== undefined && <DictationAction studyUid={study.study_uid} count={study.dictation_count} />}
                      <RowAction label="Write report" hint="Write / edit the diagnostic report" tone="hover:text-ok" onClick={() => window.paxsOpenReportModal?.(study.study_uid)}>
                        <FileText className="h-3.5 w-3.5" />
                      </RowAction>
                      <LaunchWorkflow studyUid={study.study_uid} iconOnly />
                      <RowAction label="Open in desktop viewer" hint="Open in PAXS Viewer for Windows" tone="hover:text-ok" href={getDesktopViewerUri(study.study_uid)}>
                        <MonitorPlay className="h-3.5 w-3.5" />
                      </RowAction>
                      <RowAction label="Share study" hint="Share study" tone="hover:text-[#a78bfa]" onClick={() => onActionClick('share', study)}>
                        <Share2 className="h-3.5 w-3.5" />
                      </RowAction>
                    </TooltipProvider>
                  </div>
                  <span className="inline-flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wider text-accent opacity-0 transition-opacity group-hover:opacity-100">
                    <Info className="h-3 w-3" aria-hidden="true" /> Inspect
                  </span>
                </div>
              </div>
            );
          })
        )}
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-panel border border-hairline bg-panel/55 shadow-[var(--paxs-shadow-soft)] backdrop-blur-xl">
      <div className="min-h-0 flex-1 overflow-auto">
        <div className="min-w-[1000px]">
          {/* Header — sticks to the top of the scroll well, so the column names
              stay readable once the list is longer than the viewport. */}
          <div className={cn('sticky top-0 z-10 grid gap-3 border-b border-hairline bg-ink/95 px-4 py-2.5 backdrop-blur-xl', ROW_GRID)}>
            <div aria-hidden="true" />
            <SortHeader label="Patient" sortKey="patient" />
            <SortHeader label="Study" sortKey="date" />
            <SortHeader label="Accession" sortKey="accession" />
            <div className="flex items-center gap-3">
              <SortHeader label="Series" sortKey="series" />
              <span aria-hidden="true" className="h-3 w-px bg-hairline-strong" />
              <SortHeader label="Images" sortKey="instances" />
            </div>
            <span className="font-mono text-[10px] font-semibold uppercase tracking-[0.12em] text-faint">Mod</span>
            <span className="text-right font-mono text-[10px] font-semibold uppercase tracking-[0.12em] text-faint">Actions</span>
          </div>

          {/* Body */}
          {loading ? (
            Array.from({ length: 8 }).map((_, i) => (
              <div key={i} className={cn('grid items-center gap-3 border-b border-hairline/60 px-4 py-2.5', ROW_GRID)}>
                <Skeleton className="h-10 w-10 rounded-xl" />
                <div className="space-y-2"><Skeleton className="h-3.5 w-32" /><Skeleton className="h-3 w-20" /></div>
                <div className="space-y-2"><Skeleton className="h-3.5 w-44" /><Skeleton className="h-3 w-28" /></div>
                <Skeleton className="h-6 w-20 rounded" />
                <Skeleton className="h-6 w-20 rounded-full" />
                <Skeleton className="h-5 w-10 rounded" />
                <div className="flex justify-end gap-1.5"><Skeleton className="h-8 w-8 rounded-lg" /><Skeleton className="h-8 w-8 rounded-lg" /></div>
              </div>
            ))
          ) : studies.length === 0 ? (
            <div className="flex flex-col items-center justify-center gap-2 py-20 text-center">
              <IconStudies className="size-10 opacity-70" />
              <h3 className="text-base font-semibold text-foreground">No studies found</h3>
              <p className="text-sm text-muted">Try adjusting your search or filters.</p>
            </div>
          ) : (
            studies.map((study) => {
              const primaryMod = study.modalities[0] || 'OT';
              const modColor = getModColor(primaryMod);
              return (
                <div
                  key={study.study_uid}
                  className={cn(
                    'group relative grid cursor-pointer items-center gap-3 border-b border-hairline/60 px-4 py-2 transition-colors duration-150',
                    'last:border-b-0 hover:bg-raised/45',
                    ROW_GRID,
                  )}
                  onClick={() => onStudyClick(study)}
                >
                  <span aria-hidden="true" className="mod-rail" style={{ backgroundColor: modColor }} />

                  {/* Thumbnail */}
                  <div className="thumb-well scan-sweep relative grid h-10 w-10 place-items-center overflow-hidden rounded-xl border border-hairline-strong">
                    {study.thumbnail ? (
                      <img src={study.thumbnail} alt="" className="h-full w-full object-cover" />
                    ) : (
                      <span className="text-[12px] font-bold" style={{ color: modColor }}>{getInitials(study.patient_name)}</span>
                    )}
                    <span
                      className="absolute bottom-0 left-0 rounded-tr px-1 font-mono text-[8px] font-bold leading-[13px]"
                      style={{ backgroundColor: modColor, color: '#04121f' }}
                    >
                      {primaryMod}
                    </span>
                  </div>

                  {/* Patient */}
                  <div className="flex min-w-0 items-center gap-1">
                    <StudyMediaButton onClick={() => onActionClick('burn', study)} />
                    <StudyZipButton onClick={() => onActionClick('export-zip', study)} />
                    <div className="min-w-0">
                    <p className="truncate text-[13px] font-semibold text-foreground">{displayPatientName(study.patient_name)}</p>
                    <div className="mt-1 flex flex-wrap items-center gap-1.5">
                      <span className="truncate font-mono text-[10.5px] text-faint">{study.patient_id}</span>
                      {study.patient_source && (
                        <Badge variant="info" className="h-[15px] px-1 py-0 text-[8.5px] leading-none">HIS</Badge>
                      )}
                    </div>
                    </div>
                  </div>

                  {/* Study */}
                  <div className="min-w-0">
                    <p className="truncate text-[13px] text-foreground/90">{study.description || 'No description'}</p>
                    <div className="mt-1 flex flex-wrap items-center gap-1.5">
                      <span className="shrink-0 whitespace-nowrap font-mono text-[10.5px] text-faint">
                        {study.study_date} {study.study_time}
                      </span>
                      <Badge
                        variant={study.lifecycle_state === 'ONLINE' ? 'success' : 'warning'}
                        className="h-[15px] px-1.5 py-0 text-[8.5px] leading-none"
                      >
                        {study.lifecycle_state}
                      </Badge>
                      {study.report_status && (
                        <Badge variant="outline" className="h-[15px] px-1.5 py-0 text-[8.5px] leading-none">
                          {study.report_status}
                        </Badge>
                      )}
                      {study.claimed_by && (
                        <Badge
                          variant="warning"
                          className="h-[15px] max-w-[120px] truncate px-1.5 py-0 text-[8.5px] leading-none"
                          title={`Being read by ${study.claimed_by}`}
                        >
                          READING · {study.claimed_by}
                        </Badge>
                      )}
                    </div>
                  </div>

                  {/* Accession */}
                  <div className="min-w-0">
                    <span className="inline-block max-w-full truncate rounded border border-hairline bg-ink/50 px-2 py-0.5 font-mono text-[10.5px] text-muted">
                      {study.accession}
                    </span>
                  </div>

                  {/* Series / Images */}
                  <div>
                    <span className="inline-flex items-center gap-1 rounded-full border border-hairline bg-raised-2/70 px-2 py-0.5 font-mono text-[10.5px] text-foreground/85">
                      {study.n_series}s <span className="text-faint">·</span> {study.n_instances}i
                    </span>
                  </div>

                  {/* Modalities */}
                  <div className="flex flex-wrap items-center gap-1">
                    {study.modalities.map((mod) => (
                      <span
                        key={mod}
                        className="rounded px-1.5 py-0.5 font-mono text-[9.5px] font-bold"
                        style={{ backgroundColor: getModColor(mod), color: '#04121f' }}
                      >
                        {mod}
                      </span>
                    ))}
                  </div>

                  {/* Actions */}
                  <div className="flex items-center justify-end gap-0.5" onClick={(e) => e.stopPropagation()}>
                    <LaunchWorkflow studyUid={study.study_uid} iconOnly />
                    <TooltipProvider>
                      {study.dictation_count !== undefined && <DictationAction studyUid={study.study_uid} count={study.dictation_count} />}
                      <RowAction label="Write report" hint="Write / edit the diagnostic report" tone="hover:text-ok" onClick={() => window.paxsOpenReportModal?.(study.study_uid)}>
                        <FileText className="h-3.5 w-3.5" />
                      </RowAction>
                      <RowAction label="Quick inspect" hint="Quick inspect" tone="hover:text-accent" onClick={() => onStudyClick(study)}>
                        <Info className="h-3.5 w-3.5" />
                      </RowAction>
                      <RowAction label="Open in desktop viewer" hint="Open in PAXS Viewer for Windows" tone="hover:text-ok" href={getDesktopViewerUri(study.study_uid)}>
                        <MonitorPlay className="h-3.5 w-3.5" />
                      </RowAction>
                      <RowAction label="Forward to node" hint="Forward to a DICOM node" tone="hover:text-teal" onClick={() => onActionClick('forward', study)}>
                        <Forward className="h-3.5 w-3.5" />
                      </RowAction>
                      <RowAction label="Share study" hint="Share study" tone="hover:text-[#a78bfa]" onClick={() => onActionClick('share', study)}>
                        <Share2 className="h-3.5 w-3.5" />
                      </RowAction>
                      {canDelete && (
                        <RowAction label="Delete study" hint="Delete study (administrator only)" tone="hover:text-danger" onClick={() => onActionClick('delete', study)}>
                          <Trash2 className="h-3.5 w-3.5" />
                        </RowAction>
                      )}
                    </TooltipProvider>
                  </div>
                </div>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}
