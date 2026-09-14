import { useState } from 'react';
import { StudyListPage } from '@/components/study-list/StudyListPage';
import { PacsViewerDashboard } from '@/components/viewer/PacsViewerDashboard';
import { TooltipProvider } from '@/components/ui/tooltip';

export default function App() {
  const [view, setView] = useState<'list' | 'viewer'>('list');

  return (
    <TooltipProvider>
      {view === 'viewer' ? (
        <PacsViewerDashboard onBack={() => setView('list')} />
      ) : (
        <StudyListPage onOpenViewer={() => setView('viewer')} />
      )}
    </TooltipProvider>
  );
}
