interface IconProps {
  size?: number
}

const stroke = {
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.6,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const
}

export function ChatIcon({ size = 17 }: IconProps): React.JSX.Element {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...stroke}>
      <path d="M21 15a2 2 0 0 1-2 2H8l-5 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
    </svg>
  )
}

export function SlidersIcon({ size = 17 }: IconProps): React.JSX.Element {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...stroke}>
      <line x1="21" y1="5" x2="14" y2="5" />
      <line x1="10" y1="5" x2="3" y2="5" />
      <line x1="21" y1="12" x2="12" y2="12" />
      <line x1="8" y1="12" x2="3" y2="12" />
      <line x1="21" y1="19" x2="16" y2="19" />
      <line x1="12" y1="19" x2="3" y2="19" />
      <line x1="14" y1="3" x2="14" y2="7" />
      <line x1="8" y1="10" x2="8" y2="14" />
      <line x1="16" y1="17" x2="16" y2="21" />
    </svg>
  )
}

export function AgentIcon({ size = 17 }: IconProps): React.JSX.Element {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...stroke}>
      <rect x="4" y="7" width="16" height="12" rx="2.5" />
      <path d="M12 7V4" />
      <circle cx="12" cy="3.2" r="1" />
      <circle cx="9" cy="13" r="1.1" />
      <circle cx="15" cy="13" r="1.1" />
      <path d="M9.5 16.5h5" />
    </svg>
  )
}

export function TodoIcon({ size = 17 }: IconProps): React.JSX.Element {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...stroke}>
      <rect x="5" y="3.5" width="14" height="17" rx="2" />
      <path d="m8 9 1.5 1.5L12 7.8" />
      <path d="M13.5 9h2" />
      <path d="m8 15 1.5 1.5 2.5-2.7" />
      <path d="M13.5 15h2" />
    </svg>
  )
}

export function ComfyIcon({ size = 17 }: IconProps): React.JSX.Element {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...stroke}>
      <rect x="3" y="4" width="6" height="5" rx="1.3" />
      <rect x="15" y="15" width="6" height="5" rx="1.3" />
      <circle cx="6" cy="17.5" r="2.5" />
      <circle cx="18" cy="6.5" r="2.5" />
      <path d="M9 6.5h6M8.2 9l7.5 6.2M8.5 16.2 15.5 7.8" />
    </svg>
  )
}

export function FolderIcon({ size = 14 }: IconProps): React.JSX.Element {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...stroke}>
      <path d="M3 7a2 2 0 0 1 2-2h3.5l2 2H19a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
    </svg>
  )
}

export function FileIcon({ size = 13 }: IconProps): React.JSX.Element {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...stroke}>
      <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
      <path d="M14 3v5h5" />
    </svg>
  )
}

export function ChevronDownIcon({ size = 14 }: IconProps): React.JSX.Element {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...stroke}>
      <path d="m6 9 6 6 6-6" />
    </svg>
  )
}

export function RefreshIcon({ size = 14 }: IconProps): React.JSX.Element {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...stroke}>
      <path d="M21 12a9 9 0 1 1-2.64-6.36L21 8" />
      <path d="M21 3v5h-5" />
    </svg>
  )
}

export function GlobeIcon({ size = 13 }: IconProps): React.JSX.Element {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...stroke}>
      <circle cx="12" cy="12" r="9" />
      <path d="M3 12h18" />
      <path d="M12 3c2.5 2.4 3.8 5.6 3.8 9s-1.3 6.6-3.8 9c-2.5-2.4-3.8-5.6-3.8-9S9.5 5.4 12 3Z" />
    </svg>
  )
}

export function CodeIcon({ size = 13 }: IconProps): React.JSX.Element {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...stroke}>
      <path d="m9 8-4 4 4 4" />
      <path d="m15 8 4 4-4 4" />
    </svg>
  )
}

export function PanelRightIcon({ size = 16 }: IconProps): React.JSX.Element {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...stroke}>
      <rect x="3" y="4.5" width="18" height="15" rx="2.2" />
      <line x1="14.5" y1="4.5" x2="14.5" y2="19.5" />
    </svg>
  )
}

