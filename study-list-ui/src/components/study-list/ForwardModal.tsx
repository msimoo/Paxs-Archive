import type { Study } from '@/types/study';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Forward, Server } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { displayPatientName, fetchNodes, forwardStudy, type DicomNode } from '@/lib/api';

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
      const result = await forwardStudy(study.study_uid, node);
      if (result.sent === 0) {
        setError(result.reason || `C-STORE failed: ${result.failed} instance(s) could not be sent to ${node}.`);
        return;
      }
      if (result.failed > 0) {
        onToast(`C-STORE partially completed: ${result.sent} sent, ${result.failed} failed to ${node}.`);
      } else if (result.ok) {
        onToast(`C-STORE completed: ${result.sent} instance(s) sent to ${node}.`);
      } else {
        setError(result.reason || `C-STORE returned an unsuccessful status after sending ${result.sent} instance(s).`);
        return;
      }
      onClose();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'C-STORE forwarding failed. Please retry.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && !loading && onClose()}>
      <DialogContent className="bg-panel border-hairline-strong text-foreground sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="text-xl font-bold flex items-center gap-2">
            <Forward className="w-5 h-5 text-teal" />
            C-STORE Forward
          </DialogTitle>
        </DialogHeader>

        <div className="space-y-6 py-4">
          <div className="space-y-2">
            <label className="text-sm text-muted">Study to Forward</label>
            <div className="p-3 bg-ink rounded border border-hairline text-sm">
              <div className="font-semibold text-foreground">{displayPatientName(study.patient_name)}</div>
              <div className="text-faint font-mono text-xs mt-1">UID: {study.study_uid}</div>
            </div>
          </div>

          <div className="space-y-2">
            <label className="text-sm text-muted">Destination Node</label>
            {loadingNodes ? (
              <div className="text-sm text-faint p-3">Loading nodes…</div>
            ) : nodes.length === 0 ? (
              <div className="text-sm text-warn p-3 bg-warn/10 border border-warn/40 rounded">
                No enabled DICOM nodes are configured on this server.
              </div>
            ) : (
              <Select value={node} onValueChange={setNode}>
                <SelectTrigger className="bg-ink border-hairline-strong text-white">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent className="bg-panel border-hairline-strong text-foreground">
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
            <div className="p-3 bg-danger/12 border border-danger/40 rounded-lg text-sm text-danger">
              {error}
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={loading} className="border-hairline-strong text-foreground/85 hover:bg-raised-2">
            Cancel
          </Button>
          <Button onClick={handleForward} disabled={loading || !node} className="bg-teal hover:brightness-110 text-white">
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
