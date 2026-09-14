import { Activity, Database, HardDrive, ShieldAlert, Server } from 'lucide-react';
import { Card } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import type { StudyCounts } from '@/types/study';

interface KpiHeaderProps {
  counts: StudyCounts;
}

export function KpiHeader({ counts }: KpiHeaderProps) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-5 gap-4 mb-6 z-20 relative">
      <Card className="bg-[#0c1424]/60 border-slate-700/50 backdrop-blur-xl p-4 flex flex-col justify-between hover:bg-[#0c1424]/80 transition-all shadow-[0_4px_20px_rgba(0,0,0,0.2)]">
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">DICOM Server</span>
          <Activity className="w-4 h-4 text-emerald-400" />
        </div>
        <div className="flex items-center gap-2">
          <div className="relative flex h-3 w-3">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
            <span className="relative inline-flex rounded-full h-3 w-3 bg-emerald-500"></span>
          </div>
          <span className="text-sm font-mono text-emerald-300">DIMSE :10400 Listening</span>
        </div>
      </Card>

      <Card className="bg-[#0c1424]/60 border-slate-700/50 backdrop-blur-xl p-4 flex flex-col justify-between hover:bg-[#0c1424]/80 transition-all shadow-[0_4px_20px_rgba(0,0,0,0.2)]">
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Total Studies</span>
          <Database className="w-4 h-4 text-cyan-400" />
        </div>
        <div className="text-2xl font-mono font-bold text-slate-200">
          {counts.total.toLocaleString()}
        </div>
      </Card>

      <Card className="bg-[#0c1424]/60 border-slate-700/50 backdrop-blur-xl p-4 flex flex-col justify-between hover:bg-[#0c1424]/80 transition-all shadow-[0_4px_20px_rgba(0,0,0,0.2)]">
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Online Hot Storage</span>
          <HardDrive className="w-4 h-4 text-teal-400" />
        </div>
        <div className="flex items-center gap-2">
          <span className="text-2xl font-mono font-bold text-slate-200">
            {counts.online.toLocaleString()}
          </span>
          <Badge variant="outline" className="border-teal-700 text-teal-400 bg-teal-950/30">
            {counts.total > 0 ? Math.round((counts.online / counts.total) * 100) : 0}%
          </Badge>
        </div>
      </Card>

      <Card className="bg-[#0c1424]/60 border-slate-700/50 backdrop-blur-xl p-4 flex flex-col justify-between hover:bg-[#0c1424]/80 transition-all shadow-[0_4px_20px_rgba(0,0,0,0.2)]">
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">STAT Triage</span>
          <ShieldAlert className="w-4 h-4 text-amber-500" />
        </div>
        <div className="flex items-center gap-2">
          <span className="text-2xl font-mono font-bold text-slate-200">
            {counts.critical.toLocaleString()}
          </span>
          {counts.critical > 0 && (
            <Badge variant="destructive" className="bg-amber-600 hover:bg-amber-600 shadow-[0_0_10px_rgba(217,119,6,0.5)]">
              Critical Unread
            </Badge>
          )}
        </div>
      </Card>

      <Card className="bg-[#0c1424]/60 border-slate-700/50 backdrop-blur-xl p-4 flex flex-col justify-between hover:bg-[#0c1424]/80 transition-all shadow-[0_4px_20px_rgba(0,0,0,0.2)]">
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">VNA Tenant</span>
          <Server className="w-4 h-4 text-indigo-400" />
        </div>
        <div className="flex items-center h-full">
          <Badge variant="outline" className="border-indigo-700 text-indigo-400 bg-indigo-950/30 py-1">
            Multi-Tenant Mode Active
          </Badge>
        </div>
      </Card>
    </div>
  );
}
