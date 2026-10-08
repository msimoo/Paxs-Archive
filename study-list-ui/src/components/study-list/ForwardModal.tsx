import type { Study } from '@/types/study';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Forward, Server } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { fetchNodes, forwardStudy, type DicomNode } from '@/lib/api';

interface ForwardModalProps {
  study: Study | null;
  isOpen: boolean;
  onClose: () => void;
  onToast: (msg: string) => void;
}

export function ForwardModal({ study, isOpen, onClose, onToast }: ForwardModalProps) {
  const [nodes, setNodes] = useState<DicomNode[]>([]);
  const [node, setNode] = useState('');
  const [loadingNodes, setLoadingNodes] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // H01: destinations come from the archive's own node registry — the previous
  // hardcoded list (with invented IPs) is gone.
  useEffect(() => {
    if (!isOpen) return;
    const controller = new AbortController();
    setLoadingNodes(true);
    setError(null);
    fetchNodes(controller.signal)
      .then((list) => {
        const usable = list.filter((n) => n.enabled);
        setNodes(usable);
        setNode(usable.length ? usable[0].ae_title : '');
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) {
          setError(err instanceof Error ? err.message : 'Could not load DICOM nodes');
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingNodes(false);
      });
    return () => controller.abort();
  }, [isOpen]);

  if (!study) return null;

  const handleForward = async () => {
    if (!node) return;
    setLoading(true);
    setError(null);
    try {
      await forwardStudy(study.study_uid, node);
      // H01: the backend queues the job, so the wording must not claim delivery.
      onToast(`Queued for forwarding to ${node}`);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Forward failed');
    } finally {
      setLoading(false);
    }
  };

  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && !loading && onClose()}>
      <DialogContent className="bg-[#0c1424] border-slate-700 text-slate-200 sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="text-xl font-bold flex items-center gap-2">
            <Forward className="w-5 h-5 text-teal-400" />
            C-STORE Forward
          </DialogTitle>
        </DialogHeader>

        <div className="space-y-6 py-4">
          <div className="space-y-2">
            <label className="text-sm text-slate-400">Study to Forward</label>
            <div className="p-3 bg-[#070c18] rounded border border-slate-800 text-sm">
              <div className="font-semibold text-slate-200">{study.patient_name.replace(/\^/g, ' ')}</div>
              <div className="text-slate-500 font-mono text-xs mt-1">UID: {study.study_uid}</div>
            </div>
          </div>

          <div className="space-y-2">
            <label className="text-sm text-slate-400">Destination Node</label>
            {loadingNodes ? (
              <div className="text-sm text-slate-500 p-3">Loading nodes…</div>
            ) : nodes.length === 0 ? (
              <div className="text-sm text-amber-300 p-3 bg-amber-950/20 border border-amber-900/40 rounded">
                No enabled DICOM nodes are configured on this server.
              </div>
            ) : (
              <Select value={node} onValueChange={setNode}>
                <SelectTrigger className="bg-[#070c18] border-slate-700 text-white">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent className="bg-[#0c1424] border-slate-700 text-slate-200">
                  {nodes.map((n) => (
                    <SelectItem key={n.ae_title} value={n.ae_title}>
                      {n.ae_title} ({n.host}:{n.port})
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </div>

          {error && (
            <div className="p-3 bg-rose-950/40 border border-rose-800 rounded-lg text-sm text-rose-200">
              {error}
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={loading} className="border-slate-700 text-slate-300 hover:bg-slate-800">
            Cancel
          </Button>
          <Button onClick={handleForward} disabled={loading || !node} className="bg-teal-600 hover:bg-teal-500 text-white">
            {loading ? (
              <span className="flex items-center gap-2">
                <Server className="w-4 h-4 animate-spin" /> Sending...
              </span>
            ) : (
              <span className="flex items-center gap-2">
                <Forward className="w-4 h-4" /> Send C-STORE
              </span>
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
