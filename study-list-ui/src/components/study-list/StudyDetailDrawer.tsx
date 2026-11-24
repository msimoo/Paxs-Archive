import { useEffect, useState } from 'react';
import type { Study } from '@/types/study';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { getModColor } from '@/lib/constants';
import {
  fetchStudyDetail,
  fetchStudyReport,
  displayPatientName,
  type StudyDetail,
  type StudyReport,
} from '@/lib/api';
import { Copy, Share2, Trash2, Forward, FileText, Image as ImageIcon } from 'lucide-react';
import { LaunchWorkflow } from './LaunchWorkflow';
import { ScrollArea } from '@/components/ui/scroll-area';

interface StudyDetailDrawerProps {
  study: Study | null;
  isOpen: boolean;
  onClose: () => void;
  onAction: (action: 'share' | 'forward' | 'delete', study: Study) => void;
  onToast: (msg: string) => void;
  /** Delete is administrator-only; the control is not rendered otherwise. */
  canDelete?: boolean;
}

/** DICOM DA (YYYYMMDD) -> YYYY-MM-DD; anything else is shown verbatim. */
function formatDicomDate(value?: string | null): string | null {
  if (!value) return null;
  const raw = value.trim();
  if (raw.length === 8 && /^[0-9]{8}$/.test(raw)) {
    return `${raw.slice(0, 4)}-${raw.slice(4, 6)}-${raw.slice(6, 8)}`;
  }
  return raw;
}

