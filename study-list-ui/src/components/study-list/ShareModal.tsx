import type { Study } from '@/types/study';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Copy, Link, Check } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { createShare, displayPatientName, type ShareResult } from '@/lib/api';

interface ShareModalProps {
  study: Study | null;
  isOpen: boolean;
  onClose: () => void;
  onToast: (msg: string) => void;
}

export function ShareModal({ study, isOpen, onClose, onToast }: ShareModalProps) {
  const [copied, setCopied] = useState(false);
  const [expiryDays, setExpiryDays] = useState('1');
  const [share, setShare] = useState<ShareResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const studyUid = study?.study_uid ?? null;

  // H01: the link and QR come from POST /api/v1/share. The previous version
  // invented a placeholder domain and drew a QR-shaped random pattern.
  useEffect(() => {
    if (!studyUid || !isOpen) return;
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    setShare(null);
    createShare(studyUid, Number(expiryDays), controller.signal)
      .then(setShare)
      .catch((err: unknown) => {
        if (!controller.signal.aborted) {
          setError(err instanceof Error ? err.message : 'Could not create the share link');
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [studyUid, isOpen, expiryDays]);

  if (!study) return null;

  const handleCopy = () => {
    if (!share) return;
    navigator.clipboard.writeText(share.url);
    setCopied(true);
    onToast('Link copied to clipboard');
    setTimeout(() => setCopied(false), 2000);
  };

  // The API returns a data URL; be tolerant if a bare base64 payload arrives.
  const qrSrc = share?.qr_base64
    ? (share.qr_base64.startsWith('data:') ? share.qr_base64 : `data:image/png;base64,${share.qr_base64}`)
    : null;

  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="bg-panel border-hairline-strong text-foreground sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="text-xl font-bold flex items-center gap-2">
            <Link className="w-5 h-5 text-[#a78bfa]" />
            Share Study
          </DialogTitle>
        </DialogHeader>

        <div className="space-y-6 py-4">
          <div className="space-y-2">
            <label className="text-sm text-muted">Patient</label>
            <div className="font-semibold">{displayPatientName(study.patient_name)}</div>
          </div>

          <div className="flex flex-col items-center justify-center p-6 bg-ink rounded-xl border border-hairline">
            {loading && <div className="text-sm text-faint py-10">Creating share link…</div>}
            {!loading && error && (
              <div className="text-sm text-danger text-center py-6">{error}</div>
            )}
            {!loading && !error && qrSrc && (
              <>
                <img src={qrSrc} alt="Share link QR code" className="w-32 h-32 bg-white p-2 rounded-lg" />
                <p className="text-xs text-faint mt-4 text-center">Scan to open in zero-footprint viewer</p>
              </>
            )}
            {!loading && !error && !qrSrc && (
              <p className="text-sm text-faint text-center py-6">No share link available.</p>
            )}
          </div>

          <div className="space-y-2">
            <label className="text-sm text-muted">Expiration</label>
            <Select value={expiryDays} onValueChange={setExpiryDays}>
              <SelectTrigger className="bg-ink border-hairline-strong text-white">
                <SelectValue />
              </SelectTrigger>
              <SelectContent className="bg-panel border-hairline-strong text-foreground">
                <SelectItem value="1">24 Hours</SelectItem>
                <SelectItem value="7">7 Days</SelectItem>
                <SelectItem value="30">30 Days</SelectItem>
              </SelectContent>
            </Select>
          </div>

          {share && (
            <div className="space-y-2">
              <label className="text-sm text-muted">Share link</label>
              <div className="flex items-center gap-2">
                <input
                  readOnly
                  value={share.url}
                  onFocus={(e) => e.currentTarget.select()}
                  className="flex-1 bg-ink border border-hairline rounded px-3 py-2 text-xs font-mono text-foreground/85"
                />
                <Button onClick={handleCopy} className="bg-[#7c3aed] hover:brightness-110 text-white">
                  {copied ? <Check className="w-4 h-4" /> : <Copy className="w-4 h-4" />}
                </Button>
              </div>
              <p className="text-xs text-faint">Expires {share.expires_at}</p>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
