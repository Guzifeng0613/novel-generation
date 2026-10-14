import type {
  ApiProblem,
  Artifact,
  Chapter,
  ContinuationInput,
  ImportInspection,
  OutlineChapter,
  ProjectDetail,
  ProjectSummary,
  PromptVersionStatus,
  ProviderState,
  ProviderStatus,
  ReviewIssue,
  ReviewReference,
  ReviewSeverity,
  RoutingPreset,
  RunDetail,
  RunEvent,
  RunKind,
  RunMetrics,
  RunPlan,
  RunStatus,
  RunSummary,
  StoryFact,
  StoryState,
  SummarizerInput,
  TaskSummary,
} from './types'

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, '') ?? ''

function uploadFileName(file: File) {
  return file.webkitRelativePath || file.name
}

type UnknownRecord = Record<string, unknown>

export class ApiError extends Error implements ApiProblem {
  status: number
  detail?: string

  constructor(status: number, message: string, detail?: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

function record(value: unknown): UnknownRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as UnknownRecord) : {}
}

function array(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

function str(value: unknown, fallback = ''): string {
  return typeof value === 'string' || typeof value === 'number' ? String(value) : fallback
}

function num(value: unknown): number | undefined {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : undefined
  }
  return undefined
}

function bool(value: unknown): boolean {
  return value === true || value === 'true' || value === 1
}

export function normalizeImportInspection(payload: unknown): ImportInspection {
  const source = record(extractEntity(payload, 'inspection', 'data'))
  const capabilities = record(pick(source, 'recovery_capabilities', 'recoveryCapabilities'))
  return {
    kind: str(source.kind, 'raw_text'),
    inferredTitle: str(pick(source, 'inferred_title', 'inferredTitle')),
    warnings: array(source.warnings).map((item) => str(item)).filter(Boolean),
    documentCount: num(pick(source, 'document_count', 'documentCount')) ?? 0,
    totalBytes: num(pick(source, 'total_bytes', 'totalBytes')) ?? 0,
    documents: array(source.documents).map((item) => {
      const document = record(item)
      return {
        sourceName: str(pick(document, 'source_name', 'sourceName')),
        encoding: str(document.encoding, 'unknown'),
        byteSize: num(pick(document, 'byte_size', 'byteSize')) ?? 0,
        text: str(pick(document, 'text', 'content')),
      }
    }),
    dimensionsFound: array(pick(source, 'dimensions_found', 'dimensionsFound'))
      .map((item) => str(item))
      .filter(Boolean),
    recoverable: bool(source.recoverable),
    recoveryCapabilities: Object.fromEntries(
      Object.entries(capabilities).map(([key, value]) => [key, bool(value)]),
    ),
  }
}

const STRUCTURED_LABELS: Record<string, string> = {
  pov_strategy: '视角策略',
  rhythm_curve: '节奏曲线',
  dialogue_rules: '对白规则',
  description_rules: '描写规则',
  originality_rules: '原创性规则',
}

function structuredText(value: unknown): string {
  if (value === undefined || value === null) return ''
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (Array.isArray(value)) return value.map(structuredText).filter(Boolean).join('；')
  const source = record(value)
  return Object.entries(source)
    .filter(([key]) => !['schema_version', 'id', 'plan_id', 'run_id'].includes(key))
    .map(([key, item]) => {
      const detail = structuredText(item)
      return detail ? `${STRUCTURED_LABELS[key] ?? key}：${detail}` : ''
    })
    .filter(Boolean)
    .join('\n')
}

function pick(source: UnknownRecord, ...keys: string[]): unknown {
  for (const key of keys) {
    if (source[key] !== undefined && source[key] !== null) return source[key]
  }
  return undefined
}

function extractList(payload: unknown, ...keys: string[]): unknown[] {
  if (Array.isArray(payload)) return payload
  const source = record(payload)
  for (const key of keys) {
    if (Array.isArray(source[key])) return source[key] as unknown[]
  }
  return []
}

