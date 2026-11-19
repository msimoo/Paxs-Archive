import { useEffect, useState } from 'react';
import { Disc3 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { ApiError, displayPatientName, getDiscBurn, startDiscBurn, type DiscBurnStatus } from '@/lib/api';
import type { Study } from '@/types/study';

const PHASE_LABELS = {
  queued: 'Checking the recorder and disc…',
  preparing: 'Preparing DICOM files…',
  writing: 'Writing the disc…',
  verifying: 'Reading back and verifying the disc…',
};

export function BurnStudyModal({ study, onClose }: { study: Study; onClose: () => void }) {
  const [trigger] = useState(() => document.activeElement);
  const [jobId, setJobId] = useState('');
  const [status, setStatus] = useState<DiscBurnStatus | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState('');
  const phase = status?.phase;
  const tracking = Boolean(phase && phase !== 'failed' && phase !== 'verified');

  useEffect(() => {
    if (!tracking) return;
    const controller = new AbortController();
    let timer: number;
    async function check() {
      try {
        const next = await getDiscBurn(jobId, controller.signal);
        if (controller.signal.aborted) return;
        setStatus(next);
        setError('');
        if (next.phase === 'failed' || next.phase === 'verified') return;
      } catch (err) {
        if (controller.signal.aborted) return;
        if (err instanceof ApiError && err.status === 404) {
          setStatus({ phase: 'failed', count: 0, error: 'The server lost this job. Check the disc before starting a new burn.' });
          return;
        }
        setError('Connection lost. Rechecking burn status…');
      }
      timer = window.setTimeout(check, 1500);
    }
    timer = window.setTimeout(check, 1500);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [jobId, tracking]);

  async function burn() {
    // getRandomValues also works on the HTTP origins used by local PACS installations.
    const id = jobId && phase !== 'failed' ? jobId : Array.from(
      crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, '0'),
    ).join('');
    setJobId(id);
    setStarting(true);
    setError('');
    setStatus(null);
    try {
      setStatus(await startDiscBurn(id, study.study_uid));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not start the burn.');
    } finally {
      setStarting(false);
    }
  }

  const busy = starting || tracking;
  const done = phase === 'verified';
  return (
    <Dialog open onOpenChange={(open) => { if (!open && !busy) onClose(); }}>
      <DialogContent className={busy ? 'sm:max-w-md [&>button:last-child]:hidden' : 'sm:max-w-md'} onCloseAutoFocus={(event) => {
        if (trigger instanceof HTMLElement && trigger.isConnected) {
          event.preventDefault();
          trigger.focus();
        }
      }}>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Disc3 className="h-5 w-5 text-cyan-400" aria-hidden="true" />
            Burn study to CD/DVD
          </DialogTitle>
          <DialogDescription>
            Insert a blank writable disc in the Windows PAXS server recorder. The disc contains identifiable patient data.
          </DialogDescription>
        </DialogHeader>
        <div className="rounded border border-slate-700 bg-slate-950 p-3 text-sm">
          <p className="font-semibold">{displayPatientName(study.patient_name)}</p>
          <p className="text-slate-400">{study.study_date} · {study.description || 'DICOM study'}</p>
          <p className="text-slate-400">{status?.count || study.n_instances} DICOM instances</p>
        </div>
        {starting && <p role="status" className="text-sm">Starting the burn…</p>}
        {phase && phase in PHASE_LABELS && (
          <p role="status" className="text-sm">{PHASE_LABELS[phase as keyof typeof PHASE_LABELS]}</p>
        )}
        {done && <p role="status" className="text-sm text-emerald-400">Disc verified. You can remove it from the recorder.</p>}
        {(error || status?.error) && <p role="alert" className="text-sm text-rose-400">{error || status?.error}</p>}
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={busy}>{busy || done || phase === 'failed' ? 'Close' : 'Cancel'}</Button>
          {!done && <Button onClick={burn} disabled={busy}>Burn disc</Button>}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