export function TrashIcon({ size = 15 }: IconProps): React.JSX.Element {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...stroke}>
      <path d="M4 7h16" />
      <path d="M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" />
      <path d="M6.5 7v12a2 2 0 0 0 2 2h7a2 2 0 0 0 2-2V7" />
      <path d="M10 11v6M14 11v6" />
    </svg>
  )
}

export function CloseIcon({ size = 15 }: IconProps): React.JSX.Element {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...stroke}>
      <path d="M6 6l12 12M6 18 18 6" />
    </svg>
  )
}

export function SearchIcon({ size = 13 }: IconProps): React.JSX.Element {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...stroke}>
      <circle cx="10.5" cy="10.5" r="6.5" />
      <path d="m20 20-4.7-4.7" />
    </svg>
  )
}

export function TerminalIcon({ size = 13 }: IconProps): React.JSX.Element {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...stroke}>
      <rect x="3" y="4.5" width="18" height="15" rx="2.2" />
      <path d="m7 9 3 3-3 3" />
      <path d="M13 15h4" />
    </svg>
  )
}

export function EditIcon({ size = 13 }: IconProps): React.JSX.Element {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...stroke}>
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z" />
    </svg>
  )
}

/** 고정 핀. filled=true면 채워진(고정됨) 상태. */
export function PinIcon({ size = 13, filled = false }: IconProps & { filled?: boolean }): React.JSX.Element {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      {...stroke}
      fill={filled ? 'currentColor' : 'none'}
    >
      <path d="M9 4h6l-1 6 3 3v2H7v-2l3-3z" />
      <path d="M12 15v5" />
    </svg>
  )
}

export function DatabaseIcon({ size = 13 }: IconProps): React.JSX.Element {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...stroke}>
      <ellipse cx="12" cy="5.5" rx="7.5" ry="2.8" />
      <path d="M4.5 5.5v6c0 1.55 3.36 2.8 7.5 2.8s7.5-1.25 7.5-2.8v-6" />
      <path d="M4.5 11.5v6c0 1.55 3.36 2.8 7.5 2.8s7.5-1.25 7.5-2.8v-6" />
    </svg>
  )
}

export function DownloadIcon({ size = 15 }: IconProps): React.JSX.Element {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...stroke}>
      <path d="M12 3v11" />
      <path d="m7.5 10.5 4.5 4.5 4.5-4.5" />
      <path d="M4.5 18.5v1a1 1 0 0 0 1 1h13a1 1 0 0 0 1-1v-1" />
    </svg>
  )
}

export function LinkIcon({ size = 13 }: IconProps): React.JSX.Element {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...stroke}>
      <path d="M10 13.8a4.5 4.5 0 0 0 6.36.04l2.08-2.08a4.5 4.5 0 0 0-6.36-6.36L10.9 6.56" />
      <path d="M14 10.2a4.5 4.5 0 0 0-6.36-.04l-2.08 2.08a4.5 4.5 0 0 0 6.36 6.36l1.18-1.18" />
    </svg>
  )
}

export function UnlinkIcon({ size = 13 }: IconProps): React.JSX.Element {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...stroke}>
      <path d="m9 15-1.36 1.36a4.5 4.5 0 1 1-6.36-6.36l2.08-2.08a4.5 4.5 0 0 1 6.36 0" />
      <path d="m15 9 1.36-1.36a4.5 4.5 0 1 1 6.36 6.36L20.64 16.08a4.5 4.5 0 0 1-6.36 0" />
      <path d="m4 20 16-16" />
    </svg>
  )
}

export function GraphIcon({ size = 17 }: IconProps): React.JSX.Element {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" {...stroke}>
      <circle cx="6" cy="7" r="2.4" />
      <circle cx="18" cy="6" r="2.4" />
      <circle cx="12" cy="18" r="2.7" />
      <path d="m8.2 7.1 7.45-0.75M7.45 9.1l3.1 6.2M16.6 8.3l-3 6.6" />
    </svg>
  )
}