function normalizeRunStatus(value: unknown): RunStatus {
  const raw = str(value, 'draft').toLowerCase().replaceAll('-', '_')
  const aliases: Record<string, RunStatus> = {
    pending: 'queued',
    processing: 'running',
    waiting_approval: 'awaiting_outline_approval',
    awaiting_approval: 'awaiting_outline_approval',
    outline_ready: 'awaiting_outline_approval',
    success: 'completed',
    succeeded: 'completed',
    complete: 'completed',
    canceled: 'cancelled',
    human_review: 'needs_human',
  }
  const normalized = aliases[raw] ?? raw
  const allowed = new Set<RunStatus>([
    'draft',
    'awaiting_outline_approval',
    'queued',
    'running',
    'pausing',
    'paused',
    'completed',
    'failed',
    'cancelled',
    'cancelling',
    'needs_human',
    'indeterminate',
  ])
  return allowed.has(normalized as RunStatus) ? (normalized as RunStatus) : 'draft'
}

function normalizeRunKind(value: unknown): RunKind {
  return str(value).toLowerCase().includes('analysis') ? 'analysis' : 'continuation'
}

function normalizeArtifact(value: unknown, index = 0): Artifact {
  const source = record(value)
  return {
    id: str(pick(source, 'id', 'artifact_id'), `artifact-${index}`),
    title: str(pick(source, 'title', 'name', 'filename', 'kind'), '未命名产物'),
    kind: str(pick(source, 'kind', 'artifact_type', 'type'), 'artifact'),
    status: str(pick(source, 'status', 'state')) || undefined,
    version: (pick(source, 'version', 'revision') as number | string | undefined) ?? undefined,
    createdAt: str(pick(source, 'created_at', 'createdAt')) || undefined,
    size: num(pick(source, 'size', 'size_bytes')),
    mediaType: str(pick(source, 'media_type', 'mediaType')) || undefined,
    sha256: str(pick(source, 'sha256', 'content_hash')) || undefined,
  }
}

function normalizeFact(value: unknown, index = 0, category?: string): StoryFact {
  if (typeof value === 'string') {
    return { id: `${category ?? 'fact'}-${index}`, title: value, category }
  }
  const source = record(value)
  const title = str(pick(source, 'title', 'name', 'canonical_name', 'label', 'statement', 'summary', 'description'), '未命名条目')
  const detail = str(pick(source, 'detail', 'description', 'content', 'value', 'subject', 'stakes', 'follow_up')) || undefined
  return {
    id: str(pick(source, 'id', 'fact_id', 'entity_id'), `${category ?? 'fact'}-${index}`),
    title,
    detail: detail === title ? undefined : detail,
    category: str(pick(source, 'category', 'type'), category) || undefined,
    status: str(pick(source, 'status', 'state')) || undefined,
    source: str(pick(source, 'source', 'source_ref', 'evidence')) || undefined,
  }
}

function normalizeStoryState(value: unknown): StoryState | undefined {
  const source = record(value)
  if (Object.keys(source).length === 0) return undefined
  const facts = (key: string, ...aliases: string[]) =>
    extractList(pickRecord(source, key, ...aliases), 'items').map((item, index) => normalizeFact(item, index, key))

  return {
    canon: facts('canon', 'facts', 'canonical_facts'),
    suspense: facts('suspense', 'mysteries', 'hooks'),
    entities: facts('entities', 'characters', 'locations'),
    conflicts: facts('conflicts', 'conflict'),
    worldRules: facts('world_rules', 'worldRules', 'rules'),
  }
}

function pickRecord(source: UnknownRecord, ...keys: string[]): unknown {
  const value = pick(source, ...keys)
  if (Array.isArray(value)) return value
  if (value && typeof value === 'object') return value
  return []
}

export function normalizeProjectSummary(value: unknown): ProjectSummary {
  const source = record(value)
  return {
    id: str(pick(source, 'id', 'project_id')),
    name: str(pick(source, 'name', 'title', 'project_name'), '未命名小说'),
    status: str(pick(source, 'status', 'state')) || undefined,
    createdAt: str(pick(source, 'created_at', 'createdAt')) || undefined,
    updatedAt: str(pick(source, 'updated_at', 'updatedAt')) || undefined,
    sourceCount: num(pick(source, 'source_count', 'sourceCount', 'sources_count')),
    latestRunId: str(pick(source, 'latest_run_id', 'latestRunId')) || undefined,
  }
}

