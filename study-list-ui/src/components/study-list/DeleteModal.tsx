import type { Study } from '@/types/study';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { AlertTriangle } from 'lucide-react';
import { useState } from 'react';
import { deleteStudy } from '@/lib/api';

interface DeleteModalProps {
  study: Study | null;
  isOpen: boolean;
  onClose: () => void;
  /** Called only after the server confirmed the deletion. */
  onConfirm: (uid: string) => void;
}

export function DeleteModal({ study, isOpen, onClose, onConfirm }: DeleteModalProps) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!study) return null;

  // H01: the delete is real. Success is reported only when the server confirms
  // it; the caller then refetches the list rather than removing the row locally.
  const handleDelete = async () => {
    setLoading(true);
    setError(null);
    try {
      await deleteStudy(study.study_uid);
      onConfirm(study.study_uid);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Delete failed');
    } finally {
      setLoading(false);
    }
  };

  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && !loading && onClose()}>
      <DialogContent className="bg-[#0c1424] border-rose-900/50 text-slate-200 sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="text-xl font-bold flex items-center gap-2 text-rose-500">
            <AlertTriangle className="w-6 h-6" />
            Delete Study
          </DialogTitle>
        </DialogHeader>

        <div className="py-6 space-y-4">
          <p className="text-slate-300">
            Are you sure you want to permanently delete this study? This action cannot be undone and will remove all associated series and instances from the archive.
          </p>
          <div className="p-4 bg-rose-950/20 border border-rose-900/30 rounded-lg">
            <div className="font-semibold text-rose-200">{study.patient_name.replace(/\^/g, ' ')}</div>
            <div className="text-sm text-slate-400 mt-1">{study.description || 'No description'}</div>
            <div className="text-xs font-mono text-slate-500 mt-2 break-all">{study.study_uid}</div>
          </div>
          {error && (
            <div className="p-3 bg-rose-950/40 border border-rose-800 rounded-lg text-sm text-rose-200">
              {error}
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={loading} className="border-slate-700 text-slate-300 hover:bg-slate-800 hover:text-white">
            Cancel
          </Button>
          <Button onClick={handleDelete} disabled={loading} variant="destructive" className="bg-rose-600 hover:bg-rose-500 text-white">
            {loading ? 'Deleting...' : 'Confirm Delete'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