export function StudyDetailDrawer({ study, isOpen, onClose, onAction, onToast, canDelete = false }: StudyDetailDrawerProps) {
  // C07: everything rendered here comes from the archive. The drawer previously
  // showed invented demographics and "normal" report text for any study that had
  // a report status, i.e. a fabricated clinical record. Real data is fetched
  // instead, with explicit loading / empty / error states.
  const [detail, setDetail] = useState<StudyDetail | null>(null);
  const [report, setReport] = useState<StudyReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const studyUid = study?.study_uid ?? null;

  useEffect(() => {
    if (!studyUid || !isOpen) return;
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    setDetail(null);
    setReport(null);
    Promise.all([
      fetchStudyDetail(studyUid, controller.signal),
      fetchStudyReport(studyUid, controller.signal),
    ])
      .then(([d, r]) => {
        setDetail(d);
        setReport(r);
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setError(err instanceof Error ? err.message : 'Could not load study data');
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [studyUid, isOpen]);

  if (!study) return null;

  const copyUid = () => {
    navigator.clipboard.writeText(study.study_uid);
    onToast('Study UID copied to clipboard');
  };

  const dob = formatDicomDate(detail?.patient.birth_date ?? study.patient_birth_date);
  const sex = detail?.patient.sex || study.patient_sex || null;
  const seriesCount = detail ? detail.series.length : study.n_series;
  const hasReportText = Boolean(
    report && (report.clinical_history || report.findings || report.impression)
  );

  return (
    <Dialog open={isOpen} onOpenChange={(open: boolean) => !open && onClose()}>
      <DialogContent className="max-w-2xl bg-panel/95 border-hairline backdrop-blur-2xl p-0 flex flex-col max-h-[90vh]">
        {/* Header / Patient Banner */}
        <div className="p-6 border-b border-hairline bg-raised/70">
          <DialogHeader className="mb-4 text-left">
            <div className="flex items-start justify-between">
              <div>
                <DialogTitle className="text-2xl font-bold text-white mb-1">
                  {displayPatientName(study.patient_name)}
                </DialogTitle>
                <div className="flex items-center gap-3 text-sm text-muted">
                  <span className="font-mono">{study.patient_id}</span>
                  <span>•</span>
                  {/* Real demographics only — never a substituted default. */}
                  <span>{dob ? `DOB: ${dob}` : 'DOB: Unknown'}</span>
                  <span>•</span>
                  <span>{sex || 'Sex: N/A'}</span>
                </div>
              </div>
              {study.patient_source && (
                <Badge variant="outline" className="border-teal/40 text-teal bg-teal/10">
                  {study.patient_source}
                </Badge>
              )}
            </div>
          </DialogHeader>

          {/* Action Toolbar */}
          <div className="flex items-center gap-2">
            <LaunchWorkflow studyUid={study.study_uid} className="flex-1" />
            <Button className="bg-gradient-to-r from-emerald-600 to-teal-600 hover:brightness-110 text-white font-bold" onClick={() => window.paxsOpenReportModal?.(study.study_uid)}>
              <FileText className="w-4 h-4 mr-2" /> 📝 Diagnostic Report
            </Button>
            <Button variant="outline" size="icon" className="border-hairline-strong text-[#a78bfa] hover:bg-[#a78bfa]/10" onClick={() => onAction('share', study)}>
              <Share2 className="w-4 h-4" />
            </Button>
            <Button variant="outline" size="icon" className="border-hairline-strong text-teal hover:bg-teal/10" onClick={() => onAction('forward', study)}>
              <Forward className="w-4 h-4" />
            </Button>
            {canDelete && (
              <Button variant="outline" size="icon" className="border-hairline-strong text-danger hover:bg-danger/10" onClick={() => onAction('delete', study)} aria-label="Delete study">
                <Trash2 className="w-4 h-4" />
              </Button>
            )}
          </div>
        </div>

        <ScrollArea className="flex-1 max-h-[60vh]">
          <div className="p-6 space-y-6">
            {/* Study Details */}
            <section>
              <h3 className="text-xs font-semibold text-muted uppercase tracking-wider mb-3 font-mono">Study Details</h3>
              <div className="grid grid-cols-2 gap-4 bg-raised/35 p-4 rounded-xl border border-hairline">
                <div>
                  <div className="text-xs text-faint mb-1">Accession Number</div>
                  <div className="text-sm font-mono text-foreground">{study.accession || 'N/A'}</div>
                </div>
                <div>
                  <div className="text-xs text-faint mb-1">Date &amp; Time</div>
                  <div className="text-sm text-foreground">{study.study_date} {study.study_time || ''}</div>
                </div>
                <div className="col-span-2">
                  <div className="text-xs text-faint mb-1">Description</div>
                  <div className="text-sm text-foreground">{study.description || 'No Description'}</div>
                </div>
                <div className="col-span-2">
                  <div className="text-xs text-faint mb-1">Modalities</div>
                  <div className="flex gap-1 mt-1">
                    {study.modalities.map(mod => (
                      <Badge key={mod} variant="secondary" className="font-mono text-xs" style={{ backgroundColor: getModColor(mod), color: '#04121f' }}>
                        {mod}
                      </Badge>
                    ))}
                  </div>
                </div>
                <div className="col-span-2 flex items-center justify-between group">
                  <div>
                    <div className="text-xs text-faint mb-1">Study Instance UID</div>
                    <div className="text-xs font-mono text-muted truncate max-w-[280px]" title={study.study_uid}>
                      {study.study_uid}
                    </div>
                  </div>
                  <Button variant="ghost" size="icon" className="h-6 w-6 opacity-0 group-hover:opacity-100 transition-opacity" onClick={copyUid}>
                    <Copy className="w-3 h-3 text-muted" />
                  </Button>
                </div>
              </div>
            </section>

            {/* Diagnostic Report Summary — real report text only */}
            <section>
              <div className="flex items-center justify-between mb-3">
                <h3 className="text-xs font-semibold text-muted uppercase tracking-wider font-mono">Diagnostic Report</h3>
                {study.report_status ? (
                  <Badge variant="outline" className={study.report_status === 'FINALIZED' ? "border-ok/45 text-ok" : "border-warn/45 text-warn"}>
                    {study.report_status}
                  </Badge>
                ) : (
                  <Badge variant="outline" className="border-hairline-strong text-faint">NO REPORT</Badge>
                )}
              </div>
              <div className="bg-raised/35 p-4 rounded-xl border border-hairline space-y-3 relative overflow-hidden">
                {study.report_status ? (
                  <>
                    {loading && <p className="text-sm text-faint">Loading report…</p>}
                    {!loading && error && <p className="text-sm text-danger">{error}</p>}
                    {!loading && !error && hasReportText && (
                      <>
                        {report?.clinical_history && (
                          <div>
                            <div className="text-xs font-semibold text-foreground/85 mb-1">Clinical History</div>
                            <p className="text-sm text-muted line-clamp-2">{report.clinical_history}</p>
                          </div>
                        )}
                        {report?.findings && (
                          <div>
                            <div className="text-xs font-semibold text-foreground/85 mb-1">Findings</div>
                            <p className="text-sm text-muted line-clamp-3">{report.findings}</p>
                          </div>
                        )}
                        {report?.impression && (
                          <div>
                            <div className="text-xs font-semibold text-foreground/85 mb-1">Impression</div>
                            <p className="text-sm font-medium text-foreground line-clamp-2">{report.impression}</p>
                          </div>
                        )}
                      </>
                    )}
                    {!loading && !error && !hasReportText && (
                      <p className="text-sm text-faint">
                        This report has no clinical text recorded yet. Open the report editor to read or write it.
                      </p>
                    )}
                    <Button variant="link" className="text-accent h-auto p-0 mt-2 text-sm" onClick={() => window.paxsOpenReportModal?.(study.study_uid)}>
                      <FileText className="w-4 h-4 mr-2" /> View &amp; Edit Full Report
                    </Button>
                  </>
                ) : (
                  <div className="text-center py-6 text-faint flex flex-col items-center">
                    <FileText className="w-8 h-8 mb-2 opacity-20" />
                    <p className="text-sm">No report generated for this study</p>
                    <Button variant="outline" size="sm" className="mt-3 border-ok/45 text-ok hover:bg-ok/10" onClick={() => window.paxsOpenReportModal?.(study.study_uid)}>📝 Create / Edit Report</Button>
                  </div>
                )}
              </div>
            </section>

            {/* Series Breakdown — real series from the archive */}
            <section>
              <h3 className="text-xs font-semibold text-muted uppercase tracking-wider mb-3 font-mono">Series ({seriesCount})</h3>
              <div className="space-y-2">
                {loading && <div className="text-sm text-faint p-3">Loading series…</div>}
                {!loading && error && <div className="text-sm text-danger p-3">{error}</div>}
                {!loading && !error && seriesCount === 0 && (
                  <div className="text-sm text-faint p-3">No series recorded for this study.</div>
                )}
                {!loading && !error && detail?.series.map((s, i) => (
                  <div key={s.series_uid} className="flex items-center gap-3 p-3 bg-raised/35 rounded-xl border border-hairline hover:bg-raised transition-colors group">
                    <div className="w-10 h-10 bg-ink rounded flex items-center justify-center border border-hairline-strong overflow-hidden">
                      {s.thumbnail
                        ? <img src={s.thumbnail} alt="" className="w-full h-full object-cover" />
                        : <ImageIcon className="w-4 h-4 text-faint/70" />}
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="text-sm font-medium text-foreground truncate">
                        Series {s.series_number ?? i + 1}{s.description ? ` — ${s.description}` : ''}
                      </div>
                      <div className="text-xs text-faint mt-0.5">
                        {s.modality || 'N/A'} • {s.instances} image{s.instances === 1 ? '' : 's'}
                        {s.body_part ? ` • ${s.body_part}` : ''}
                      </div>
                    </div>
                    <LaunchWorkflow studyUid={study.study_uid} iconOnly className="text-accent" />
                  </div>
                ))}
              </div>
            </section>
          </div>
        </ScrollArea>
      </DialogContent>
    </Dialog>
  );
}
