import { useCallback, useEffect, useState } from 'react';
import {
  CheckCircle2,
  Disc3,
  Download,
  FolderOpen,
  HardDrive,
  Info,
  Laptop,
  Loader2,
  RefreshCw,
  Server,
  Usb,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import {
  ApiError,
  checkServerDiscMedia,
  displayPatientName,
  fetchServerDevices,
  getDiscBurn,
  startDiscBurn,
  startServerUsbExport,
  type DiscBurnStatus,
  type OpticalRecorder,
  type ServerMediaDevices,
  type UsbDrive,
} from '@/lib/api';
import {
  downloadStudyMedia,
  formatBytes,
  getDesktopAuthContext,
  getDesktopMediaBridge,
  safeStudyFolderName,
  supportsDirectUsbWrite,
  writeStudyDirectToUsbDirectory,
  type LocalMediaInventory,
  type LocalMediaJobStatus,
} from '@/lib/clientMedia';
import { cn } from '@/lib/utils';
import type { Study } from '@/types/study';

type ExportSourceTab = 'client' | 'server';

interface SelectedTarget {
  kind: 'disc' | 'usb';
  drive: string;
  label: string;
}

const PHASE_LABELS: Record<string, string> = {
  queued: 'Checking drive and media…',
  downloading: 'Downloading study package from archive…',
  preparing: 'Preparing DICOMDIR & DICOM files…',
  writing: 'Writing files to media…',
  verifying: 'Reading back and verifying SHA-256 checksums…',
};

function makeJobId(): string {
  return Array.from(
    crypto.getRandomValues(new Uint8Array(16)),
    (byte) => byte.toString(16).padStart(2, '0'),
  ).join('');
}

function DriveCard({
  kind,
  drive,
  title,
  subtitle,
  selected,
  disabled,
  onSelect,
}: {
  kind: 'disc' | 'usb';
  drive: string;
  title: string;
  subtitle: string;
  selected: boolean;
  disabled?: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onSelect}
      className={cn(
        'focus-ring flex w-full items-center gap-3 rounded-xl border p-3 text-left transition-all',
        selected
          ? 'border-accent bg-accent/10 shadow-[0_0_0_1px_var(--paxs-accent)]'
          : 'border-hairline bg-ink/50 hover:border-hairline-strong hover:bg-raised/60',
        disabled && 'cursor-not-allowed opacity-55',
      )}
    >
      <span
        className={cn(
          'grid size-9 shrink-0 place-items-center rounded-lg border',
          kind === 'disc'
            ? 'border-accent/35 bg-accent/10 text-accent'
            : 'border-ok/35 bg-ok/10 text-ok',
        )}
      >
        {kind === 'disc' ? <Disc3 className="size-4" aria-hidden="true" /> : <Usb className="size-4" aria-hidden="true" />}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="rounded bg-raised-2 px-1.5 py-0.5 font-mono text-[11px] font-bold text-foreground">
            {drive}
          </span>
          <span className="truncate text-[13px] font-semibold text-foreground">{title}</span>
        </div>
        <p className="mt-0.5 truncate font-mono text-[11px] text-muted">{subtitle}</p>
      </div>
      <span
        aria-hidden="true"
        className={cn(
          'size-4 shrink-0 rounded-full border-2 transition-colors',
          selected ? 'border-accent bg-accent' : 'border-hairline-strong',
        )}
      />
    </button>
  );
}

