import type { Study } from '@/types/study';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { getModColor } from '@/lib/constants';
import { getViewerUrl } from '@/lib/api';
import { Copy, Eye, Share2, Trash2, Forward, FileText, Image as ImageIcon } from 'lucide-react';
import { ScrollArea } from '@/components/ui/scroll-area';

interface StudyDetailDrawerProps {
  study: Study | null;
  isOpen: boolean;
  onClose: () => void;
  onAction: (action: 'share' | 'forward' | 'delete', study: Study) => void;
  onToast: (msg: string) => void;
}

export function StudyDetailDrawer({ study, isOpen, onClose, onAction, onToast }: StudyDetailDrawerProps) {
  if (!study) return null;

  const copyUid = () => {
    navigator.clipboard.writeText(study.study_uid);
    onToast('Study UID copied to clipboard');
  };

  return (
    <Dialog open={isOpen} onOpenChange={(open: boolean) => !open && onClose()}>
      <DialogContent className="max-w-2xl bg-[#070c18]/95 border-slate-700/50 backdrop-blur-2xl p-0 flex flex-col max-h-[90vh]">
        {/* Header / Patient Banner */}
        <div className="p-6 border-b border-slate-800 bg-[#0c1424]/80">
          <DialogHeader className="mb-4 text-left">
            <div className="flex items-start justify-between">
              <div>
                <DialogTitle className="text-2xl font-bold text-white mb-1">
                  {study.patient_name.replace(/\^/g, ' ')}
                </DialogTitle>
                <div className="flex items-center gap-3 text-sm text-slate-400">
                  <span className="font-mono">{study.patient_id}</span>
                  <span>•</span>
                  <span>DOB: 1980-01-01</span>
                  <span>•</span>
                  <span>M</span>
                </div>
              </div>
              {study.patient_source && (
                <Badge variant="outline" className="border-teal-700 text-teal-400 bg-teal-950/30">
                  {study.patient_source}
                </Badge>
              )}
            </div>
          </DialogHeader>

          {/* Action Toolbar */}
          <div className="flex items-center gap-2">
            <Button className="flex-1 bg-cyan-600 hover:bg-cyan-500 text-white" onClick={() => window.open(getViewerUrl(study.study_uid), '_blank')}>
              <Eye className="w-4 h-4 mr-2" /> Open Viewer
            </Button>
            <Button className="bg-gradient-to-r from-emerald-600 to-teal-600 hover:brightness-110 text-white font-bold" onClick={() => (window as any).paxsOpenReportModal?.(study.study_uid)}>
              <FileText className="w-4 h-4 mr-2" /> 📝 Diagnostic Report
            </Button>
            <Button variant="outline" size="icon" className="border-slate-700 text-indigo-400 hover:bg-indigo-950/30" onClick={() => onAction('share', study)}>
              <Share2 className="w-4 h-4" />
            </Button>
            <Button variant="outline" size="icon" className="border-slate-700 text-teal-400 hover:bg-teal-950/30" onClick={() => onAction('forward', study)}>
              <Forward className="w-4 h-4" />
            </Button>
            <Button variant="outline" size="icon" className="border-slate-700 text-rose-400 hover:bg-rose-950/30" onClick={() => onAction('delete', study)}>
              <Trash2 className="w-4 h-4" />
            </Button>
          </div>
        </div>

        <ScrollArea className="flex-1 max-h-[60vh]">
          <div className="p-6 space-y-6">
            {/* Study Details */}
            <section>
              <h3 className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-3 font-mono">Study Details</h3>
              <div className="grid grid-cols-2 gap-4 bg-[#0c1424]/40 p-4 rounded-xl border border-slate-800">
                <div>
                  <div className="text-xs text-slate-500 mb-1">Accession Number</div>
                  <div className="text-sm font-mono text-slate-200">{study.accession || 'N/A'}</div>
                </div>
                <div>
                  <div className="text-xs text-slate-500 mb-1">Date &amp; Time</div>
                  <div className="text-sm text-slate-200">{study.study_date} {study.study_time || ''}</div>
                </div>
                <div className="col-span-2">
                  <div className="text-xs text-slate-500 mb-1">Description</div>
                  <div className="text-sm text-slate-200">{study.description || 'No Description'}</div>
                </div>
                <div className="col-span-2">
                  <div className="text-xs text-slate-500 mb-1">Modalities</div>
                  <div className="flex gap-1 mt-1">
                    {study.modalities.map(mod => (
                      <Badge key={mod} variant="secondary" className="font-mono text-xs" style={{ backgroundColor: getModColor(mod), color: '#070c18' }}>
                        {mod}
                      </Badge>
                    ))}
                  </div>
                </div>
                <div className="col-span-2 flex items-center justify-between group">
                  <div>
                    <div className="text-xs text-slate-500 mb-1">Study Instance UID</div>
                    <div className="text-xs font-mono text-slate-400 truncate max-w-[280px]" title={study.study_uid}>
                      {study.study_uid}
                    </div>
                  </div>
                  <Button variant="ghost" size="icon" className="h-6 w-6 opacity-0 group-hover:opacity-100 transition-opacity" onClick={copyUid}>
                    <Copy className="w-3 h-3 text-slate-400" />
                  </Button>
                </div>
              </div>
            </section>

            {/* Diagnostic Report Summary */}
            <section>
              <div className="flex items-center justify-between mb-3">
                <h3 className="text-xs font-semibold text-slate-400 uppercase tracking-wider font-mono">Diagnostic Report</h3>
                {study.report_status ? (
                  <Badge variant="outline" className={study.report_status === 'FINALIZED' ? "border-emerald-700 text-emerald-400" : "border-amber-700 text-amber-400"}>
                    {study.report_status}
                  </Badge>
                ) : (
                  <Badge variant="outline" className="border-slate-700 text-slate-500">NO REPORT</Badge>
                )}
              </div>
              <div className="bg-[#0c1424]/40 p-4 rounded-xl border border-slate-800 space-y-3 relative overflow-hidden">
                {study.report_status ? (
                  <>
                    <div>
                      <div className="text-xs font-semibold text-slate-300 mb-1">Clinical History</div>
                      <p className="text-sm text-slate-400 line-clamp-2">Patient presents with acute pain. Follow-up from previous examination.</p>
                    </div>
                    <div>
                      <div className="text-xs font-semibold text-slate-300 mb-1">Findings</div>
                      <p className="text-sm text-slate-400 line-clamp-3">Visualized structures appear unremarkable. No acute fracture or dislocation.</p>
                    </div>
                    <div>
                      <div className="text-xs font-semibold text-slate-300 mb-1">Impression</div>
                      <p className="text-sm font-medium text-slate-200">1. Unremarkable examination.</p>
                    </div>
                    <Button variant="link" className="text-cyan-400 h-auto p-0 mt-2 text-sm" onClick={() => (window as any).paxsOpenReportModal?.(study.study_uid)}>
                      <FileText className="w-4 h-4 mr-2" /> View &amp; Edit Full Report
                    </Button>
                  </>
                ) : (
                  <div className="text-center py-6 text-slate-500 flex flex-col items-center">
                    <FileText className="w-8 h-8 mb-2 opacity-20" />
                    <p className="text-sm">No report generated for this study</p>
                    <Button variant="outline" size="sm" className="mt-3 border-emerald-700 text-emerald-400 hover:bg-emerald-950/40" onClick={() => (window as any).paxsOpenReportModal?.(study.study_uid)}>📝 Create / Edit Report</Button>
                  </div>
                )}
              </div>
            </section>

            {/* Series Breakdown */}
            <section>
              <h3 className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-3 font-mono">Series ({study.n_series})</h3>
              <div className="space-y-2">
                {Array.from({ length: Math.min(study.n_series, 5) }).map((_, i) => (
                  <div key={i} className="flex items-center gap-3 p-3 bg-[#0c1424]/40 rounded-xl border border-slate-800 hover:bg-[#111c30] transition-colors group">
                    <div className="w-10 h-10 bg-[#070c18] rounded flex items-center justify-center border border-slate-700">
                       <ImageIcon className="w-4 h-4 text-slate-600" />
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="text-sm font-medium text-slate-200 truncate">Series {i + 1}</div>
                      <div className="text-xs text-slate-500 mt-0.5">#{i + 1} • {Math.floor((i + 1) * 15)} images</div>
                    </div>
                    <Button variant="ghost" size="icon" className="opacity-0 group-hover:opacity-100 text-cyan-400" onClick={() => window.open(getViewerUrl(study.study_uid), '_blank')}>
                      <Eye className="w-4 h-4" />
                    </Button>
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
