import { useState } from 'react';
import {
  ZoomIn,
  Move,
  Ruler,
  RotateCw,
  Sun,
  Grid,
  Sliders,
  Play,
  Pause,
  Settings,
  User,
  ChevronLeft,
  Activity,
  Layers,
  HelpCircle,
  Scissors,
  Eye,
  Box,
  Spline,
  Flame,
  Compass,
  Heart,
  BarChart3,
  Minimize2,
  Link2,
  Star,
  FileCheck,
  LayoutTemplate,
  Download,
  History,
  CheckCircle2,
  Layers3,
  SlidersHorizontal,
  Zap,
  RefreshCw,
  Undo2,
  Redo2,
  BrainCircuit,
  Sparkles,
  ShieldCheck,
  TrendingUp
} from 'lucide-react';
import { PaxsFooter } from '../common/PaxsFooter';

interface PacsViewerDashboardProps {
  onBack?: () => void;
}

export function PacsViewerDashboard({ onBack }: PacsViewerDashboardProps) {
  const [activeTool, setActiveTool] = useState<string>('zoom');
  const [gridMode, setGridMode] = useState<'1x1' | '2x2'>('2x2');
  const [isPlaying, setIsPlaying] = useState<boolean>(false);
  const [currentSlice, setCurrentSlice] = useState<number>(120);
  const [activePreset, setActivePreset] = useState<string>('soft-tissue');
  const [hoverHu, setHoverHu] = useState<string>('145 HU');

  // Measurement Undo/Redo State History
  const [historyIndex, setHistoryIndex] = useState<number>(2);

  // Section 1 State: MPR & 3D Upgrades
  const [projectionMode, setProjectionMode] = useState<'normal' | 'mip' | 'minip' | 'avgip'>('normal');
  const [slabThickness, setSlabThickness] = useState<number>(1);
  const [volumeLut, setVolumeLut] = useState<'bone' | 'vascular' | 'lung' | 'soft-tissue' | 'angio-gold'>('bone');
  const [cropBoxOpen, setCropBoxOpen] = useState<boolean>(false);
  const [cropBounds, setCropBounds] = useState({
    xmin: 0, xmax: 100,
    ymin: 0, ymax: 100,
    zmin: 0, zmax: 100,
  });
  const [rulerActive, setRulerActive] = useState<boolean>(false);
  const rulerDistance = '42.8 mm';
  const [cmprActive, setCmprActive] = useState<boolean>(false);

  // Section 2 State: AI-Assisted Computer-Aided Diagnosis (CAD / AI Overlays)
  const [aiCadActive, setAiCadActive] = useState<boolean>(true);
  const [aiSegmentation, setAiSegmentation] = useState<'none' | 'lungs' | 'liver' | 'heart' | 'kidneys'>('lungs');
  const [boneSuppressionActive, setBoneSuppressionActive] = useState<boolean>(false);
  const [tumorDoublingActive, setTumorDoublingActive] = useState<boolean>(false);

  // Section 3 State: Advanced Diagnostic Tooling Suite
  const [suvActive, setSuvActive] = useState<boolean>(false);
  const [cobbAngleActive, setCobbAngleActive] = useState<boolean>(false);
  const [agatstonActive, setAgatstonActive] = useState<boolean>(false);
  const [ctrActive, setCtrActive] = useState<boolean>(false);
  const [roiHistogramActive, setRoiHistogramActive] = useState<boolean>(false);
  const [activeToolSuiteModal, setActiveToolSuiteModal] = useState<string | null>(null);

  // Section 4 State: Multi-Modality Image Fusion & Registration
  const [fusionActive, setFusionActive] = useState<boolean>(false);
  const [fusionOpacity, setFusionOpacity] = useState<number>(55);
  const [petLut, setPetLut] = useState<'hot-iron' | 'rainbow' | 'pet-spectrum' | 'jet'>('hot-iron');
  const [registrationAligned, setRegistrationAligned] = useState<boolean>(true);
  const [dsaActive, setDsaActive] = useState<boolean>(false);

  // Section 5 State: Viewport Ergonomics & Hanging Protocols
  const [hangingProtocol, setHangingProtocol] = useState<'ct-chest' | 'mammo-4view' | 'mri-brain' | 'xray-1x1'>('ct-chest');
  const [priorComparisonActive, setPriorComparisonActive] = useState<boolean>(false);
  const [linkedScrolling, setLinkedScrolling] = useState<boolean>(true);
  const [keyImages, setKeyImages] = useState<Array<{ id: number; slice: number; title: string; note: string }>>([
    { id: 1, slice: 45, title: 'Right Lung Nodule', note: '6.2mm subpleural nodule in RUL' },
    { id: 2, slice: 120, title: 'Bifurcation Carina', note: 'Normal airway lumen diameter' },
  ]);
  const [keyImageDrawerOpen, setKeyImageDrawerOpen] = useState<boolean>(false);
  const [srModalOpen, setSrModalOpen] = useState<boolean>(false);
  const [exportNotice, setExportNotice] = useState<string | null>(null);

  const totalSlices = 240;

  const presets = [
    { id: 'auto', label: 'Auto', window: '400 / 40' },
    { id: 'soft-tissue', label: 'Soft Tissue', window: '400 / 40' },
    { id: 'brain', label: 'Brain', window: '80 / 40' },
    { id: 'lung', label: 'Lung', window: '1500 / -600' },
    { id: 'bone', label: 'Bone', window: '1800 / 400' },
  ];

  const tools = [
    { id: 'zoom', label: 'Zoom In (Z)', icon: ZoomIn },
    { id: 'pan', label: 'Move / Pan (P)', icon: Move },
    { id: 'ruler', label: 'Ruler Measurement (M)', icon: Ruler },
    { id: 'contrast', label: 'Contrast (Sun)', icon: Sun },
    { id: 'rotate', label: 'Rotate Clockwise', icon: RotateCw },
    { id: 'presets', label: 'Sliders Presets', icon: Sliders },
  ];

  const volumeLutOptions = [
    { id: 'bone', label: 'Bone / Skeletal', color: '#fef08a' },
    { id: 'vascular', label: 'Angio Vascular (Crimson)', color: '#ef4444' },
    { id: 'lung', label: 'Pulmonary Lung (Cyan)', color: '#06b6d4' },
    { id: 'soft-tissue', label: 'Soft Tissue (Flesh)', color: '#f472b6' },
    { id: 'angio-gold', label: 'Gold Contrast', color: '#eab308' },
  ];

  const togglePlay = () => {
    setIsPlaying(!isPlaying);
  };

  const handleUndo = () => {
    if (historyIndex > 0) {
      setHistoryIndex(historyIndex - 1);
      triggerExportNotice(`Undo Measurement Action (${historyIndex - 1} remaining)`);
    }
  };

  const handleRedo = () => {
    setHistoryIndex(historyIndex + 1);
    triggerExportNotice(`Redo Measurement Action (${historyIndex + 1})`);
  };

  const addKeyImage = () => {
    const newBookmark = {
      id: Date.now(),
      slice: currentSlice,
      title: `Key Slice ${currentSlice}`,
      note: `Marked at slice ${currentSlice} for radiologist SR report`,
    };
    setKeyImages([...keyImages, newBookmark]);
    triggerExportNotice(`Slice ${currentSlice} added to Key Images`);
  };

  const triggerExportNotice = (msg: string) => {
    setExportNotice(msg);
    setTimeout(() => setExportNotice(null), 3000);
  };

  return (
    <div className="flex flex-col h-screen w-screen bg-[#070c18] text-slate-100 font-sans selection:bg-cyan-500/30 overflow-hidden">
      
      {/* Toast Notification */}
      {exportNotice && (
        <div className="fixed bottom-6 right-6 bg-cyan-950/90 border border-cyan-500/50 text-cyan-200 px-4 py-2.5 rounded-lg shadow-2xl z-50 flex items-center gap-2 text-xs font-mono animate-in slide-in-from-bottom-5">
          <CheckCircle2 className="w-4 h-4 text-cyan-400" />
          <span>{exportNotice}</span>
        </div>
      )}

      {/* Compact Top Header with h-14 & dark slate background bg-slate-900 */}
      <header className="h-14 bg-slate-900 border-b border-slate-800 px-4 flex items-center justify-between shrink-0 shadow-md z-40">
        
        {/* Left: Brand mark & Back Button */}
        <div className="flex items-center gap-3">
          <button
            onClick={onBack || (() => { window.location.href = '/viewer'; })}
            className="p-2 hover:bg-slate-800 rounded-lg text-slate-300 hover:text-cyan-400 transition-colors flex items-center gap-1 text-xs font-medium cursor-pointer"
            title="Back to PACS Study List"
          >
            <ChevronLeft className="w-4 h-4" />
            <span>Studies</span>
          </button>
          <div
            className="flex items-center gap-2 cursor-pointer"
            onClick={onBack || (() => { window.location.href = '/viewer'; })}
          >
            <div className="w-8 h-8 rounded-lg bg-cyan-500/10 border border-cyan-500/30 flex items-center justify-center text-cyan-400 shadow-[0_0_12px_rgba(34,211,238,0.2)]">
              <BrainCircuit className="w-4 h-4" />
            </div>
            <span className="font-bold text-sm tracking-wide text-white">PAXS <span className="text-cyan-400 text-xs font-mono">AI CAD 2.0</span></span>
          </div>
        </div>

        {/* Centered Patient Info Bar with Prominent Patient Name Display */}
        <div className="flex items-center gap-4 bg-slate-950/90 px-4 py-1.5 rounded-full border border-cyan-500/30 shadow-[0_0_20px_rgba(34,211,238,0.15)]">
          <div className="flex items-center gap-2.5 text-xs">
            {/* Prominent High-Contrast Patient Name Display */}
            <span className="font-extrabold text-sm tracking-wider text-white bg-slate-900 px-3 py-0.5 rounded-md border border-cyan-500/40 uppercase shadow-sm">
              ELEANOR SMITH
            </span>
            <span className="text-slate-500">•</span>
            <span className="text-cyan-300 font-mono font-semibold">54Y / FEMALE</span>
            <span className="text-slate-500">•</span>
            <span className="text-slate-300 font-mono">MRN: 904-8120</span>
          </div>
          <span className="h-4 w-px bg-slate-800" />
          <div className="flex items-center gap-1.5">
            <span className="px-2 py-0.5 rounded text-[10px] font-mono font-bold uppercase bg-cyan-950 text-cyan-400 border border-cyan-500/40">
              CT CHEST / CAD
            </span>
            <span className="text-[11px] text-slate-400 font-mono">240 Slices</span>
          </div>
        </div>

        {/* Right-aligned Profile, AI Status & Settings */}
        <div className="flex items-center gap-2">
          
          {/* AI CAD Active Status Indicator */}
          <div className="flex items-center gap-2 bg-cyan-950/60 px-3 py-1 rounded-full border border-cyan-500/40 text-xs font-mono text-cyan-300 mr-2 shadow-[0_0_12px_rgba(34,211,238,0.2)]">
            <Sparkles className="w-3.5 h-3.5 text-cyan-400 animate-spin" />
            <span className="text-cyan-300 font-bold">AI CAD INFERENCE ACTIVE</span>
          </div>

          <button className="p-2 hover:bg-slate-800 rounded-lg text-slate-300 transition-colors relative" title="Help & Shortcuts">
            <HelpCircle className="w-4 h-4" />
          </button>
          <button className="p-2 hover:bg-slate-800 rounded-lg text-slate-300 transition-colors" title="Settings">
            <Settings className="w-4 h-4" />
          </button>
          
          <div className="h-4 w-px bg-slate-800 mx-1" />

          {/* User Profile Avatar */}
          <div className="flex items-center gap-2 pl-1 cursor-pointer group">
            <div className="w-8 h-8 rounded-full bg-slate-800 border border-slate-700 flex items-center justify-center text-slate-300 group-hover:border-cyan-500/50 transition-colors">
              <User className="w-4 h-4" />
            </div>
            <span className="text-xs font-medium text-slate-300 group-hover:text-white hidden lg:inline">Dr. Harrison</span>
          </div>
        </div>
      </header>

      {/* Main Canvas Radiology Scan Viewport Area (Grid gap-2 bg-black p-3) */}
      <main className="flex-1 min-h-0 flex gap-3 p-3 relative overflow-hidden bg-[#070c18]">
        
        {/* Left Series Drawer & Section 2 AI CAD Controls */}
        <aside className="w-64 bg-slate-900/90 rounded-xl border border-slate-800 p-3 flex flex-col gap-3 shrink-0 backdrop-blur-md">
          <div className="flex items-center justify-between pb-2 border-b border-slate-800 text-xs font-semibold text-slate-300">
            <span className="flex items-center gap-2"><BrainCircuit className="w-3.5 h-3.5 text-cyan-400" /> AI CAD MODULE (SEC. 2)</span>
            <span className="font-mono text-[10px] text-cyan-400 font-bold">94.2% CONF</span>
          </div>

          {/* Section 2: AI CAD Controls Box */}
          <div className="p-3 bg-slate-950/90 border border-cyan-500/30 rounded-lg flex flex-col gap-2.5">
            
            {/* 2.1 Automated Pulmonary Nodule & Mass Detection */}
            <button
              onClick={() => {
                setAiCadActive(!aiCadActive);
                triggerExportNotice(aiCadActive ? 'AI Nodule CAD Overlays Hidden' : 'AI Nodule CAD Active (6.2mm RUL Nodule)');
              }}
              className={`w-full p-2 rounded-lg border text-left flex items-center justify-between transition-all ${
                aiCadActive
                  ? 'bg-cyan-950/70 border-cyan-500/50 text-cyan-300 font-bold shadow-[0_0_12px_rgba(34,211,238,0.3)]'
                  : 'bg-slate-900/60 border-slate-800 text-slate-400 hover:text-white'
              }`}
            >
              <span className="flex items-center gap-2 text-xs"><Sparkles className="w-3.5 h-3.5 text-cyan-400" /> Nodule CAD Overlay</span>
              <span className="text-[10px] font-mono font-bold text-cyan-400">{aiCadActive ? 'ON' : 'OFF'}</span>
            </button>

            {/* 2.2 AI Organ & Structure Segmentation Mask */}
            <div className="space-y-1 text-xs">
              <div className="text-[11px] font-semibold text-slate-300">AI Organ Segmentation:</div>
              <select
                value={aiSegmentation}
                onChange={(e) => setAiSegmentation(e.target.value as any)}
                className="w-full bg-slate-900 border border-slate-800 text-slate-200 rounded p-1.5 text-xs font-semibold outline-none cursor-pointer"
              >
                <option value="none">None (Disabled)</option>
                <option value="lungs">Lungs Mask (Cyan 1.25L)</option>
                <option value="liver">Liver Mask (Emerald 1.48L)</option>
                <option value="heart">Heart Mask (Rose 0.65L)</option>
                <option value="kidneys">Kidneys Mask (Purple 0.32L)</option>
              </select>
            </div>

            {/* 2.3 Automated Bone Suppression (Rib Subtraction) */}
            <button
              onClick={() => {
                setBoneSuppressionActive(!boneSuppressionActive);
                triggerExportNotice(boneSuppressionActive ? 'Standard CXR View' : 'Bone Suppression Active (Rib Subtraction)');
              }}
              className={`w-full p-2 rounded-lg border text-left flex items-center justify-between transition-all ${
                boneSuppressionActive
                  ? 'bg-amber-950/70 border-amber-500/50 text-amber-300 font-bold shadow-[0_0_12px_rgba(245,158,11,0.3)]'
                  : 'bg-slate-900/60 border-slate-800 text-slate-400 hover:text-white'
              }`}
            >
              <span className="flex items-center gap-2 text-xs"><ShieldCheck className="w-3.5 h-3.5 text-amber-400" /> Bone Suppression</span>
              <span className="text-[10px] font-mono font-bold text-amber-400">{boneSuppressionActive ? 'SUBTRACTED' : 'OFF'}</span>
            </button>

            {/* 2.4 Volumetric Tumor Doubling Time & Growth Profiler */}
            <button
              onClick={() => {
                setTumorDoublingActive(!tumorDoublingActive);
                triggerExportNotice(tumorDoublingActive ? 'Growth Profiler Off' : 'Tumor Doubling Profiler Active (240 Days, ΔV +14.2%)');
              }}
              className={`w-full p-2 rounded-lg border text-left flex items-center justify-between transition-all ${
                tumorDoublingActive
                  ? 'bg-emerald-950/70 border-emerald-500/50 text-emerald-300 font-bold shadow-[0_0_12px_rgba(52,211,153,0.3)]'
                  : 'bg-slate-900/60 border-slate-800 text-slate-400 hover:text-white'
              }`}
            >
              <span className="flex items-center gap-2 text-xs"><TrendingUp className="w-3.5 h-3.5 text-emerald-400" /> Tumor Doubling Time</span>
              <span className="text-[10px] font-mono font-bold text-emerald-400">{tumorDoublingActive ? '240 DAYS' : 'OFF'}</span>
            </button>

          </div>

          {/* Series List */}
          <div className="flex flex-col gap-2 overflow-y-auto pr-1 flex-1">
            <div className="text-[10px] font-mono font-bold text-slate-400 uppercase tracking-wider">Series List</div>
            {[
              { id: 1, name: 'Axial CT Substrate', mod: 'CT', imgs: 240, active: true },
              { id: 2, name: 'AI Lung Nodule CAD', mod: 'CAD', imgs: 240, active: aiCadActive },
              { id: 3, name: 'Coronal Spine MPR', mod: 'CT', imgs: 180, active: false },
            ].map((s) => (
              <div
                key={s.id}
                className={`p-2 rounded-lg border transition-all cursor-pointer flex gap-2.5 items-center group ${
                  s.active
                    ? 'bg-slate-800/80 border-cyan-500/50 shadow-[0_0_15px_rgba(34,211,238,0.15)]'
                    : 'bg-slate-950/40 border-slate-800/80 hover:border-slate-700 hover:bg-slate-800/40'
                }`}
              >
                <div className="w-10 h-10 rounded bg-black border border-slate-800 flex items-center justify-center text-cyan-500/40 font-mono text-[10px] overflow-hidden relative">
                  <span className="font-bold">{s.mod}</span>
                </div>
                <div className="flex flex-col min-w-0 flex-1">
                  <span className={`text-xs font-semibold truncate ${s.active ? 'text-white' : 'text-slate-300 group-hover:text-white'}`}>
                    {s.name}
                  </span>
                  <span className="text-[10px] font-mono text-slate-400">{s.imgs} imgs</span>
                </div>
              </div>
            ))}
          </div>
        </aside>

        {/* Central Viewport Grid */}
        <section className="flex-1 min-h-0 flex flex-col relative">
          
          {/* Floating Translucent Toolbar Overlay with FIXED Undo / Redo & Measurement Dropdown Clickability */}
          <div className="absolute top-4 left-1/2 -translate-x-1/2 bg-slate-900/95 backdrop-blur-md border border-slate-700/60 rounded-full px-4 py-2 flex items-center gap-2.5 shadow-2xl z-30 flex-wrap max-w-[98%] justify-center pointer-events-auto">
            
            {/* FIXED Undo & Redo Buttons positioned cleanly before measurement tools */}
            <div className="flex items-center gap-1 bg-slate-950/80 p-1 rounded-full border border-slate-800">
              <button
                onClick={handleUndo}
                className="p-1.5 rounded-full text-slate-300 hover:text-white hover:bg-slate-800 transition-colors flex items-center justify-center pointer-events-auto"
                title="Undo Measurement Action (Ctrl+Z)"
              >
                <Undo2 className="w-3.5 h-3.5" />
              </button>
              <button
                onClick={handleRedo}
                className="p-1.5 rounded-full text-slate-300 hover:text-white hover:bg-slate-800 transition-colors flex items-center justify-center pointer-events-auto"
                title="Redo Measurement Action (Ctrl+Y)"
              >
                <Redo2 className="w-3.5 h-3.5" />
              </button>
            </div>

            <div className="h-4 w-px bg-slate-700/60 mx-0.5" />

            {/* Measurement Tool Buttons with 100% Clickable Pointer Events */}
            {tools.map((t) => {
              const Icon = t.icon;
              const isActive = activeTool === t.id;
              return (
                <div key={t.id} className="relative group">
                  <button
                    onClick={() => setActiveTool(t.id)}
                    className={`p-2 rounded-full transition-all flex items-center justify-center pointer-events-auto ${
                      isActive
                        ? 'bg-gradient-to-r from-cyan-500 to-teal-400 text-slate-950 font-bold shadow-[0_0_16px_rgba(34,211,238,0.5)] scale-105'
                        : 'text-slate-300 hover:text-white hover:bg-slate-800/80'
                    }`}
                  >
                    <Icon className="w-4 h-4" />
                  </button>
                  <div className="absolute top-full mt-2 left-1/2 -translate-x-1/2 px-2.5 py-1 bg-slate-950 text-slate-200 text-[11px] font-medium rounded-md border border-slate-800 opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none whitespace-nowrap shadow-xl z-50">
                    {t.label}
                  </div>
                </div>
              );
            })}

            <div className="h-4 w-px bg-slate-700/60 mx-0.5" />

            {/* Projection Mode Selector */}
            <div className="flex items-center gap-1.5 bg-slate-950/70 px-2.5 py-1 rounded-full border border-slate-800 text-xs pointer-events-auto">
              <Eye className="w-3.5 h-3.5 text-cyan-400" />
              <select
                value={projectionMode}
                onChange={(e) => setProjectionMode(e.target.value as any)}
                className="bg-transparent text-slate-200 font-semibold outline-none cursor-pointer text-xs"
              >
                <option value="normal" className="bg-slate-900">Normal Slice</option>
                <option value="mip" className="bg-slate-900">MIP (Max Intensity)</option>
                <option value="minip" className="bg-slate-900">MinIP (Min Intensity)</option>
                <option value="avgip" className="bg-slate-900">Average IP</option>
              </select>
            </div>

            <div className="h-4 w-px bg-slate-700/60 mx-0.5" />

            {/* Key Image Bookmark Action Button */}
            <button
              onClick={addKeyImage}
              className="px-2.5 py-1 rounded-full bg-slate-800/80 hover:bg-slate-700 text-slate-200 text-xs font-semibold flex items-center gap-1 transition-all pointer-events-auto"
              title="Bookmark current slice to Key Images"
            >
              <Star className="w-3.5 h-3.5 text-amber-400 fill-amber-400" />
              <span>Mark Key Slice</span>
            </button>

            {/* Linked Scrolling Toggle Button */}
            <button
              onClick={() => {
                setLinkedScrolling(!linkedScrolling);
                triggerExportNotice(linkedScrolling ? 'Scrolling Unlinked' : 'Scrolling Linked');
              }}
              className={`px-2.5 py-1 rounded-full text-xs font-semibold flex items-center gap-1 transition-all pointer-events-auto ${
                linkedScrolling
                  ? 'bg-cyan-500 text-slate-950 font-bold shadow-[0_0_12px_rgba(34,211,238,0.5)]'
                  : 'bg-slate-800 text-slate-400'
              }`}
            >
              <Link2 className="w-3.5 h-3.5" />
              <span>{linkedScrolling ? 'Linked Sync' : 'Unlinked'}</span>
            </button>

            <div className="h-4 w-px bg-slate-700/60 mx-0.5" />

            {/* Layout Grid Selector */}
            <div className="flex items-center bg-slate-950/60 p-0.5 rounded-full border border-slate-800 pointer-events-auto">
              <button
                onClick={() => setGridMode('1x1')}
                className={`px-2 py-1 rounded-full text-xs font-semibold transition-colors flex items-center gap-1 ${
                  gridMode === '1x1' ? 'bg-slate-800 text-cyan-400' : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                <Grid className="w-3 h-3" />
                1x1
              </button>
              <button
                onClick={() => setGridMode('2x2')}
                className={`px-2 py-1 rounded-full text-xs font-semibold transition-colors flex items-center gap-1 ${
                  gridMode === '2x2' ? 'bg-slate-800 text-cyan-400' : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                <Grid className="w-3 h-3" />
                2x2
              </button>
            </div>

            {/* Cine Loop Play Button */}
            <button
              onClick={togglePlay}
              className={`px-3 py-1 rounded-full text-xs font-semibold flex items-center gap-1.5 transition-all pointer-events-auto ${
                isPlaying
                  ? 'bg-emerald-500 text-slate-950 font-bold shadow-[0_0_14px_rgba(52,211,153,0.4)]'
                  : 'bg-slate-800 text-slate-200 hover:bg-slate-700'
              }`}
            >
              {isPlaying ? <Pause className="w-3.5 h-3.5" /> : <Play className="w-3.5 h-3.5" />}
              <span>{isPlaying ? 'Pause' : 'Cine'}</span>
            </button>
          </div>

          {/* Central Radiology Viewport Grid (gap-2 bg-black p-3) */}
          <div
            className={`flex-1 rounded-xl bg-black border border-slate-800 p-3 grid ${
              gridMode === '1x1' ? 'grid-cols-1 grid-rows-1' : 'grid-cols-2 grid-rows-2'
            } gap-2 relative overflow-hidden`}
          >
            {[
              { title: 'AXIAL · Z (AI CAD INFERENCE)', color: '#22d3ee' },
              ...(gridMode === '2x2'
                ? [
                    { title: 'SAGITTAL · X (ORGAN SEGMENTATION)', color: '#2dd4bf' },
                    { title: 'CORONAL · Y', color: '#fbbf24' },
                    { title: '3D SPATIAL VOLUME', color: '#a855f7' },
                  ]
                : []),
            ].map((vp, i) => (
              <div
                key={i}
                className="relative bg-[#03060d] border border-slate-800/80 rounded-lg overflow-hidden flex flex-col group hover:border-cyan-500/40 transition-colors"
                onMouseMove={() => {
                  const val = Math.floor(Math.random() * 300 - 50);
                  setHoverHu(`${val} HU`);
                }}
              >
                {/* Viewport Title & Status Dot */}
                <div className="absolute top-2 left-3 right-3 flex items-center justify-between z-20 pointer-events-none">
                  <div className="flex items-center gap-2 bg-slate-950/80 px-2.5 py-1 rounded-md backdrop-blur-sm border border-slate-800/60">
                    <span className="w-2 h-2 rounded-full" style={{ backgroundColor: vp.color }} />
                    <span className="text-[11px] font-mono font-bold tracking-wider text-slate-200">{vp.title}</span>
                  </div>
                  <div className="flex items-center gap-1.5">
                    {aiCadActive && (
                      <span className="text-[10px] font-mono font-bold text-cyan-400 bg-cyan-950/90 px-2 py-0.5 rounded border border-cyan-500/40 uppercase flex items-center gap-1">
                        <Sparkles className="w-3 h-3" /> CAD 94.2%
                      </span>
                    )}
                    {boneSuppressionActive && (
                      <span className="text-[10px] font-mono font-bold text-amber-400 bg-amber-950/90 px-2 py-0.5 rounded border border-amber-500/40 uppercase">
                        BONE SUBTRACTED
                      </span>
                    )}
                    <div className="text-[10px] font-mono text-cyan-400 bg-slate-950/70 px-2 py-1 rounded border border-slate-800/60">
                      {vp.title.includes('3D') ? `3D LUT: ${volumeLut.toUpperCase()}` : `SLICE ${currentSlice} / ${totalSlices}`}
                    </div>
                  </div>
                </div>

                {/* DICOM Metadata Corner HUD with Prominent High-Contrast Patient Name */}
                <div className="absolute top-10 left-3 text-[11px] font-mono space-y-0.5 pointer-events-none drop-shadow z-20">
                  {/* High contrast, uppercase formatted patient name display */}
                  <div className="text-white font-extrabold tracking-wide text-xs bg-slate-950/90 px-2 py-0.5 rounded border border-cyan-500/40 inline-block">
                    PATIENT: ELEANOR SMITH
                  </div>
                  <div className="text-cyan-300/80">kVp: 120 · mA: 250</div>
                  <div className="text-cyan-300/80">AI SEGMENTATION: {aiSegmentation.toUpperCase()}</div>
                </div>

                <div className="absolute top-10 right-3 text-[11px] font-mono text-cyan-300/80 space-y-0.5 text-right pointer-events-none drop-shadow z-20">
                  <div>SERIES: AI CAD OVERLAY</div>
                  <div>NODULE CONF: 94.2%</div>
                </div>

                <div className="absolute bottom-3 left-3 text-[11px] font-mono text-cyan-400 space-y-0.5 pointer-events-none drop-shadow z-20">
                  <div>W/L: {presets.find((p) => p.id === activePreset)?.window}</div>
                  <div>POS: {(currentSlice * 1.25).toFixed(1)}mm</div>
                </div>

                {/* Section 2 AI CAD Pulmonary Nodule Detection Badge */}
                {aiCadActive && (
                  <div className="absolute bottom-10 left-3 text-xs font-mono font-bold text-cyan-300 bg-cyan-950/90 px-2.5 py-1 rounded border border-cyan-500/50 shadow-[0_0_14px_rgba(34,211,238,0.4)] pointer-events-none z-20 flex items-center gap-1.5">
                    <Sparkles className="w-3.5 h-3.5 text-cyan-400 animate-spin" />
                    <span>AI NODULE DETECTED: RUL SUBPLEURAL 6.2mm (94.2% CONF)</span>
                  </div>
                )}

                {/* Volumetric Tumor Doubling Time Indicator */}
                {tumorDoublingActive && (
                  <div className="absolute bottom-16 left-3 text-xs font-mono font-bold text-emerald-300 bg-emerald-950/90 px-2.5 py-1 rounded border border-emerald-500/50 shadow-[0_0_14px_rgba(52,211,153,0.4)] pointer-events-none z-20 flex items-center gap-1.5">
                    <TrendingUp className="w-3.5 h-3.5 text-emerald-400" />
                    <span>TUMOR DOUBLING TIME: 240 DAYS (VOL GROWTH ΔV +14.2%)</span>
                  </div>
                )}

                <div className="absolute bottom-3 right-3 text-[11px] font-mono text-emerald-400 font-bold pointer-events-none drop-shadow z-20 bg-slate-950/80 px-2 py-0.5 rounded border border-emerald-500/30">
                  {hoverHu}
                </div>

                {/* Simulated Radiology Image Rendering Engine with AI Bounding Box & Organ Masks */}
                <div className="flex-1 flex items-center justify-center relative overflow-hidden bg-black">
                  <div className="w-[85%] h-[85%] rounded-full bg-gradient-to-tr from-slate-950 via-slate-900 to-slate-800 opacity-40 blur-2xl absolute" />
                  
                  {/* AI Organ Segmentation Overlay Mask */}
                  {aiSegmentation !== 'none' && (
                    <div
                      className="w-[65%] h-[65%] rounded-full blur-lg absolute transition-all opacity-30"
                      style={{
                        backgroundColor: aiSegmentation === 'lungs' ? '#06b6d4' : (aiSegmentation === 'liver' ? '#10b981' : (aiSegmentation === 'heart' ? '#f43f5e' : '#a855f7')),
                      }}
                    />
                  )}

                  <div className="relative z-10 text-center flex flex-col items-center">
                    <div className="w-44 h-44 rounded-full border-2 border-slate-800/80 flex items-center justify-center relative">
                      <div className="w-32 h-32 rounded-full border border-cyan-500/20 flex items-center justify-center">
                        <div className="w-20 h-20 rounded-full bg-cyan-950/30 border border-teal-500/30 flex items-center justify-center">
                          <Activity className="w-10 h-10 text-cyan-400/60 animate-pulse" />
                        </div>
                      </div>

                      {/* AI CAD Nodule Detection Bounding Box Ring */}
                      {aiCadActive && (
                        <div className="absolute top-6 right-6 w-10 h-10 border-2 border-dashed border-cyan-400 rounded-md animate-ping opacity-75" />
                      )}

                      <div className="absolute inset-0 flex items-center justify-center pointer-events-none opacity-40">
                        <div className="w-full h-px bg-cyan-400" />
                        <div className="h-full w-px bg-cyan-400 absolute" />
                      </div>
                    </div>
                    <span className="text-[10px] font-mono text-slate-500 mt-2 uppercase">
                      {aiCadActive ? 'AI DEEP LEARNING INFERENCE OVERLAY ACTIVE' : '16-BIT DICOM GRAYSCALE CANVAS'}
                    </span>
                  </div>
                </div>
              </div>
            ))}
          </div>

          {/* Bottom Presets Bar & Slider */}
          <div className="mt-3 flex items-center justify-between bg-slate-900/80 px-4 py-2 rounded-xl border border-slate-800 backdrop-blur-md gap-4 flex-wrap">
            <div className="flex items-center gap-2">
              <span className="text-xs font-semibold text-slate-400 mr-1">PRESETS:</span>
              {presets.map((p) => (
                <button
                  key={p.id}
                  onClick={() => setActivePreset(p.id)}
                  className={`px-3 py-1 rounded-lg text-xs font-medium transition-all ${
                    activePreset === p.id
                      ? 'bg-cyan-500/20 text-cyan-400 border border-cyan-500/40 font-semibold shadow-[0_0_10px_rgba(34,211,238,0.2)]'
                      : 'bg-slate-800/60 text-slate-400 hover:text-slate-200 border border-slate-800'
                  }`}
                >
                  {p.label}
                </button>
              ))}
            </div>

            {/* Slice Scroll Slider */}
            <div className="flex items-center gap-3 w-72">
              <span className="text-xs font-mono text-slate-400 shrink-0">Slice</span>
              <input
                type="range"
                min={1}
                max={totalSlices}
                value={currentSlice}
                onChange={(e) => setCurrentSlice(Number(e.target.value))}
                className="w-full h-1.5 bg-slate-800 rounded-lg appearance-none cursor-pointer accent-cyan-400"
              />
              <span className="text-xs font-mono font-bold text-cyan-400 shrink-0">{currentSlice}</span>
            </div>
          </div>

        </section>
      </main>

      {/* PAXS Archive Site Footer */}
      <PaxsFooter />
    </div>
  );
}
