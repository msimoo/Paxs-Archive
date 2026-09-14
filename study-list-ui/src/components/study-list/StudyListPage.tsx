import { useState, useEffect } from 'react';
import { useStudies } from '@/hooks/use-studies';
import { StatusChips } from './StatusChips';
import { SearchToolbar } from './SearchToolbar';
import { StudyTable } from './StudyTable';
import { Pagination } from './Pagination';
import { KpiHeader } from './KpiHeader';
import { StudyDetailDrawer } from './StudyDetailDrawer';
import { ShareModal } from './ShareModal';
import { ForwardModal } from './ForwardModal';
import { DeleteModal } from './DeleteModal';
import { PaxsFooter } from '../common/PaxsFooter';
import { Button } from '@/components/ui/button';
import { Upload, Calendar, Network, LayoutGrid, List, Eye, FileText, LogOut } from 'lucide-react';
import type { Study } from '@/types/study';

interface CurrentUser {
  username: string;
  full_name?: string | null;
  role: string;
}

interface StudyListPageProps {
  onOpenViewer?: () => void;
}

export function StudyListPage({ onOpenViewer }: StudyListPageProps) {
  const {
    studies,
    total,
    counts,
    loading,
    error,
    filters,
    setSearch,
    setModality,
    setSort,
    setPage,
    setStatusFilter,
    toggleSpecialFilter,
    setDateRange
  } = useStudies();

  const [viewMode, setViewMode] = useState<'table' | 'grid'>('table');
  const [selectedStudy, setSelectedStudy] = useState<Study | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);
  const [forwardOpen, setForwardOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [toastMessage, setToastMessage] = useState<string | null>(null);
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);

  useEffect(() => {
    const token = localStorage.getItem('paxs_token');
    const headers: Record<string, string> = {};
    if (token) headers['Authorization'] = `Bearer ${token}`;

    fetch('/api/v1/auth/me', { headers, credentials: 'same-origin' })
      .then(res => res.ok ? res.json() : null)
      .then(data => {
        if (data) setCurrentUser(data);
      })
      .catch(() => {});
  }, []);

  const handleLogout = async () => {
    try {
      await fetch('/api/v1/auth/logout', { method: 'POST', credentials: 'same-origin' });
    } catch (err) {
      console.error('Logout error:', err);
    } finally {
      try {
        localStorage.removeItem('paxs_token');
      } catch (_) {}
      window.location.href = '/login';
    }
  };

  const handleStudyClick = (study: Study) => {
    setSelectedStudy(study);
    setDrawerOpen(true);
  };

  const handleActionClick = (action: 'share' | 'forward' | 'delete', study: Study) => {
    setSelectedStudy(study);
    if (action === 'share') setShareOpen(true);
    if (action === 'forward') setForwardOpen(true);
    if (action === 'delete') setDeleteOpen(true);
  };

  const showToast = (msg: string) => {
    setToastMessage(msg);
    setTimeout(() => setToastMessage(null), 3000);
  };

  const handleDeleteConfirm = (uid: string) => {
    showToast(`Study ${uid} deleted successfully.`);
  };

  return (
    <div className="min-h-screen bg-[#070c18] text-slate-200 font-sans relative overflow-hidden selection:bg-cyan-500/30">
      <div className="absolute top-[-20%] right-[-10%] w-[50%] h-[50%] rounded-full bg-cyan-900/20 blur-[120px] pointer-events-none" />
      <div className="absolute bottom-[-20%] left-[-10%] w-[40%] h-[40%] rounded-full bg-teal-900/10 blur-[120px] pointer-events-none" />

      {toastMessage && (
        <div className="fixed bottom-4 right-4 bg-emerald-950/90 border border-emerald-500/50 text-emerald-200 px-4 py-3 rounded-lg shadow-lg z-50 animate-in slide-in-from-bottom-5">
          {toastMessage}
        </div>
      )}

      <main className="relative z-10 max-w-[1600px] mx-auto p-6 md:p-8 flex flex-col gap-6 min-h-screen">
        
        {/* Header */}
        <header className="flex flex-col md:flex-row md:items-end justify-between gap-4 pb-2 border-b border-slate-800/60">
          <div>
            <h1 className="text-3xl font-bold tracking-tight text-white flex items-center gap-3">
              Patient Studies
              {loading && <span className="flex h-3 w-3 relative"><span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-cyan-400 opacity-75"></span><span className="relative inline-flex rounded-full h-3 w-3 bg-cyan-500"></span></span>}
            </h1>
            <p className="text-slate-500 mt-1 text-sm">Manage and review radiological examinations</p>
          </div>
          <div className="flex flex-wrap items-center gap-3" data-paxs-react-user-chip="1">
            <Button
              onClick={() => {
                const firstUid = studies[0]?.study_uid;
                if (firstUid) (window as any).paxsOpenReportModal?.(firstUid);
                else alert('No studies available to report.');
              }}
              className="bg-gradient-to-r from-emerald-600 to-teal-500 text-white font-bold hover:brightness-110 shadow-[0_0_15px_rgba(16,185,129,0.3)] transition-all cursor-pointer"
            >
              <FileText className="w-4 h-4 mr-2" />
              📝 Diagnostic Report
            </Button>
            <Button 
              onClick={() => window.location.href = '/#nodes'}
              variant="ghost" 
              className="text-slate-400 hover:text-cyan-400 hover:bg-cyan-950/30 border border-slate-800 cursor-pointer"
            >
              <Network className="w-4 h-4 mr-2" />
              Nodes
            </Button>
            <Button 
              onClick={() => window.location.href = '/#workflow'}
              variant="outline" 
              className="bg-[#0c1424] border-slate-700 text-slate-300 hover:text-white hover:bg-slate-800 cursor-pointer"
            >
              <Calendar className="w-4 h-4 mr-2" />
              Schedule
            </Button>
            <Button 
              onClick={() => window.location.href = '/#studies'}
              className="bg-cyan-600 hover:bg-cyan-500 text-white shadow-[0_0_15px_rgba(34,211,238,0.3)] cursor-pointer"
            >
              <Upload className="w-4 h-4 mr-2" />
              Upload DICOM
            </Button>

            {/* User Profile Info & Logout */}
            <div className="flex items-center gap-2 pl-2 border-l border-slate-800/80">
              {currentUser ? (
                <div className="flex items-center gap-2 px-3 py-1.5 bg-[#0c1424]/90 border border-slate-700/60 rounded-full shadow-inner">
                  <div className="w-6 h-6 rounded-full bg-gradient-to-tr from-cyan-600 to-teal-400 text-[#04121f] font-black text-xs flex items-center justify-center shadow-sm">
                    {(currentUser.full_name || currentUser.username || '?').charAt(0).toUpperCase()}
                  </div>
                  <div className="flex flex-col leading-none">
                    <span className="text-xs font-bold text-slate-200 truncate max-w-[120px]">
                      {currentUser.full_name || currentUser.username}
                    </span>
                    <span className="text-[9px] font-extrabold text-cyan-400 uppercase tracking-wider">
                      {currentUser.role}
                    </span>
                  </div>
                </div>
              ) : null}

              <Button
                onClick={handleLogout}
                variant="outline"
                size="sm"
                className="bg-rose-950/20 border-rose-800/40 text-rose-300 hover:text-white hover:bg-rose-600/90 hover:border-rose-500 transition-all font-semibold text-xs px-3 shadow-sm cursor-pointer"
                title="Sign out of your account"
              >
                <LogOut className="w-3.5 h-3.5 mr-1.5" />
                Logout
              </Button>
            </div>
          </div>
        </header>

        <KpiHeader counts={counts} />

        {error && (
          <div className="bg-rose-950/50 border border-rose-500/50 text-rose-200 px-4 py-3 rounded-lg flex items-center backdrop-blur-sm">
            <span className="font-semibold mr-2">Error:</span> {error}
          </div>
        )}

        {/* Filters & View Toggles */}
        <section className="flex flex-col gap-5 z-20">
          <div className="flex justify-between items-start gap-4">
            <div className="flex-1">
              <StatusChips 
                filters={filters} 
                counts={counts} 
                setStatusFilter={setStatusFilter} 
                toggleSpecialFilter={toggleSpecialFilter} 
              />
            </div>
            <div className="flex bg-[#0c1424] p-1 rounded-lg border border-slate-700/50">
              <button 
                onClick={() => setViewMode('table')} 
                className={`p-1.5 rounded-md transition-colors ${viewMode === 'table' ? 'bg-slate-800 text-white' : 'text-slate-500 hover:text-slate-300'}`}
              >
                <List className="w-4 h-4" />
              </button>
              <button 
                onClick={() => setViewMode('grid')} 
                className={`p-1.5 rounded-md transition-colors ${viewMode === 'grid' ? 'bg-slate-800 text-white' : 'text-slate-500 hover:text-slate-300'}`}
              >
                <LayoutGrid className="w-4 h-4" />
              </button>
            </div>
          </div>
          <SearchToolbar 
            filters={filters} 
            setSearch={setSearch} 
            setModality={setModality} 
            setDateRange={setDateRange} 
          />
        </section>

        {/* Table Area */}
        <section className="flex-1 min-h-0 flex flex-col gap-4 relative z-10">
          <StudyTable 
            studies={studies} 
            loading={loading} 
            filters={filters} 
            setSort={setSort}
            viewMode={viewMode}
            onStudyClick={handleStudyClick}
            onActionClick={handleActionClick}
          />
          <Pagination 
            page={filters.page || 1} 
            limit={filters.limit || 20} 
            total={total} 
            setPage={setPage} 
          />
        </section>
      </main>

      {/* PAXS Archive Site Footer */}
      <PaxsFooter />

      {/* Modals & Drawers */}
      <StudyDetailDrawer 
        study={selectedStudy} 
        isOpen={drawerOpen} 
        onClose={() => setDrawerOpen(false)} 
        onAction={handleActionClick}
        onToast={showToast}
      />
      
      <ShareModal 
        study={selectedStudy} 
        isOpen={shareOpen} 
        onClose={() => setShareOpen(false)}
        onToast={showToast}
      />

      <ForwardModal 
        study={selectedStudy} 
        isOpen={forwardOpen} 
        onClose={() => setForwardOpen(false)}
        onToast={showToast}
      />

      <DeleteModal 
        study={selectedStudy} 
        isOpen={deleteOpen} 
        onClose={() => setDeleteOpen(false)}
        onConfirm={handleDeleteConfirm}
      />
    </div>
  );
}