export function normalizeRunSummary(value: unknown): RunSummary {
  const source = record(value)
  return {
    id: str(pick(source, 'id', 'run_id')),
    projectId: str(pick(source, 'project_id', 'projectId')) || undefined,
    kind: normalizeRunKind(pick(source, 'kind', 'run_type', 'type')),
    status: normalizeRunStatus(pick(source, 'status', 'state')),
    stage: str(pick(source, 'stage', 'current_stage', 'phase')) || undefined,
    createdAt: str(pick(source, 'created_at', 'createdAt')) || undefined,
    updatedAt: str(pick(source, 'updated_at', 'updatedAt')) || undefined,
    routingPreset: str(pick(source, 'routing_preset', 'routingPreset')) as RoutingPreset | undefined,
    targetChars: num(pick(source, 'target_chars', 'targetChars')),
    branchName: str(pick(source, 'branch_name', 'branchName')) || undefined,
  }
}

export function normalizeProjectDetail(value: unknown): ProjectDetail {
  const source = record(value)
  const base = normalizeProjectSummary(source)
  const sources = extractList(source.sources, 'items').map(normalizeArtifact)
  const artifacts = extractList(source.artifacts, 'items').map(normalizeArtifact)
  return {
    ...base,
    description: str(pick(source, 'description', 'summary')) || undefined,
    activeBranch: str(pick(source, 'active_branch', 'activeBranch'), 'main'),
    sources,
    artifacts,
    storyState: normalizeStoryState(pick(source, 'story_state', 'storyState', 'memory')),
    runs: extractList(pick(source, 'runs', 'run_history'), 'items').map(normalizeRunSummary),
  }
}

function normalizeOutlineChapter(value: unknown, index = 0): OutlineChapter {
  const source = record(value)
  return {
    id: str(pick(source, 'id', 'chapter_id'), `outline-${index + 1}`),
    index: num(pick(source, 'index', 'chapter_index', 'number')) ?? index + 1,
    title: str(pick(source, 'title', 'name'), `第${index + 1}章`),
    summary: str(pick(source, 'summary', 'brief', 'description', 'content')),
    targetChars: num(pick(source, 'target_chars', 'targetChars')),
    pov: str(pick(source, 'pov', 'point_of_view')) || undefined,
    status: str(pick(source, 'status', 'state')) || undefined,
    dependencies: array(pick(source, 'dependencies', 'depends_on')).map((item) => str(item)).filter(Boolean),
    expectedEndState: str(pick(source, 'expected_end_state', 'expectedEndState', 'end_state')) || undefined,
    canonConflicts: array(pick(source, 'canon_conflicts', 'canonConflicts', 'conflicts')).map((item) =>
      typeof item === 'string' ? item : str(pick(record(item), 'explanation', 'requested_change', 'message', 'detail', 'summary')),
    ).filter(Boolean),
  }
}

function normalizeRunPlan(value: unknown): RunPlan | undefined {
  const source = record(value)
  if (Object.keys(source).length === 0) return undefined
  return {
    id: str(pick(source, 'id', 'plan_id')) || undefined,
    version: (pick(source, 'version', 'revision') as number | string | undefined) ?? undefined,
    approvedHash: str(pick(source, 'approved_hash', 'approvedHash', 'plan_hash')) || undefined,
    planHash: str(pick(source, 'plan_hash', 'planHash', 'content_hash')) || undefined,
    status: str(pick(source, 'status', 'state')) || undefined,
    premise: str(pick(source, 'premise', 'starting_point', 'summary')) || undefined,
    styleCard: structuredText(pick(source, 'style_card', 'styleCard', 'style')) || undefined,
    userRequirements: str(pick(source, 'user_requirements', 'userRequirements')) || undefined,
    canonConflicts: array(pick(source, 'canon_conflicts', 'canonConflicts')).map((item) =>
      typeof item === 'string' ? item : str(pick(record(item), 'explanation', 'requested_change', 'message', 'detail', 'summary')),
    ).filter(Boolean),
    chapters: extractList(pick(source, 'chapters', 'chapter_briefs'), 'items').map(normalizeOutlineChapter),
  }
}

