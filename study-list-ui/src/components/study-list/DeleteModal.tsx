import type { Study } from '@/types/study';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { AlertTriangle } from 'lucide-react';
import { useState } from 'react';

interface DeleteModalProps {
  study: Study | null;
  isOpen: boolean;
  onClose: () => void;
  onConfirm: (uid: string) => void;
}

export function DeleteModal({ study, isOpen, onClose, onConfirm }: DeleteModalProps) {
  const [loading, setLoading] = useState(false);

  if (!study) return null;

  const handleDelete = () => {
    setLoading(true);
    // Simulate delete process, typically you'd await an API call
    setTimeout(() => {
      setLoading(false);
      onConfirm(study.study_uid);
      onClose();
    }, 1000);
  };

  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && onClose()}>
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
