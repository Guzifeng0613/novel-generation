import {
  Archive,
  ArrowLeft,
  BookOpen,
  Check,
  ChevronDown,
  ChevronRight,
  ClipboardCheck,
  Clock3,
  Download,
  FileText,
  Folder,
  GitBranch,
  Layers3,
  Menu,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  Sparkles,
  Square,
  TriangleAlert,
  Upload,
  X,
  type LucideIcon,
  type LucideProps,
} from 'lucide-react'

export type IconName =
  | 'archive'
  | 'arrow-left'
  | 'book'
  | 'check'
  | 'chevron-down'
  | 'chevron-right'
  | 'clock'
  | 'close'
  | 'download'
  | 'file'
  | 'folder'
  | 'layers'
  | 'menu'
  | 'pause'
  | 'play'
  | 'plus'
  | 'refresh'
  | 'review'
  | 'route'
  | 'search'
  | 'settings'
  | 'spark'
  | 'stop'
  | 'upload'
  | 'warning'

interface IconProps extends Omit<LucideProps, 'ref'> {
  name: IconName
  size?: number
}

const icons: Record<IconName, LucideIcon> = {
  archive: Archive,
  'arrow-left': ArrowLeft,
  book: BookOpen,
  check: Check,
  'chevron-down': ChevronDown,
  'chevron-right': ChevronRight,
  clock: Clock3,
  close: X,
  download: Download,
  file: FileText,
  folder: Folder,
  layers: Layers3,
  menu: Menu,
  pause: Pause,
  play: Play,
  plus: Plus,
  refresh: RefreshCw,
  review: ClipboardCheck,
  route: GitBranch,
  search: Search,
  settings: Settings2,
  spark: Sparkles,
  stop: Square,
  upload: Upload,
  warning: TriangleAlert,
}

export function Icon({ name, size = 18, ...props }: IconProps) {
  const Component = icons[name]
  return <Component aria-hidden="true" size={size} strokeWidth={1.75} {...props} />
}