function normalizeChapter(value: unknown, index = 0): Chapter {
  const source = record(value)
  return {
    id: str(pick(source, 'id', 'chapter_id'), `chapter-${index + 1}`),
    index: num(pick(source, 'index', 'chapter_index', 'number')) ?? index + 1,
    title: str(pick(source, 'title', 'name'), `第${index + 1}章`),
    status: str(pick(source, 'status', 'state')) || undefined,
    content: str(pick(source, 'content', 'text', 'body')) || undefined,
    previousContent: str(pick(source, 'previous_content', 'previousContent', 'before')) || undefined,
    targetChars: num(pick(source, 'target_chars', 'targetChars')),
    actualChars: num(pick(source, 'actual_chars', 'actualChars', 'char_count')),
    revision: num(pick(source, 'revision', 'revision_count')),
    pov: str(pick(source, 'pov', 'point_of_view')) || undefined,
    contentSha256: str(pick(source, 'content_sha256', 'contentSha256')) || undefined,
    versionSource: str(pick(source, 'version_source', 'versionSource')) || undefined,
    humanEdited: pick(source, 'human_edited', 'humanEdited') === undefined
      ? undefined
      : bool(pick(source, 'human_edited', 'humanEdited')),
    editable: source.editable === undefined ? undefined : bool(source.editable),
  }
}

function normalizeSeverity(value: unknown): ReviewSeverity {
  const raw = str(value, 'note').toLowerCase()
  if (raw === 'critical' || raw === 'error') return 'blocker'
  if (raw === 'warning') return 'major'
  if (raw === 'info') return 'note'
  return ['blocker', 'major', 'minor', 'note'].includes(raw) ? (raw as ReviewSeverity) : 'note'
}

function normalizeReviewReference(value: unknown, index = 0): ReviewReference {
  const source = record(value)
  return {
    id: str(pick(source, 'id', 'fact_id', 'mystery_id', 'foreshadowing_id'), `ref-${index + 1}`),
    kind: str(pick(source, 'kind', 'type'), 'canon'),
    title: str(pick(source, 'title', 'statement', 'label', 'description'), '未命名依据'),
    detail: str(pick(source, 'detail', 'category', 'status', 'follow_up')) || undefined,
  }
}

function normalizeIssue(value: unknown, index = 0): ReviewIssue {
  const source = record(value)
  return {
    id: str(pick(source, 'id', 'issue_id'), `issue-${index + 1}`),
    severity: normalizeSeverity(pick(source, 'severity', 'level')),
    dimension: str(pick(source, 'dimension', 'category', 'review_key'), '综合'),
    message: str(pick(source, 'message', 'description', 'problem', 'title'), '未提供问题描述'),
    title: str(pick(source, 'title', 'name')) || undefined,
    evidence: str(pick(source, 'evidence', 'quote')) || undefined,
    instruction: str(pick(source, 'instruction', 'suggestion', 'fix')) || undefined,
    location: str(pick(source, 'location', 'range')) || undefined,
    startOffset: num(pick(source, 'start_offset', 'startOffset')),
    endOffset: num(pick(source, 'end_offset', 'endOffset')),
    references: extractList(pick(source, 'references', 'refs'), 'items').map(normalizeReviewReference),
    status: str(pick(source, 'status', 'state'), 'open'),
    chapterId: str(pick(source, 'chapter_id', 'chapterId')) || undefined,
    stale: source.stale === undefined ? undefined : bool(source.stale),
  }
}

function normalizeMetrics(value: unknown, root: UnknownRecord): RunMetrics {
  const source = record(value)
  const budget = record(pick(root, 'budget', 'budget_usage'))
  return {
    queued: num(pick(source, 'queued', 'queued_tasks')),
    active: num(pick(source, 'active', 'active_tasks', 'running')),
    completed: num(pick(source, 'completed', 'completed_tasks')),
    calls: num(pick(source, 'calls', 'call_count', 'api_calls', 'total_calls')) ?? num(pick(budget, 'calls_used')),
    tokens: num(pick(source, 'tokens', 'total_tokens', 'tokens_used')) ?? num(pick(budget, 'tokens_used')),
    cost: num(pick(source, 'cost', 'cost_used', 'total_cost')) ?? num(pick(budget, 'cost_used')),
    maxCost: num(pick(source, 'max_cost', 'maxCost')) ?? num(pick(budget, 'max_cost')),
    elapsedSeconds: num(pick(source, 'elapsed_seconds', 'elapsedSeconds', 'duration_seconds')),
    warning: str(pick(source, 'warning', 'budget_warning')) || undefined,
  }
}

