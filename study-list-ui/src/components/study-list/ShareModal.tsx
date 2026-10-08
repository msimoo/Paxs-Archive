import type { Study } from '@/types/study';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Copy, Link, Check } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { createShare, type ShareResult } from '@/lib/api';

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
      <DialogContent className="bg-[#0c1424] border-slate-700 text-slate-200 sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="text-xl font-bold flex items-center gap-2">
            <Link className="w-5 h-5 text-indigo-400" />
            Share Study
          </DialogTitle>
        </DialogHeader>

        <div className="space-y-6 py-4">
          <div className="space-y-2">
            <label className="text-sm text-slate-400">Patient</label>
            <div className="font-semibold">{study.patient_name.replace(/\^/g, ' ')}</div>
          </div>

          <div className="flex flex-col items-center justify-center p-6 bg-[#070c18] rounded-xl border border-slate-800">
            {loading && <div className="text-sm text-slate-500 py-10">Creating share link…</div>}
            {!loading && error && (
              <div className="text-sm text-rose-300 text-center py-6">{error}</div>
            )}
            {!loading && !error && qrSrc && (
              <>
                <img src={qrSrc} alt="Share link QR code" className="w-32 h-32 bg-white p-2 rounded-lg" />
                <p className="text-xs text-slate-500 mt-4 text-center">Scan to open in zero-footprint viewer</p>
              </>
            )}
            {!loading && !error && !qrSrc && (
              <p className="text-sm text-slate-500 text-center py-6">No share link available.</p>
            )}
          </div>

          <div className="space-y-2">
            <label className="text-sm text-slate-400">Expiration</label>
            <Select value={expiryDays} onValueChange={setExpiryDays}>
              <SelectTrigger className="bg-[#070c18] border-slate-700 text-white">
                <SelectValue />
              </SelectTrigger>
              <SelectContent className="bg-[#0c1424] border-slate-700 text-slate-200">
                <SelectItem value="1">24 Hours</SelectItem>
                <SelectItem value="7">7 Days</SelectItem>
                <SelectItem value="30">30 Days</SelectItem>
              </SelectContent>
            </Select>
          </div>

          {share && (
            <div className="space-y-2">
              <label className="text-sm text-slate-400">Share link</label>
              <div className="flex items-center gap-2">
                <input
                  readOnly
                  value={share.url}
                  onFocus={(e) => e.currentTarget.select()}
                  className="flex-1 bg-[#070c18] border border-slate-800 rounded px-3 py-2 text-xs font-mono text-slate-300"
                />
                <Button onClick={handleCopy} className="bg-indigo-600 hover:bg-indigo-500 text-white">
                  {copied ? <Check className="w-4 h-4" /> : <Copy className="w-4 h-4" />}
                </Button>
              </div>
              <p className="text-xs text-slate-500">Expires {share.expires_at}</p>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
