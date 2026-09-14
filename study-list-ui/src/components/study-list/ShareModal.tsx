import type { Study } from '@/types/study';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Copy, Link, Check } from 'lucide-react';
import { useState } from 'react';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';

interface ShareModalProps {
  study: Study | null;
  isOpen: boolean;
  onClose: () => void;
  onToast: (msg: string) => void;
}

export function ShareModal({ study, isOpen, onClose, onToast }: ShareModalProps) {
  const [copied, setCopied] = useState(false);
  const [expiry, setExpiry] = useState('24h');

  if (!study) return null;

  const shareUrl = `https://paxs.local/share/${study.study_uid}?exp=${expiry}`;

  const handleCopy = () => {
    navigator.clipboard.writeText(shareUrl);
    setCopied(true);
    onToast('Link copied to clipboard');
    setTimeout(() => setCopied(false), 2000);
  };

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
            {/* Fake QR Code */}
            <div className="w-32 h-32 bg-white p-2 rounded-lg flex flex-wrap gap-1 items-center justify-center relative overflow-hidden">
                <div className="absolute inset-0 grid grid-cols-5 grid-rows-5 gap-0.5 p-1 opacity-80">
                    {Array.from({length: 25}).map((_, i) => (
                        <div key={i} className={`bg-black ${Math.random() > 0.5 ? 'opacity-100' : 'opacity-0'}`}></div>
                    ))}
                </div>
            </div>
            <p className="text-xs text-slate-500 mt-4 text-center">Scan to open in zero-footprint viewer</p>
          </div>

          <div className="space-y-2">
            <label className="text-sm text-slate-400">Expiration</label>
            <Select value={expiry} onValueChange={setExpiry}>
              <SelectTrigger className="bg-[#070c18] border-slate-700 text-white">
                <SelectValue />
              </SelectTrigger>
              <SelectContent className="bg-[#0c1424] border-slate-700 text-slate-200">
                <SelectItem value="1h">1 Hour</SelectItem>
                <SelectItem value="24h">24 Hours</SelectItem>
                <SelectItem value="7d">7 Days</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-2">
            <label className="text-sm text-slate-400">Secure Link</label>
            <div className="flex gap-2">
              <input 
                type="text" 
                readOnly 
                value={shareUrl} 
                className="flex-1 bg-[#070c18] border border-slate-700 rounded-md px-3 text-sm font-mono text-slate-300"
              />
              <Button onClick={handleCopy} className="bg-indigo-600 hover:bg-indigo-500 text-white w-12">
                {copied ? <Check className="w-4 h-4" /> : <Copy className="w-4 h-4" />}
              </Button>
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