function normalizeTask(value: unknown, index = 0): TaskSummary {
  const source = record(value)
  return {
    id: str(pick(source, 'id', 'task_id'), `task-${index + 1}`),
    operation: str(pick(source, 'operation', 'task_type'), 'unknown'),
    status: str(pick(source, 'status', 'state'), 'pending'),
    sequenceIndex: num(pick(source, 'sequence_index', 'sequenceIndex')),
    provider: str(source.provider) || undefined,
    model: str(source.model) || undefined,
    attemptCount: num(pick(source, 'attempt_count', 'attemptCount')) ?? 0,
    maxAttempts: num(pick(source, 'max_attempts', 'maxAttempts')) ?? 0,
    retryable: bool(source.retryable),
    requiresAcknowledgement: bool(
      pick(source, 'requires_acknowledgement', 'requiresAcknowledgement'),
    ),
    errorCode: str(pick(source, 'error_code', 'errorCode')) || undefined,
    errorMessage: str(pick(source, 'error_message', 'errorMessage')) || undefined,
    updatedAt: str(pick(source, 'updated_at', 'updatedAt')) || undefined,
  }
}

export function normalizeRunDetail(value: unknown): RunDetail {
  const source = record(value)
  const base = normalizeRunSummary(source)
  const review = record(pick(source, 'review', 'review_report', 'qa_report'))
  const artifacts = extractList(pick(source, 'artifacts', 'outputs'), 'items').map(normalizeArtifact)
  const plan = normalizeRunPlan(pick(source, 'plan', 'outline', 'run_plan'))
  if (plan && !plan.planHash) {
    plan.planHash = artifacts.find((artifact) => artifact.kind === 'run_plan')?.sha256
  }
  return {
    ...base,
    plan,
    chapters: extractList(pick(source, 'chapters', 'chapter_outputs'), 'items').map(normalizeChapter),
    reviewIssues: extractList(pick(source, 'review_issues', 'issues', 'reviews'), 'items').length > 0
      ? extractList(pick(source, 'review_issues', 'issues', 'reviews'), 'items').map(normalizeIssue)
      : extractList(pick(review, 'issues', 'review_issues'), 'items').map(normalizeIssue),
    tasks: extractList(pick(source, 'tasks', 'task_summaries'), 'items').map(normalizeTask),
    artifacts,
    metrics: normalizeMetrics(pick(source, 'metrics', 'usage', 'progress'), source),
    message: str(pick(source, 'message', 'status_message')) || undefined,
    error: str(pick(source, 'error', 'error_message')) || undefined,
    contentRevisionInvalidated: pick(source, 'content_revision_invalidated', 'contentRevisionInvalidated') === undefined
      ? undefined
      : bool(pick(source, 'content_revision_invalidated', 'contentRevisionInvalidated')),
    deliveryValid: pick(source, 'delivery_valid', 'deliveryValid') === undefined
      ? undefined
      : bool(pick(source, 'delivery_valid', 'deliveryValid')),
  }
}

export function normalizeProviderStatus(value: unknown): ProviderStatus {
  const source = record(value)
  let entries = extractList(pick(source, 'providers', 'items'), 'items')
  if (entries.length === 0) {
    entries = ['openai', 'deepseek'].flatMap((id) => {
      const provider = source[id]
      return provider && typeof provider === 'object' ? [{ ...record(provider), id }] : []
    })
  }
  const providers: ProviderState[] = entries.map((item, index) => {
    const entry = record(item)
    const id = str(pick(entry, 'id', 'provider'), `provider-${index}`)
    return {
      id,
      name: str(pick(entry, 'name', 'label'), id === 'openai' ? 'OpenAI' : id === 'deepseek' ? 'DeepSeek' : id),
      configured: bool(pick(entry, 'configured', 'has_key', 'enabled')),
      available: pick(entry, 'available', 'healthy') === undefined ? undefined : bool(pick(entry, 'available', 'healthy')),
      model: str(pick(entry, 'model', 'default_model')) || undefined,
      message: str(pick(entry, 'message', 'detail')) || undefined,
    }
  })
  return { providers }
}

export function normalizePromptVersions(value: unknown): PromptVersionStatus {
  return {
    prompts: extractList(value, 'prompts', 'items').map((item, index) => {
      const source = record(item)
      return {
        file: str(pick(source, 'file', 'filename'), `prompt-${index + 1}.md`),
        name: str(pick(source, 'name', 'title'), '未命名提示词'),
        version: str(pick(source, 'version'), '0'),
        sha256: str(pick(source, 'sha256', 'content_hash')),
      }
    }),
  }
}

