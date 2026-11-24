import type { Study } from '@/types/study';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { AlertTriangle } from 'lucide-react';
import { useState } from 'react';
import { ApiError, deleteStudy, displayPatientName } from '@/lib/api';

interface DeleteModalProps {
  study: Study | null;
  isOpen: boolean;
  onClose: () => void;
  /** Called only after the server confirmed the deletion. */
  onConfirm: (uid: string) => void;
}

/**
 * Actionable copy for the statuses the delete endpoint actually returns.
 *
 * The raw text was `Request failed (403): Requires one of ('ADMIN',
 * 'RADIOLOGIST') roles` — accurate, but it read as a broken button rather than
 * a permissions answer. 409 is the C-19 case: a signed report outlives its
 * images, so the study is refused rather than half-deleted.
 */
function deleteErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401) return 'Your session expired. Sign in again to delete studies.';
    if (err.status === 403) return 'Only an administrator can delete studies.';
    if (err.status === 404) return 'This study is no longer in your archive scope — refresh the list.';
    if (err.status === 409) return err.message.replace(/^Request failed \(\d+\):\s*/, '');
    if (err.status >= 500) {
      return 'The server could not delete this study. Nothing was removed — retry, or check the server log.';
    }
  }
  return err instanceof Error ? err.message : 'Delete failed';
}

export function DeleteModal({ study, isOpen, onClose, onConfirm }: DeleteModalProps) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!study) return null;

  // Report success only after the server confirms it; the caller removes the row
  // immediately and refreshes the authoritative totals in the background.
  const handleDelete = async () => {
    setLoading(true);
    setError(null);
    try {
      await deleteStudy(study.study_uid);
      onConfirm(study.study_uid);
      onClose();
    } catch (err) {
      setError(deleteErrorMessage(err));
    } finally {
      setLoading(false);
    }
  };

  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && !loading && onClose()}>
      <DialogContent className="bg-panel border-danger/40 text-foreground sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="text-xl font-bold flex items-center gap-2 text-danger">
            <AlertTriangle className="w-6 h-6" />
            Delete Study
          </DialogTitle>
        </DialogHeader>

        <div className="py-6 space-y-4">
          <p className="text-foreground/85">
            Are you sure you want to permanently delete this study? This action cannot be undone and will remove all associated series and instances from the archive.
          </p>
          <div className="p-4 bg-danger/8 border border-danger/25 rounded-lg">
            <div className="font-semibold text-danger">{displayPatientName(study.patient_name)}</div>
            <div className="text-sm text-muted mt-1">{study.description || 'No description'}</div>
            <div className="text-xs font-mono text-faint mt-2 break-all">{study.study_uid}</div>
          </div>
          {error && (
            <div className="p-3 bg-danger/12 border border-danger/40 rounded-lg text-sm text-danger">
              {error}
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={loading} className="border-hairline-strong text-foreground/85 hover:bg-raised-2 hover:text-foreground">
            Cancel
          </Button>
          <Button onClick={handleDelete} disabled={loading} variant="destructive" className="bg-danger hover:bg-danger text-white">
            {loading ? 'Deleting...' : 'Confirm Delete'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
