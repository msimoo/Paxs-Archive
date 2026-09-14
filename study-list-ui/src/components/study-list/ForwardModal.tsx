import type { Study } from '@/types/study';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Forward, Server } from 'lucide-react';
import { useState } from 'react';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';

interface ForwardModalProps {
  study: Study | null;
  isOpen: boolean;
  onClose: () => void;
  onToast: (msg: string) => void;
}

export function ForwardModal({ study, isOpen, onClose, onToast }: ForwardModalProps) {
  const [node, setNode] = useState('ARCHIVE_A');
  const [syntax, setSyntax] = useState('implicit');
  const [loading, setLoading] = useState(false);

  if (!study) return null;

  const handleForward = () => {
    setLoading(true);
    setTimeout(() => {
      setLoading(false);
      onToast(`Successfully forwarded study to ${node}`);
      onClose();
    }, 1500);
  };

  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && onClose()}>
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
            <Select value={node} onValueChange={setNode}>
              <SelectTrigger className="bg-[#070c18] border-slate-700 text-white">
                <SelectValue />
              </SelectTrigger>
              <SelectContent className="bg-[#0c1424] border-slate-700 text-slate-200">
                <SelectItem value="ARCHIVE_A">ARCHIVE_A (10.0.0.5:104)</SelectItem>
                <SelectItem value="ORTHANC_REMOTE">ORTHANC_REMOTE (192.168.1.50:4242)</SelectItem>
                <SelectItem value="PACS_BACKUP">PACS_BACKUP (10.0.0.9:11112)</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-2">
            <label className="text-sm text-slate-400">Transfer Syntax</label>
            <Select value={syntax} onValueChange={setSyntax}>
              <SelectTrigger className="bg-[#070c18] border-slate-700 text-white">
                <SelectValue />
              </SelectTrigger>
              <SelectContent className="bg-[#0c1424] border-slate-700 text-slate-200">
                <SelectItem value="implicit">Implicit VR Little Endian</SelectItem>
                <SelectItem value="explicit">Explicit VR Little Endian</SelectItem>
                <SelectItem value="jpeg_lossless">JPEG Lossless</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose} className="border-slate-700 text-slate-300 hover:bg-slate-800">
            Cancel
          </Button>
          <Button onClick={handleForward} disabled={loading} className="bg-teal-600 hover:bg-teal-500 text-white">
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
