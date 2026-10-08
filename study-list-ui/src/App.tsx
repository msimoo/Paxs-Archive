import { StudyListPage } from '@/components/study-list/StudyListPage';
import { TooltipProvider } from '@/components/ui/tooltip';

export default function App() {
  // C07: the mock viewer dashboard (invented AI CAD findings and synthetic HU
  // values) was removed — production must not ship fabricated clinical content.
  // The real viewer is opened in its own window (see getViewerUrl).
  return (
    <TooltipProvider>
      <StudyListPage />
    </TooltipProvider>
  );
}