async function parseResponse(response: Response): Promise<unknown> {
  if (response.status === 204) return undefined
  const contentType = response.headers.get('content-type') ?? ''
  if (contentType.includes('application/json')) return response.json()
  return response.text()
}

async function request(path: string, init?: RequestInit): Promise<unknown> {
  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: init?.body instanceof FormData
        ? init.headers
        : { 'Content-Type': 'application/json', Accept: 'application/json', ...init?.headers },
    })
  } catch (error) {
    throw new ApiError(0, '无法连接本地编排服务', error instanceof Error ? error.message : undefined)
  }
  const payload = await parseResponse(response)
  if (!response.ok) {
    const body = record(payload)
    const detail = str(pick(body, 'detail', 'error', 'message')) || (typeof payload === 'string' ? payload : undefined)
    throw new ApiError(response.status, detail || `请求失败（${response.status}）`, detail)
  }
  return payload
}

function extractEntity(payload: unknown, ...keys: string[]): unknown {
  const source = record(payload)
  for (const key of keys) {
    if (source[key] && typeof source[key] === 'object') return source[key]
  }
  return payload
}

export const api = {
  async health(): Promise<boolean> {
    await request('/api/health')
    return true
  },

  async listProjects(signal?: AbortSignal): Promise<ProjectSummary[]> {
    const payload = await request('/api/projects', { signal })
    return extractList(payload, 'projects', 'items').map(normalizeProjectSummary).filter((item) => item.id)
  },

  async getProject(id: string, signal?: AbortSignal): Promise<ProjectDetail> {
    const payload = await request(`/api/projects/${encodeURIComponent(id)}`, { signal })
    return normalizeProjectDetail(extractEntity(payload, 'project', 'data'))
  },

  async importProject(files: File[], name?: string, order?: string[]): Promise<ProjectDetail> {
    const body = new FormData()
    files.forEach((file) => body.append('files', file, uploadFileName(file)))
    if (name?.trim()) body.append('name', name.trim())
    if (order?.length) body.append('order', JSON.stringify(order))
    const payload = await request('/api/projects/import', { method: 'POST', body })
    return normalizeProjectDetail(extractEntity(payload, 'project', 'data'))
  },

  async inspectImport(files: File[]): Promise<ImportInspection> {
    const body = new FormData()
    files.forEach((file) => body.append('files', file, uploadFileName(file)))
    const payload = await request('/api/projects/import/inspect', { method: 'POST', body })
    return normalizeImportInspection(payload)
  },

  async startAnalysis(projectId: string, input: SummarizerInput): Promise<RunDetail> {
    const payload = await request(`/api/summarizer/projects/${encodeURIComponent(projectId)}/runs`, {
      method: 'POST',
      body: JSON.stringify({
        splitter_enabled: input.splitterEnabled,
        routing_preset: input.routingPreset,
        ...(input.maxCost === undefined ? {} : { max_cost: input.maxCost }),
        ...(input.maxTokens === undefined ? {} : { max_tokens: input.maxTokens }),
        ...(input.maxCalls === undefined ? {} : { max_calls: input.maxCalls }),
      }),
    })
    return normalizeRunDetail(extractEntity(payload, 'run', 'data'))
  },

  async startContinuation(projectId: string, input: ContinuationInput): Promise<RunDetail> {
    const payload = await request(`/api/continuator/projects/${encodeURIComponent(projectId)}/runs`, {
      method: 'POST',
      body: JSON.stringify({
        target_chars: input.targetChars,
        user_requirements: input.userRequirements,
        routing_preset: input.routingPreset,
        ...(input.maxCost === undefined ? {} : { max_cost: input.maxCost }),
        ...(input.branchName ? { branch_name: input.branchName } : {}),
      }),
    })
    return normalizeRunDetail(extractEntity(payload, 'run', 'data'))
  },

  async getRun(id: string, signal?: AbortSignal): Promise<RunDetail> {
    const payload = await request(`/api/runs/${encodeURIComponent(id)}`, { signal })
    return normalizeRunDetail(extractEntity(payload, 'run', 'data'))
  },

  async approveOutline(
    id: string,
    options: { planHash?: string; allowCanonOverrides?: boolean } = {},
  ): Promise<RunDetail> {
    const payload = await request(`/api/runs/${encodeURIComponent(id)}/outline/approve`, {
      method: 'POST',
      body: JSON.stringify({
        ...(options.planHash ? { plan_hash: options.planHash } : {}),
        allow_canon_overrides: options.allowCanonOverrides === true,
      }),
    })
    return normalizeRunDetail(extractEntity(payload, 'run', 'data'))
  },

  async reviseOutline(id: string, feedback: string, chapterId?: string): Promise<RunDetail> {
    const payload = await request(`/api/runs/${encodeURIComponent(id)}/outline/revise`, {
      method: 'POST',
      body: JSON.stringify({ feedback, ...(chapterId ? { chapter_id: chapterId } : {}) }),
    })
    return normalizeRunDetail(extractEntity(payload, 'run', 'data'))
  },

  async controlRun(id: string, action: 'pause' | 'resume' | 'cancel'): Promise<RunDetail> {
    const payload = await request(`/api/runs/${encodeURIComponent(id)}/${action}`, {
      method: 'POST',
      body: JSON.stringify({}),
    })
    return normalizeRunDetail(extractEntity(payload, 'run', 'data'))
  },

  async retryTask(
    runId: string,
    taskId: string,
    options: { acknowledgeIndeterminate?: boolean } = {},
  ): Promise<RunDetail> {
    const payload = await request(`/api/runs/${encodeURIComponent(runId)}/tasks/${encodeURIComponent(taskId)}/retry`, {
      method: 'POST',
      body: JSON.stringify({
        acknowledge_indeterminate: options.acknowledgeIndeterminate === true,
      }),
    })
    return normalizeRunDetail(extractEntity(payload, 'run', 'data'))
  },

  async updateChapterContent(
    runId: string,
    chapterId: string,
    input: { content: string; baseRevision: number; baseSha256: string; reason?: string },
  ): Promise<RunDetail> {
    const payload = await request(`/api/runs/${encodeURIComponent(runId)}/chapters/${encodeURIComponent(chapterId)}/content`, {
      method: 'PATCH',
      body: JSON.stringify({
        content: input.content,
        base_revision: input.baseRevision,
        base_sha256: input.baseSha256,
        reason: input.reason ?? '',
      }),
    })
    return normalizeRunDetail(extractEntity(payload, 'run', 'data'))
  },

  async providers(signal?: AbortSignal): Promise<ProviderStatus> {
    const payload = await request('/api/providers/status', { signal })
    return normalizeProviderStatus(payload)
  },

  async saveProviderKeys(keys: { openaiApiKey?: string; deepseekApiKey?: string }): Promise<ProviderStatus> {
    const payload = await request('/api/providers/keys', {
      method: 'POST',
      body: JSON.stringify({
        ...(keys.openaiApiKey?.trim() ? { openai_api_key: keys.openaiApiKey.trim() } : {}),
        ...(keys.deepseekApiKey?.trim() ? { deepseek_api_key: keys.deepseekApiKey.trim() } : {}),
      }),
    })
    return normalizeProviderStatus(payload)
  },

  async promptVersions(signal?: AbortSignal): Promise<PromptVersionStatus> {
    const payload = await request('/api/prompts/versions', { signal })
    return normalizePromptVersions(payload)
  },

  artifactDownloadUrl(id: string): string {
    return `${API_BASE}/api/artifacts/${encodeURIComponent(id)}/download`
  },

  artifactPreviewUrl(id: string): string {
    return `${API_BASE}/api/artifacts/${encodeURIComponent(id)}/preview`
  },

  eventSource(runId: string): EventSource {
    return new EventSource(`${API_BASE}/api/runs/${encodeURIComponent(runId)}/events`)
  },
}

export function parseRunEvent(raw: string, eventType = 'message'): RunEvent {
  try {
    const payload = JSON.parse(raw) as unknown
    const source = record(payload)
    const maybeRun = pick(source, 'run', 'snapshot')
    return {
      type: str(pick(source, 'type', 'event'), eventType),
      run: maybeRun && typeof maybeRun === 'object' ? normalizeRunDetail(maybeRun) : undefined,
      message: str(pick(source, 'message', 'detail')) || undefined,
      payload,
    }
  } catch {
    return { type: eventType, message: raw }
  }
}
