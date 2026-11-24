import { useState, useEffect, useCallback } from 'react';
import { useStudies } from '@/hooks/use-studies';
import { StatusChips } from './StatusChips';
import { SearchToolbar } from './SearchToolbar';
import { StudyTable } from './StudyTable';
import { Pagination } from './Pagination';
import { VitalsStrip } from './VitalsStrip';
import { StudyDetailDrawer } from './StudyDetailDrawer';
import { ShareModal } from './ShareModal';
import { ForwardModal } from './ForwardModal';
import { DeleteModal } from './DeleteModal';
import { BurnStudyModal } from './BurnStudyModal';
import { PaxsFooter } from '../common/PaxsFooter';
import { ThemeToggle } from '../common/ThemeToggle';
import { useTheme } from '@/hooks/use-theme';
import {
  apiFetch,
  appBasePath,
  canDeleteStudies,
  clearAuthToken,
  displayPatientName,
  fetchCurrentUser,
  getMediaExporterSession,
  openMediaExporterLauncher,
  type ExporterSessionStatus,
} from '@/lib/api';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import {
  IconGrid,
  IconNodes,
  IconReport,
  IconSchedule,
  IconStudies,
  IconTable,
  IconUpload,
} from '@/components/icons/PaxsIcons';
import { LogOut } from 'lucide-react';
import type { Study } from '@/types/study';

interface CurrentUser {
  username: string;
  full_name?: string | null;
  role: string;
}

interface StudyListPageProps {
  onOpenViewer?: () => void;
}

