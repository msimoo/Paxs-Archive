import type { ReactElement } from 'react';
import { ArrowUpRight, ChevronDown, Eye, Layers, Microscope } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem,
  DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { openViewer } from '@/lib/api';
import { cn } from '@/lib/utils';

const workflows = [
  // The PAXS mode leads: it is the reading workflow this archive exists for.
  // Basic stays the route a plain /viewer link resolves to (getViewerUrl default),
  // so this order is a menu choice, not a change of default.
  {id: 'diagnostic', label: 'PAXS Diagnostic', description: 'Diagnostic reading — PAXS toolbar, 3D and reporting', icon: Microscope},
  {id: 'basic', label: 'Basic Viewer', description: 'Review images and measurements', icon: Eye},
  {id: 'segmentation', label: 'Segmentation Viewer', description: 'Create and edit segmentations', icon: Layers},
] as const;

interface LaunchWorkflowProps {
  studyUid: string;
  iconOnly?: boolean;
  className?: string;
}

/** STUDIES-WORKFLOW: share one explicit workflow choice across all study launch controls. */
export function LaunchWorkflow({studyUid, iconOnly = false, className}: LaunchWorkflowProps): ReactElement {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button type="button" variant={iconOnly ? 'ghost' : 'default'} size={iconOnly ? 'icon' : 'default'}
          aria-label="Launch workflow" title="Launch workflow"
          className={cn(iconOnly && 'h-8 w-8 text-muted hover:text-accent', className)}
          onClick={event => event.stopPropagation()}>
          <Eye className="h-4 w-4 shrink-0" aria-hidden="true" />
          {!iconOnly && <><span className="mx-2">Launch workflow</span><ChevronDown className="h-3.5 w-3.5" aria-hidden="true" /></>}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" sideOffset={8} className="w-72 rounded-xl p-1.5"
        onClick={event => event.stopPropagation()}>
        <DropdownMenuLabel className="px-3 text-[10px] uppercase tracking-wider">Launch workflow</DropdownMenuLabel>
        <DropdownMenuSeparator />
        {workflows.map(({id, label, description, icon: Icon}) => (
          <DropdownMenuItem key={id} className="gap-3 rounded-lg px-3 py-3 cursor-pointer"
            onSelect={() => openViewer(studyUid, id)}>
            <span className="grid h-9 w-9 shrink-0 place-items-center rounded-lg border border-hairline bg-raised text-accent">
              <Icon className="h-4 w-4" aria-hidden="true" />
            </span>
            <span className="min-w-0 flex-1">
              <span className="block text-[13px] font-semibold">{label}</span>
              <span className="block mt-0.5 text-[11px] text-muted">{description}</span>
            </span>
            <ArrowUpRight className="h-3.5 w-3.5 text-faint" aria-hidden="true" />
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
