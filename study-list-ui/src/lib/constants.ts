export const MOD_COLORS: Record<string, string> = {
  CT: '#38bdf8', MR: '#a78bfa', CR: '#fbbf24', DX: '#fb923c',
  US: '#34d399', NM: '#fb7185', PT: '#f472b6', XA: '#22d3ee',
  MG: '#e879f9', OT: '#94a3b8',
};

export const MOD_LABELS: Record<string, string> = {
  CT: 'Computed Tomography', MR: 'Magnetic Resonance', CR: 'Computed Radiography',
  DX: 'Digital X-Ray', US: 'Ultrasound', NM: 'Nuclear Medicine',
  PT: 'PET Scan', XA: 'X-Ray Angiography', MG: 'Mammography', OT: 'Other',
};

export const STATUS_FILTERS = [
  { key: '', label: 'All', icon: null },
  { key: 'ONLINE', label: 'Online', icon: null },
  { key: 'ARCHIVED', label: 'Archived', icon: null },
] as const;

export const SPECIAL_FILTERS = [
  { key: 'critical', label: 'Critical', emoji: '🚨' },
  { key: 'reported', label: 'Reported', emoji: '📄' },
  { key: 'oru', label: 'EMR', emoji: '➤' },
  { key: 'his', label: 'HIS', emoji: '🏥' },
] as const;

export function getModColor(mod: string): string {
  return MOD_COLORS[mod.toUpperCase()] || '#94a3b8';
}