function isCurrentUser(value: unknown): value is CurrentUser {
  if (typeof value !== 'object' || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return typeof candidate.username === 'string' && typeof candidate.role === 'string';
}

export function StudyListPage(_props: StudyListPageProps) {
  const { theme } = useTheme();
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
    setDateRange,
    removeStudy,
    refresh,
  } = useStudies();

  const [viewMode, setViewMode] = useState<'table' | 'grid'>('table');
  const [selectedStudy, setSelectedStudy] = useState<Study | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);
  const [forwardOpen, setForwardOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [burnStudy, setBurnStudy] = useState<Study | null>(null);
  const [exporterTracker, setExporterTracker] = useState<{
    sessionId: string;
    studyLabel: string;
    status: ExporterSessionStatus | null;
  } | null>(null);
  const [toastMessage, setToastMessage] = useState<string | null>(null);
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);

  // Delete is administrator-only. The role loads asynchronously, so this fails
  // closed: the control is not rendered until an ADMIN identity is known.
  const canDelete = canDeleteStudies(currentUser?.role);

  const showToast = useCallback((msg: string) => {
    setToastMessage(msg);
    window.setTimeout(() => setToastMessage(null), 3000);
  }, []);

  useEffect(() => {
    let active = true;
    void fetchCurrentUser()
      .then((data) => {
        if (!isCurrentUser(data)) throw new Error('The signed-in user response was invalid.');
        if (active) setCurrentUser(data);
      })
      .catch((error: unknown) => {
        if (active && !(error instanceof Error && error.name === 'AbortError')) {
          showToast(error instanceof Error ? error.message : 'Could not load the signed-in user.');
        }
      });
    return () => { active = false; };
  }, [showToast]);

  const handleLogout = async () => {
    try {
      const response = await apiFetch(`${appBasePath()}/api/v1/auth/logout`, { method: 'POST' });
      if (!response.ok) throw new Error(`Sign out failed (${response.status}).`);
    } catch (error: unknown) {
      showToast(error instanceof Error ? error.message : 'Sign out failed.');
    } finally {
      clearAuthToken();
      window.location.assign(`${appBasePath()}/login`);
    }
  };

  const handleStudyClick = (study: Study) => {
    setSelectedStudy(study);
    setDrawerOpen(true);
  };

  // Poll live progress from standalone PAXS-MediaExporter.exe until verified or failed.
  useEffect(() => {
    if (!exporterTracker) return;
    const phase = exporterTracker.status?.phase;
    if (phase === 'verified' || phase === 'failed') return;

    const controller = new AbortController();
    let timer = 0;

    async function poll() {
      if (!exporterTracker) return;
      try {
        const next = await getMediaExporterSession(exporterTracker.sessionId, controller.signal);
        if (controller.signal.aborted) return;
        setExporterTracker((prev) =>
          prev && prev.sessionId === exporterTracker.sessionId ? { ...prev, status: next } : prev,
        );
        if (next.phase === 'verified' || next.phase === 'failed') return;
      } catch {
        if (controller.signal.aborted) return;
      }
      timer = window.setTimeout(poll, 900);
    }

    timer = window.setTimeout(poll, 700);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [exporterTracker]);

  const launchStandaloneExporter = async (study: Study, mode: 'media' | 'dicom-zip') => {
    const studyLabel = displayPatientName(study.patient_name) || study.patient_id || study.study_uid;
    try {
      const res = await openMediaExporterLauncher(study.study_uid, mode);
      if (res.launched) {
        setExporterTracker({
          sessionId: res.session_id,
          studyLabel,
          status: {
            session_id: res.session_id,
            study_uid: study.study_uid,
            mode,
            phase: 'launching',
            percent: 0,
            message: 'PAXS Media Exporter Studio opened — waiting for destination selection…',
            target_label: '',
            output_path: null,
            files_done: 0,
            files_total: study.n_instances || 0,
            bytes_written: 0,
            speed_mbps: 0,
            microdicom_verified: false,
            error: null,
          },
        });
        showToast(
          mode === 'media'
            ? 'Launched PAXS Media Exporter Studio (CD/DVD/USB + MicroDicom 2026.5).'
            : 'Launched PAXS Media Exporter Studio — choose your export directory.',
        );
        return;
      }
    } catch {
      // Fall back cleanly to built-in web modal / direct browser download below.
    }

    if (mode === 'media') {
      setBurnStudy(study);
    } else {
      const href = `${appBasePath()}/api/v1/export/dicomdir/client/${encodeURIComponent(study.study_uid)}/dicom-zip`;
      window.location.assign(href);
    }
  };

  const handleActionClick = (action: 'share' | 'forward' | 'delete' | 'burn' | 'export-zip', study: Study) => {
    if (action === 'burn') {
      void launchStandaloneExporter(study, 'media');
      return;
    }
    if (action === 'export-zip') {
      void launchStandaloneExporter(study, 'dicom-zip');
      return;
    }
    // The delete control is only rendered for an ADMIN, but a stale role (or a
    // keyboard/programmatic dispatch) must not reach the modal either.
    if (action === 'delete' && !canDelete) {
      showToast('Only an administrator can delete studies.');
      return;
    }
    setSelectedStudy(study);
    if (action === 'share') setShareOpen(true);
    if (action === 'forward') setForwardOpen(true);
    if (action === 'delete') setDeleteOpen(true);
  };

  const handleDeleteConfirm = (uid: string) => {
    removeStudy(uid);
    setSelectedStudy((current) => current?.study_uid === uid ? null : current);
    setDrawerOpen(false);
    void refresh();
    showToast(`Study ${uid} deleted successfully.`);
  };

  return (
    /* A workstation, not a document: on a desktop viewport the page itself never
       scrolls — the table does, and the header, command bar and pagination stay
       put. Below `lg` the shell relaxes back to normal document flow so a phone
       can still scroll the whole page. */
    <div className="relative flex min-h-[100dvh] flex-col overflow-x-hidden bg-ink text-foreground lg:h-[100dvh] lg:overflow-hidden">
      {/* The room: auroras + instrument grid, purely decorative. */}
      <div className="room-backdrop" aria-hidden="true" />

      {exporterTracker && exporterTracker.status && (
        <div
          role="status"
          aria-live="polite"
          className="animate-slide-up fixed bottom-5 right-5 z-50 w-[360px] max-w-[calc(100vw-2rem)] rounded-2xl border border-accent/40 bg-panel/95 p-3.5 text-foreground shadow-[var(--paxs-shadow)] backdrop-blur-xl"
        >
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <div className="flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wider text-accent">
                <span
                  className={cn(
                    'h-2 w-2 rounded-full',
                    exporterTracker.status.phase === 'verified'
                      ? 'bg-ok'
                      : exporterTracker.status.phase === 'failed'
                        ? 'bg-danger'
                        : 'animate-pulse bg-accent',
                  )}
                />
                PAXS Media Exporter · {exporterTracker.status.phase}
              </div>
              <div className="mt-0.5 truncate text-[13px] font-semibold">{exporterTracker.studyLabel}</div>
            </div>
            <button
              type="button"
              aria-label="Dismiss export tracker"
              onClick={() => setExporterTracker(null)}
              className="rounded-md px-1.5 py-0.5 text-xs text-muted hover:bg-raised-2 hover:text-foreground"
            >
              ✕
            </button>
          </div>
          <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-raised-2">
            <div
              className={cn(
                'h-full transition-all duration-300',
                exporterTracker.status.phase === 'verified'
                  ? 'bg-ok'
                  : exporterTracker.status.phase === 'failed'
                    ? 'bg-danger'
                    : 'bg-accent',
              )}
              style={{ width: `${Math.max(4, Math.min(100, exporterTracker.status.percent))}%` }}
            />
          </div>
          <p className="mt-1.5 text-[11.5px] text-muted">
            {exporterTracker.status.error || exporterTracker.status.message}
          </p>
          <div className="mt-1.5 flex items-center justify-between font-mono text-[10.5px] text-faint">
            <span>
              {exporterTracker.status.percent}% · {exporterTracker.status.files_done}/{exporterTracker.status.files_total} files
            </span>
            <span>
              {exporterTracker.status.microdicom_verified
                ? '✓ MicroDicom 2026.5 Ready'
                : exporterTracker.status.speed_mbps > 0
                  ? `${exporterTracker.status.speed_mbps.toFixed(1)} MB/s`
                  : exporterTracker.status.target_label || ''}
            </span>
          </div>
        </div>
      )}

      {toastMessage && !exporterTracker && (
        <div
          role="status"
          className="animate-slide-up fixed bottom-5 right-5 z-50 flex items-center gap-2 rounded-xl border border-ok/40 bg-panel px-4 py-3 text-sm font-medium text-foreground shadow-[var(--paxs-shadow)]"
        >
          <span className="h-1.5 w-1.5 rounded-full bg-ok" aria-hidden="true" />
          {toastMessage}
        </div>
      )}

      {/* ── Top bar: brand · title · vitals · session ─────────────── */}
      <header className="sticky top-0 z-40 shrink-0 border-b border-hairline bg-ink/80 backdrop-blur-xl">
        <div className="mx-auto flex h-[52px] max-w-[1680px] items-center gap-2.5 px-3 sm:px-6 lg:px-8">
          <img
            src={theme === 'light' ? '/static/logo/dark-viewer.png' : '/static/logo/light-viewer.png'}
            alt="PAXS Archive logo"
            title="PAXS Archive · Study list"
            data-paxs-theme-logo="1"
            className="h-8 w-[84px] shrink-0 object-contain"
          />

          <span aria-hidden="true" className="hidden h-6 w-px shrink-0 bg-hairline-strong sm:block" />

          <div className="flex shrink-0 items-center gap-2">
            <span className="grid size-7 place-items-center rounded-lg border border-accent/25 bg-accent/10">
              <IconStudies className="size-4" />
            </span>
            <h1 className="text-[15px] font-bold tracking-tight sm:text-base">Patient studies</h1>
            {loading && (
              <span role="status" className="relative flex size-2" aria-label="Refreshing studies">
                <span className="absolute inline-flex size-full animate-ping rounded-full bg-accent opacity-70" />
                <span className="relative inline-flex size-2 rounded-full bg-accent" />
              </span>
            )}
          </div>

          <VitalsStrip counts={counts} className="ml-1 hidden xl:flex" />

          {/* One right-aligned cluster: the listener heartbeat, then the session
              controls. Both margins must not be `auto`, or free space splits
              between them and the pill drifts away from the buttons. */}
          <div className="ml-auto flex shrink-0 items-center gap-2">
            {/* Listener heartbeat — the one fact that is about the service, not the data. */}
            <span
              title="DICOM DIMSE listener: C-STORE · C-FIND · C-MOVE"
              className="hidden shrink-0 items-center gap-1.5 rounded-full border border-ok/35 bg-ok/[0.08] py-[3px] pl-2 pr-2.5 xl:inline-flex"
            >
              <span className="relative flex size-1.5" aria-hidden="true">
                <span className="absolute inline-flex size-full animate-ping rounded-full bg-ok opacity-70" />
                <span className="relative inline-flex size-1.5 rounded-full bg-ok" />
              </span>
              <span className="font-mono text-[11px] font-bold leading-none text-ok">:10400</span>
              <span className="text-[9px] font-semibold uppercase leading-none tracking-[0.13em] text-faint">dicom live</span>
            </span>

            <ThemeToggle />

            <div
              className="hidden items-center gap-2 rounded-full border border-hairline bg-panel/60 py-1 pl-1 pr-3 sm:flex"
              data-paxs-react-user-chip="1"
            >
              <span className="grid h-6 w-6 place-items-center rounded-full bg-gradient-to-tr from-accent to-teal text-[10px] font-black text-[#04121f]">
                {(currentUser?.full_name || currentUser?.username || '?').charAt(0).toUpperCase()}
              </span>
              <span className="flex max-w-[150px] flex-col leading-none">
                <span className="truncate text-[11.5px] font-semibold text-foreground">
                  {currentUser ? (currentUser.full_name || currentUser.username) : 'Signing in…'}
                </span>
                <span className="mt-0.5 font-mono text-[8.5px] font-bold uppercase tracking-[0.14em] text-accent">
                  {currentUser?.role ?? '—'}
                </span>
              </span>
            </div>

            <Button
              onClick={handleLogout}
              variant="ghost"
              size="sm"
              title="Sign out of your account"
              aria-label="Sign out"
              className="h-8 gap-1.5 px-2.5 text-[12px] text-danger hover:bg-danger/10 hover:text-danger"
            >
              <LogOut className="h-3.5 w-3.5" aria-hidden="true" />
              <span className="hidden sm:inline">Sign out</span>
            </Button>
          </div>
        </div>
      </header>

      <main className="relative z-10 mx-auto flex w-full min-h-0 max-w-[1680px] flex-1 flex-col gap-2 px-3 py-2 sm:px-6 lg:px-8">

        {/* ── Command bar: search · filters · view · verbs ──────────── */}
        <div className="flex flex-wrap items-center gap-2">
          <div className="min-w-[240px] flex-1">
            <SearchToolbar
              filters={filters}
              setSearch={setSearch}
              setModality={setModality}
              setDateRange={setDateRange}
            />
          </div>

          <div className="flex shrink-0 items-center gap-1.5">
            <div
              role="group"
              aria-label="View mode"
              className="flex items-center gap-0.5 rounded-lg border border-hairline bg-panel/60 p-0.5"
            >
              {([
                { mode: 'table' as const, icon: IconTable, label: 'Table view' },
                { mode: 'grid' as const, icon: IconGrid, label: 'Grid view' },
              ]).map(({ mode, icon: Icon, label }) => (
                <button
                  key={mode}
                  type="button"
                  aria-label={label}
                  aria-pressed={viewMode === mode}
                  onClick={() => setViewMode(mode)}
                  className={cn(
                    'focus-ring grid h-8 w-8 place-items-center rounded-md transition-colors',
                    viewMode === mode
                      ? 'bg-raised-2 shadow-[inset_0_1px_0_var(--paxs-sheen)]'
                      : 'opacity-55 hover:bg-raised/60 hover:opacity-100',
                  )}
                >
                  <Icon className="size-4" />
                </button>
              ))}
            </div>

            <span aria-hidden="true" className="mx-0.5 hidden h-5 w-px bg-hairline-strong sm:block" />

            <div className="flex items-center gap-1.5">
              <Button
                onClick={() => {
                  // H16: bound to the explicit selection — never to whatever
                  // happens to be first in the (sortable) list.
                  if (selectedStudy) {
                    window.paxsOpenReportModal?.(selectedStudy.study_uid);
                  }
                }}
                disabled={!selectedStudy}
                title={
                  selectedStudy
                    ? `Open report for ${displayPatientName(selectedStudy.patient_name)} — ${selectedStudy.study_uid}`
                    : 'Select a study to open/create report'
                }
                size="sm"
                variant="outline"
                className="h-9 gap-1.5 px-2.5"
              >
                <IconReport className="size-4" />
                <span className="hidden 2xl:inline">Report</span>
              </Button>
              <Button
                onClick={() => window.location.href = '/#nodes'}
                variant="outline"
                size="sm"
                title="DICOM nodes"
                className="h-9 gap-1.5 px-2.5"
              >
                <IconNodes className="size-4" />
                <span className="hidden 2xl:inline">Nodes</span>
              </Button>
              <Button
                onClick={() => window.location.href = '/#workflow'}
                variant="outline"
                size="sm"
                title="Modality worklist"
                className="h-9 gap-1.5 px-2.5"
              >
                <IconSchedule className="size-4" />
                <span className="hidden 2xl:inline">Schedule</span>
              </Button>
              <Button
                onClick={() => window.location.href = '/#studies'}
                variant="secondary"
                size="sm"
                title="Upload DICOM"
                className="h-9 gap-1.5 px-2.5"
              >
                <IconUpload className="size-4" />
                <span className="hidden 2xl:inline">Upload</span>
              </Button>
            </div>
          </div>
        </div>

        {error && (
          <div
            role="alert"
            className="flex items-center gap-2 rounded-xl border border-danger/40 bg-danger/10 px-4 py-2 text-[13px] text-danger"
          >
            <span className="font-semibold">Error:</span> {error}
          </div>
        )}

        <StatusChips
          filters={filters}
          counts={counts}
          setStatusFilter={setStatusFilter}
          toggleSpecialFilter={toggleSpecialFilter}
        />

        {/* ── The list ────────────────────────────────────────────── */}
        <section className="flex min-h-0 flex-1 flex-col gap-1.5" aria-label="Study list">
          <StudyTable
            studies={studies}
            loading={loading}
            filters={filters}
            setSort={setSort}
            viewMode={viewMode}
            onStudyClick={handleStudyClick}
            onActionClick={handleActionClick}
            canDelete={canDelete}
          />
          <Pagination
            page={filters.page || 1}
            limit={filters.limit || 20}
            total={total}
            setPage={setPage}
          />
        </section>
      </main>

      <PaxsFooter />

      {/* Modals & Drawer */}
      <StudyDetailDrawer
        study={selectedStudy}
        isOpen={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        onAction={handleActionClick}
        onToast={showToast}
        canDelete={canDelete}
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

      {burnStudy && <BurnStudyModal study={burnStudy} onClose={() => setBurnStudy(null)} />}

      <DeleteModal
        study={selectedStudy}
        isOpen={deleteOpen}
        onClose={() => setDeleteOpen(false)}
        onConfirm={handleDeleteConfirm}
      />
    </div>
  );
}