export function BurnStudyModal({ study, onClose }: { study: Study; onClose: () => void }) {
  const [trigger] = useState(() => document.activeElement);
  const desktopBridge = getDesktopMediaBridge();
  const canDirectBrowserUsb = supportsDirectUsbWrite();

  const [tab, setTab] = useState<ExportSourceTab>('client');

  // Device inventories
  const [localDevices, setLocalDevices] = useState<LocalMediaInventory | null>(null);
  const [localLoading, setLocalLoading] = useState(false);
  const [localTarget, setLocalTarget] = useState<SelectedTarget | null>(null);
  const [localDiscInfo, setLocalDiscInfo] = useState<string>('');

  const [serverDevices, setServerDevices] = useState<ServerMediaDevices | null>(null);
  const [serverLoading, setServerLoading] = useState(false);
  const [serverTarget, setServerTarget] = useState<SelectedTarget | null>(null);
  const [serverDiscInfo, setServerDiscInfo] = useState<string>('');

  // Active export job (server or desktop)
  const [jobScope, setJobScope] = useState<'server' | 'desktop' | null>(null);
  const [jobId, setJobId] = useState('');
  const [status, setStatus] = useState<DiscBurnStatus | LocalMediaJobStatus | null>(null);
  const [busyMessage, setBusyMessage] = useState('');
  const [successMessage, setSuccessMessage] = useState('');
  const [error, setError] = useState('');

  const phase = status?.phase;
  const tracking = Boolean(jobScope && jobId && phase && phase !== 'failed' && phase !== 'verified');
  const busy = Boolean(busyMessage) || tracking;
  const done = phase === 'verified' || Boolean(successMessage);

  const pickDefaultTarget = (
    recorders: OpticalRecorder[],
    usbDrives: UsbDrive[],
    preferredDisc?: string,
  ): SelectedTarget | null => {
    if (usbDrives.length > 0) {
      return { kind: 'usb', drive: usbDrives[0].drive, label: usbDrives[0].label };
    }
    if (recorders.length > 0) {
      const match = preferredDisc
        ? recorders.find((r) => r.drive.toUpperCase() === preferredDisc.toUpperCase())
        : undefined;
      const chosen = match || recorders[0];
      return { kind: 'disc', drive: chosen.drive, label: chosen.model };
    }
    return null;
  };

  const refreshLocalDevices = useCallback(async () => {
    if (!desktopBridge) return;
    setLocalLoading(true);
    setLocalDiscInfo('');
    try {
      const inv = await desktopBridge.list_local_media();
      setLocalDevices(inv);
      setLocalTarget((prev) => {
        if (prev) {
          const exists = prev.kind === 'usb'
            ? inv.usb_drives.some((u) => u.drive === prev.drive)
            : inv.recorders.some((r) => r.drive === prev.drive);
          if (exists) return prev;
        }
        return pickDefaultTarget(inv.recorders, inv.usb_drives);
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not scan local drives.');
    } finally {
      setLocalLoading(false);
    }
  }, [desktopBridge]);

  const refreshServerDevices = useCallback(async () => {
    setServerLoading(true);
    setServerDiscInfo('');
    try {
      const inv = await fetchServerDevices();
      setServerDevices(inv);
      setServerTarget((prev) => {
        if (prev) {
          const exists = prev.kind === 'usb'
            ? inv.usb_drives.some((u) => u.drive === prev.drive)
            : inv.recorders.some((r) => r.drive === prev.drive);
          if (exists) return prev;
        }
        return pickDefaultTarget(inv.recorders, inv.usb_drives, inv.default_disc_drive);
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not query archive server drives.');
    } finally {
      setServerLoading(false);
    }
  }, []);

  useEffect(() => {
    if (tab === 'client' && desktopBridge && !localDevices && !localLoading) {
      void refreshLocalDevices();
    } else if (tab === 'server' && !serverDevices && !serverLoading) {
      void refreshServerDevices();
    }
  }, [tab, desktopBridge, localDevices, localLoading, serverDevices, serverLoading, refreshLocalDevices, refreshServerDevices]);

  // Poll server or desktop background export job
  useEffect(() => {
    if (!tracking || !jobScope || !jobId) return;
    const controller = new AbortController();
    let timer = 0;

    async function poll() {
      try {
        const next = jobScope === 'desktop' && desktopBridge
          ? await desktopBridge.get_local_media_job(jobId)
          : await getDiscBurn(jobId, controller.signal);
        if (controller.signal.aborted) return;
        setStatus(next);
        setError('');
        if (next.phase === 'failed' || next.phase === 'verified') return;
      } catch (err) {
        if (controller.signal.aborted) return;
        if (err instanceof ApiError && err.status === 404) {
          setStatus({ phase: 'failed', count: 0, error: 'The server lost this job. Check the drive before retrying.' });
          return;
        }
        setError('Connection interrupted. Rechecking export status…');
      }
      timer = window.setTimeout(poll, 1200);
    }

    timer = window.setTimeout(poll, 1000);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [tracking, jobScope, jobId, desktopBridge]);

  async function handleCheckDisc(scope: 'client' | 'server', drive: string) {
    setError('');
    if (scope === 'client' && desktopBridge) {
      setLocalDiscInfo('Checking inserted disc…');
      try {
        const res = await desktopBridge.check_local_disc(drive);
        if (res.ok && res.media_type) {
          setLocalDiscInfo(`Ready: ${res.media_type} · ${formatBytes(res.available_bytes || 0)} free on ${res.drive || drive}`);
        } else {
          setLocalDiscInfo(res.error || 'Insert a blank writable CD or DVD.');
        }
      } catch (err) {
        setLocalDiscInfo(err instanceof Error ? err.message : 'Could not inspect disc.');
      }
      return;
    }

    setServerDiscInfo('Checking server disc…');
    try {
      const info = await checkServerDiscMedia(drive);
      setServerDiscInfo(`Ready: ${info.media_type} · ${formatBytes(info.available_bytes)} free on ${info.drive}`);
    } catch (err) {
      setServerDiscInfo(err instanceof Error ? err.message : 'Insert a blank writable CD or DVD in the server drive.');
    }
  }

  async function handleStartDesktopExport(mode: 'disc' | 'usb' | 'folder', target: string) {
    if (!desktopBridge) return;
    const id = makeJobId();
    const auth = getDesktopAuthContext();
    setJobScope('desktop');
    setJobId(id);
    setError('');
    setSuccessMessage('');
    setStatus({ phase: 'queued', mode, drive: target, output_path: null, error: null });
    try {
      const next = await desktopBridge.start_local_media_export(
        id,
        study.study_uid,
        mode,
        target,
        safeStudyFolderName(study),
        'PAXS_DICOM',
        auth.originUrl,
        auth.token,
        auth.cookie,
      );
      setStatus(next);
    } catch (err) {
      setStatus(null);
      setJobScope(null);
      setError(err instanceof Error ? err.message : 'Could not start local export.');
    }
  }

  async function handleDesktopPickFolder() {
    if (!desktopBridge) return;
    setError('');
    try {
      const picked = await desktopBridge.pick_local_folder();
      if (picked.ok && picked.path) {
        await handleStartDesktopExport('folder', picked.path);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not open folder picker.');
    }
  }

  async function handleStartServerExport() {
    if (!serverTarget) return;
    const id = makeJobId();
    setJobScope('server');
    setJobId(id);
    setError('');
    setSuccessMessage('');
    setStatus({ phase: 'queued', count: study.n_instances, target_kind: serverTarget.kind, drive: serverTarget.drive, error: null });
    try {
      const next = serverTarget.kind === 'disc'
        ? await startDiscBurn(id, study.study_uid, serverTarget.drive)
        : await startServerUsbExport(id, study.study_uid, serverTarget.drive, safeStudyFolderName(study));
      setStatus(next);
    } catch (err) {
      setStatus(null);
      setJobScope(null);
      setError(err instanceof Error ? err.message : 'Could not start server export.');
    }
  }

  async function handleBrowserDownload(format: 'iso' | 'zip') {
    setError('');
    setSuccessMessage('');
    setStatus(null);
    setBusyMessage(format === 'iso' ? 'Building CD/DVD disc image (.iso)…' : 'Building USB DICOMDIR package (.zip)…');
    try {
      const res = await downloadStudyMedia(study.study_uid, format, study);
      setSuccessMessage(
        format === 'iso'
          ? `Downloaded ${res.filename} (${formatBytes(res.bytes)}). Right-click the ISO file in Explorer and choose "Burn disc image".`
          : `Downloaded ${res.filename} (${formatBytes(res.bytes)}). Extract all files onto your USB drive keeping DICOMDIR at the folder root.`,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Download failed.');
    } finally {
      setBusyMessage('');
    }
  }

  async function handleBrowserDirectUsb() {
    setError('');
    setSuccessMessage('');
    setStatus(null);
    setBusyMessage('Select your USB drive or destination folder…');
    try {
      const res = await writeStudyDirectToUsbDirectory(study.study_uid, study, setBusyMessage);
      setSuccessMessage(`Exported ${res.fileCount} files directly into folder "${res.folderName}" with DICOMDIR.`);
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') {
        // User cancelled the OS folder picker — keep the modal calm.
      } else {
        setError(err instanceof Error ? err.message : 'Direct USB write failed.');
      }
    } finally {
      setBusyMessage('');
    }
  }

  function resetFeedback() {
    setStatus(null);
    setJobScope(null);
    setJobId('');
    setError('');
    setSuccessMessage('');
  }

  return (
    <Dialog open onOpenChange={(open) => { if (!open && !busy) onClose(); }}>
      <DialogContent
        className={cn('sm:max-w-[560px]', busy && '[&>button:last-child]:hidden')}
        onCloseAutoFocus={(event) => {
          if (trigger instanceof HTMLElement && trigger.isConnected) {
            event.preventDefault();
            trigger.focus();
          }
        }}
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Disc3 className="h-5 w-5 text-accent" aria-hidden="true" />
            Export Study to CD/DVD or USB
          </DialogTitle>
          <DialogDescription>
            Create a standard DICOMDIR portable media export from your device or directly on the archive server.
          </DialogDescription>
        </DialogHeader>

        {/* Study summary */}
        <div className="flex items-center justify-between gap-3 rounded-xl border border-hairline bg-ink/70 px-3.5 py-2.5 text-sm">
          <div className="min-w-0">
            <p className="truncate font-semibold text-foreground">{displayPatientName(study.patient_name)}</p>
            <p className="truncate text-xs text-muted">
              {study.study_date} · {study.description || 'DICOM study'}
            </p>
          </div>
          <span className="shrink-0 rounded-full border border-hairline-strong bg-raised px-2.5 py-1 font-mono text-[11px] font-semibold text-accent">
            {study.n_instances} files
          </span>
        </div>

        {/* Source switcher: Client Device vs Archive Server */}
        <div
          role="tablist"
          aria-label="Export device location"
          className="grid grid-cols-2 gap-1.5 rounded-xl border border-hairline bg-ink/60 p-1"
        >
          <button
            type="button"
            role="tab"
            aria-selected={tab === 'client'}
            disabled={busy}
            onClick={() => { setTab('client'); resetFeedback(); }}
            className={cn(
              'focus-ring flex items-center justify-center gap-2 rounded-lg px-3 py-2 text-xs font-semibold transition-all',
              tab === 'client'
                ? 'bg-raised-2 text-foreground shadow-sm'
                : 'text-muted hover:text-foreground',
            )}
          >
            <Laptop className="size-3.5 text-accent" aria-hidden="true" />
            <span>This Device (Client)</span>
            {desktopBridge && (
              <span className="rounded bg-ok/15 px-1.5 py-0.5 font-mono text-[9px] font-bold text-ok">APP</span>
            )}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === 'server'}
            disabled={busy}
            onClick={() => { setTab('server'); resetFeedback(); }}
            className={cn(
              'focus-ring flex items-center justify-center gap-2 rounded-lg px-3 py-2 text-xs font-semibold transition-all',
              tab === 'server'
                ? 'bg-raised-2 text-foreground shadow-sm'
                : 'text-muted hover:text-foreground',
            )}
          >
            <Server className="size-3.5 text-accent" aria-hidden="true" />
            <span>Archive Server</span>
          </button>
        </div>

        {/* ===================== TAB 1: CLIENT DEVICE ===================== */}
        {tab === 'client' && !busy && !done && (
          <div className="grid gap-3 text-sm">
            {desktopBridge ? (
              <>
                <div className="flex items-center justify-between">
                  <span className="font-mono text-[10.5px] font-semibold uppercase tracking-wider text-muted">
                    Detected drives on this PC
                  </span>
                  <button
                    type="button"
                    onClick={() => void refreshLocalDevices()}
                    disabled={localLoading}
                    className="focus-ring inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-accent hover:bg-raised-2"
                  >
                    <RefreshCw className={cn('size-3', localLoading && 'animate-spin')} aria-hidden="true" />
                    Refresh drives
                  </button>
                </div>

                {localLoading && !localDevices ? (
                  <div className="flex items-center gap-2 rounded-xl border border-hairline bg-ink/40 p-4 text-xs text-muted">
                    <Loader2 className="size-4 animate-spin text-accent" aria-hidden="true" />
                    Scanning local CD/DVD recorders and USB drives…
                  </div>
                ) : (
                  <div className="grid gap-2">
                    {localDevices?.recorders.map((rec) => (
                      <DriveCard
                        key={`local-disc-${rec.drive}`}
                        kind="disc"
                        drive={rec.drive}
                        title={rec.model}
                        subtitle="Local CD/DVD Recorder · Direct IMAPI burn"
                        selected={localTarget?.kind === 'disc' && localTarget.drive === rec.drive}
                        onSelect={() => {
                          setLocalTarget({ kind: 'disc', drive: rec.drive, label: rec.model });
                          setLocalDiscInfo('');
                        }}
                      />
                    ))}
                    {localDevices?.usb_drives.map((usb) => (
                      <DriveCard
                        key={`local-usb-${usb.drive}`}
                        kind="usb"
                        drive={usb.drive}
                        title={usb.label}
                        subtitle={`Removable USB (${usb.filesystem}) · ${formatBytes(usb.free_bytes)} free of ${formatBytes(usb.total_bytes)}`}
                        selected={localTarget?.kind === 'usb' && localTarget.drive === usb.drive}
                        onSelect={() => {
                          setLocalTarget({ kind: 'usb', drive: usb.drive, label: usb.label });
                          setLocalDiscInfo('');
                        }}
                      />
                    ))}
                    {localDevices && localDevices.recorders.length === 0 && localDevices.usb_drives.length === 0 && (
                      <div className="rounded-xl border border-dashed border-hairline-strong bg-ink/40 p-4 text-center text-xs text-muted">
                        No CD/DVD recorder or removable USB drive detected on this PC.
                        Insert a USB stick or disc and click <b>Refresh drives</b>, or choose a folder below.
                      </div>
                    )}
                  </div>
                )}

                {localDiscInfo && (
                  <p className="rounded-lg border border-hairline bg-raised/60 px-3 py-2 font-mono text-[11.5px] text-accent">
                    {localDiscInfo}
                  </p>
                )}

                <div className="flex flex-wrap items-center gap-2 pt-1">
                  {localTarget && (
                    <Button
                      onClick={() => void handleStartDesktopExport(localTarget.kind, localTarget.drive)}
                      className="flex-1"
                    >
                      {localTarget.kind === 'disc' ? <Disc3 className="size-4" aria-hidden="true" /> : <Usb className="size-4" aria-hidden="true" />}
                      {localTarget.kind === 'disc'
                        ? `Burn directly to ${localTarget.drive}`
                        : `Export directly to USB ${localTarget.drive}`}
                    </Button>
                  )}
                  {localTarget?.kind === 'disc' && (
                    <Button variant="outline" onClick={() => void handleCheckDisc('client', localTarget.drive)}>
                      Check disc
                    </Button>
                  )}
                  <Button variant="outline" onClick={() => void handleDesktopPickFolder()}>
                    <FolderOpen className="size-4" aria-hidden="true" />
                    Choose folder
                  </Button>
                </div>

                <div className="flex items-center justify-between border-t border-hairline pt-2.5 text-xs text-muted">
                  <span>Or save portable files:</span>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => void handleBrowserDownload('iso')}
                      className="font-medium text-accent hover:underline"
                    >
                      Save .ISO
                    </button>
                    <span>·</span>
                    <button
                      type="button"
                      onClick={() => void handleBrowserDownload('zip')}
                      className="font-medium text-accent hover:underline"
                    >
                      Save .ZIP
                    </button>
                  </div>
                </div>
              </>
            ) : (
              <>
                {/* Browser client export */}
                {canDirectBrowserUsb && (
                  <div className="grid gap-1.5 rounded-xl border border-ok/30 bg-ok/[0.06] p-3">
                    <Button onClick={() => void handleBrowserDirectUsb()}>
                      <Usb className="size-4" aria-hidden="true" />
                      Write directly to USB drive / folder
                    </Button>
                    <p className="text-xs text-muted">
                      Select your connected USB drive in the picker — PAXS will write <code>DICOMDIR</code> and all study images directly onto it.
                    </p>
                  </div>
                )}

                <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                  <div className="grid gap-1.5 rounded-xl border border-hairline bg-ink/50 p-3">
                    <Button variant="outline" onClick={() => void handleBrowserDownload('iso')}>
                      <Disc3 className="size-4 text-accent" aria-hidden="true" />
                      Download CD/DVD (.iso)
                    </Button>
                    <p className="text-[11.5px] leading-snug text-muted">
                      Right-click the downloaded ISO in Windows and select <b>Burn disc image</b>.
                    </p>
                  </div>
                  <div className="grid gap-1.5 rounded-xl border border-hairline bg-ink/50 p-3">
                    <Button variant="outline" onClick={() => void handleBrowserDownload('zip')}>
                      <Download className="size-4 text-accent" aria-hidden="true" />
                      Download USB (.zip)
                    </Button>
                    <p className="text-[11.5px] leading-snug text-muted">
                      Extract to your USB drive keeping <code>DICOMDIR</code> and subfolders together.
                    </p>
                  </div>
                </div>

                <div className="flex items-start gap-2 rounded-xl border border-hairline bg-raised/50 px-3 py-2.5 text-xs text-muted">
                  <Info className="mt-0.5 size-4 shrink-0 text-accent" aria-hidden="true" />
                  <span>
                    Web browsers block direct CD/DVD burner access. For automatic local CD/DVD &amp; USB drive detection and 1-click disc burning on this PC, open the standalone <b className="text-foreground">PAXS-Archive.exe</b> desktop app — or switch to the <button type="button" className="font-semibold text-accent hover:underline" onClick={() => { setTab('server'); resetFeedback(); }}>Archive Server</button> tab.
                  </span>
                </div>
              </>
            )}
          </div>
        )}

        {/* ===================== TAB 2: ARCHIVE SERVER ===================== */}
        {tab === 'server' && !busy && !done && (
          <div className="grid gap-3 text-sm">
            <div className="flex items-center justify-between">
              <span className="font-mono text-[10.5px] font-semibold uppercase tracking-wider text-muted">
                Detected drives on archive server
              </span>
              <button
                type="button"
                onClick={() => void refreshServerDevices()}
                disabled={serverLoading}
                className="focus-ring inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-accent hover:bg-raised-2"
              >
                <RefreshCw className={cn('size-3', serverLoading && 'animate-spin')} aria-hidden="true" />
                Refresh drives
              </button>
            </div>

            {serverLoading && !serverDevices ? (
              <div className="flex items-center gap-2 rounded-xl border border-hairline bg-ink/40 p-4 text-xs text-muted">
                <Loader2 className="size-4 animate-spin text-accent" aria-hidden="true" />
                Scanning archive server CD/DVD recorders and USB drives…
              </div>
            ) : (
              <div className="grid gap-2">
                {serverDevices?.recorders.map((rec) => (
                  <DriveCard
                    key={`srv-disc-${rec.drive}`}
                    kind="disc"
                    drive={rec.drive}
                    title={rec.model}
                    subtitle={
                      serverDevices.disc_burn_enabled
                        ? 'Server Optical Recorder · Blank CD/DVD required'
                        : 'Server Optical Recorder · Disabled in Settings (DISC_BURN_ENABLED)'
                    }
                    selected={serverTarget?.kind === 'disc' && serverTarget.drive === rec.drive}
                    onSelect={() => {
                      setServerTarget({ kind: 'disc', drive: rec.drive, label: rec.model });
                      setServerDiscInfo('');
                    }}
                  />
                ))}
                {serverDevices?.usb_drives.map((usb) => (
                  <DriveCard
                    key={`srv-usb-${usb.drive}`}
                    kind="usb"
                    drive={usb.drive}
                    title={usb.label}
                    subtitle={`Server USB (${usb.filesystem}) · ${formatBytes(usb.free_bytes)} free of ${formatBytes(usb.total_bytes)}`}
                    selected={serverTarget?.kind === 'usb' && serverTarget.drive === usb.drive}
                    onSelect={() => {
                      setServerTarget({ kind: 'usb', drive: usb.drive, label: usb.label });
                      setServerDiscInfo('');
                    }}
                  />
                ))}
                {serverDevices && serverDevices.recorders.length === 0 && serverDevices.usb_drives.length === 0 && (
                  <div className="rounded-xl border border-dashed border-hairline-strong bg-ink/40 p-4 text-center text-xs text-muted">
                    <HardDrive className="mx-auto mb-1.5 size-5 opacity-60" aria-hidden="true" />
                    No CD/DVD recorder or removable USB drive was detected on the archive server.
                    Connect media to the server and click <b>Refresh drives</b>.
                  </div>
                )}
              </div>
            )}

            {serverDiscInfo && (
              <p className="rounded-lg border border-hairline bg-raised/60 px-3 py-2 font-mono text-[11.5px] text-accent">
                {serverDiscInfo}
              </p>
            )}

            {serverTarget && (
              <div className="flex flex-wrap items-center gap-2 pt-1">
                <Button
                  onClick={() => void handleStartServerExport()}
                  disabled={serverTarget.kind === 'disc' && !serverDevices?.disc_burn_enabled}
                  className="flex-1"
                >
                  {serverTarget.kind === 'disc' ? <Disc3 className="size-4" aria-hidden="true" /> : <Usb className="size-4" aria-hidden="true" />}
                  {serverTarget.kind === 'disc'
                    ? `Burn on server (${serverTarget.drive})`
                    : `Write to server USB (${serverTarget.drive})`}
                </Button>
                {serverTarget.kind === 'disc' && (
                  <Button variant="outline" onClick={() => void handleCheckDisc('server', serverTarget.drive)}>
                    Check disc
                  </Button>
                )}
              </div>
            )}

            {serverTarget?.kind === 'disc' && serverDevices && !serverDevices.disc_burn_enabled && (
              <p className="text-xs text-warn">
                Server CD/DVD burning is currently turned off. Enable <b>Enable CD/DVD burning</b> in Admin Settings to burn discs on the server.
              </p>
            )}
          </div>
        )}

        {/* ===================== LIVE PROGRESS / RESULT ===================== */}
        {busy && (
          <div role="status" className="flex items-center gap-3 rounded-xl border border-accent/30 bg-accent/10 p-4 text-sm">
            <Loader2 className="size-5 shrink-0 animate-spin text-accent" aria-hidden="true" />
            <div className="min-w-0">
              <p className="font-semibold text-foreground">
                {busyMessage || (phase && PHASE_LABELS[phase]) || 'Exporting study…'}
              </p>
              <p className="mt-0.5 text-xs text-muted">
                Keep the drive connected until verification finishes.
              </p>
            </div>
          </div>
        )}

        {done && (
          <div role="status" className="grid gap-2.5 rounded-xl border border-ok/35 bg-ok/10 p-4 text-sm">
            <div className="flex items-start gap-2.5">
              <CheckCircle2 className="mt-0.5 size-5 shrink-0 text-ok" aria-hidden="true" />
              <div className="min-w-0">
                <p className="font-semibold text-ok">Export completed &amp; verified</p>
                <p className="mt-0.5 text-xs text-foreground/90">
                  {successMessage
                    || (status?.output_path
                      ? `Written and SHA-256 verified at ${status.output_path}`
                      : 'Media verified. You can safely eject or remove it.')}
                </p>
              </div>
            </div>
            <div className="flex flex-wrap gap-2">
              {desktopBridge && status?.output_path && (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => void desktopBridge.open_local_path(status.output_path || '')}
                >
                  <FolderOpen className="size-3.5" aria-hidden="true" />
                  Open folder
                </Button>
              )}
              <Button size="sm" variant="outline" onClick={resetFeedback}>
                Export another copy
              </Button>
            </div>
          </div>
        )}

        {(error || status?.error) && (
          <p role="alert" className="rounded-xl border border-danger/40 bg-danger/10 px-3.5 py-2.5 text-xs font-medium text-danger">
            {error || status?.error}
          </p>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={busy}>Close</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
