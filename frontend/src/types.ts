export type RunStatus =
  | 'draft'
  | 'awaiting_outline_approval'
  | 'queued'
  | 'running'
  | 'pausing'
  | 'paused'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'cancelling'
  | 'needs_human'
  | 'indeterminate'

export type RunKind = 'analysis' | 'continuation'
export type RoutingPreset = 'openai-only' | 'deepseek-only' | 'hybrid-quality'

export interface ProjectSummary {
  id: string
  name: string
  status?: string
  createdAt?: string
  updatedAt?: string
  sourceCount?: number
  latestRunId?: string
}

export interface ImportInspectionDocument {
  sourceName: string
  encoding: string
  byteSize: number
  text: string
}

export interface ImportInspection {
  kind: 'raw_text' | 'summary_bundle' | 'project_bundle' | string
  inferredTitle: string
  warnings: string[]
  documentCount: number
  totalBytes: number
  documents: ImportInspectionDocument[]
  dimensionsFound: string[]
  recoverable: boolean
  recoveryCapabilities: Record<string, boolean>
}

export interface Artifact {
  id: string
  title: string
  kind: string
  status?: string
  version?: number | string
  createdAt?: string
  size?: number
  mediaType?: string
  sha256?: string
}

export interface StoryFact {
  id: string
  title: string
  detail?: string
  category?: string
  status?: string
  source?: string
}

export interface StoryState {
  canon: StoryFact[]
  suspense: StoryFact[]
  entities: StoryFact[]
  conflicts: StoryFact[]
  worldRules: StoryFact[]
}

export interface ProjectDetail extends ProjectSummary {
  description?: string
  activeBranch?: string
  sources: Artifact[]
  artifacts: Artifact[]
  storyState?: StoryState
  runs: RunSummary[]
}

export interface RunSummary {
  id: string
  projectId?: string
  kind: RunKind
  status: RunStatus
  stage?: string
  createdAt?: string
  updatedAt?: string
  routingPreset?: RoutingPreset | string
  targetChars?: number
  branchName?: string
}

export interface OutlineChapter {
  id: string
  index: number
  title: string
  summary: string
  targetChars?: number
  pov?: string
  status?: string
  dependencies: string[]
  expectedEndState?: string
  canonConflicts: string[]
}

export interface RunPlan {
  id?: string
  version?: number | string
  approvedHash?: string
  planHash?: string
  status?: string
  premise?: string
  styleCard?: string
  userRequirements?: string
  canonConflicts: string[]
  chapters: OutlineChapter[]
}

export interface Chapter {
  id: string
  index: number
  title: string
  status?: string
  content?: string
  previousContent?: string
  targetChars?: number
  actualChars?: number
  revision?: number
  pov?: string
  contentSha256?: string
  versionSource?: string
  humanEdited?: boolean
  editable?: boolean
}

export type ReviewSeverity = 'blocker' | 'major' | 'minor' | 'note'

export interface ReviewReference {
  id: string
  kind: 'canon' | 'suspense' | string
  title: string
  detail?: string
}

export interface ReviewIssue {
  id: string
  severity: ReviewSeverity
  dimension: string
  message: string
  title?: string
  evidence?: string
  instruction?: string
  location?: string
  startOffset?: number
  endOffset?: number
  references: ReviewReference[]
  status?: 'open' | 'resolved' | string
  chapterId?: string
  stale?: boolean
}

export interface RunMetrics {
  queued?: number
  active?: number
  completed?: number
  calls?: number
  tokens?: number
  cost?: number
  maxCost?: number
  elapsedSeconds?: number
  warning?: string
}

export interface TaskSummary {
  id: string
  operation: string
  status: string
  sequenceIndex?: number
  provider?: string
  model?: string
  attemptCount: number
  maxAttempts: number
  retryable: boolean
  requiresAcknowledgement: boolean
  errorCode?: string
  errorMessage?: string
  updatedAt?: string
}

export interface RunDetail extends RunSummary {
  plan?: RunPlan
  chapters: Chapter[]
  reviewIssues: ReviewIssue[]
  tasks: TaskSummary[]
  artifacts: Artifact[]
  metrics: RunMetrics
  message?: string
  error?: string
  contentRevisionInvalidated?: boolean
  deliveryValid?: boolean
}

export interface ProviderState {
  id: 'openai' | 'deepseek' | string
  name: string
  configured: boolean
  available?: boolean
  model?: string
  message?: string
}

export interface ProviderStatus {
  providers: ProviderState[]
}

export interface PromptVersion {
  file: string
  name: string
  version: string
  sha256: string
}

export interface PromptVersionStatus {
  prompts: PromptVersion[]
}

export interface ContinuationInput {
  targetChars: number
  userRequirements: string
  routingPreset: RoutingPreset
  maxCost?: number
  branchName?: string
}

export interface SummarizerInput {
  splitterEnabled: boolean
  routingPreset: RoutingPreset
  maxCost?: number
  maxTokens?: number
  maxCalls?: number
}

export interface ApiProblem {
  status: number
  message: string
  detail?: string
}

export interface RunEvent {
  type: string
  run?: RunDetail
  message?: string
  payload?: unknown
}

export const ACTIVE_RUN_STATUSES = new Set<RunStatus>(['queued', 'running', 'pausing', 'cancelling'])

export const WORKFLOW_STAGES = [
  { id: 'ingest', label: '素材入库', short: '入库' },
  { id: 'analysis', label: '拆分 / 总结', short: '总结' },
  { id: 'outline', label: '章纲审批', short: '章纲' },
  { id: 'writing', label: '分章生产', short: '写作' },
  { id: 'review', label: '总编审校', short: '审校' },
  { id: 'delivery', label: '交付', short: '交付' },
] as const
