import type { RunStatus } from './types'
import { WORKFLOW_STAGES } from './types'

const DATE_FORMATTER = new Intl.DateTimeFormat('zh-CN', {
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
})

const NUMBER_FORMATTER = new Intl.NumberFormat('zh-CN')

export function formatDate(value?: string): string {
  if (!value) return '时间未知'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : DATE_FORMATTER.format(date)
}

export function formatNumber(value?: number): string {
  return value === undefined ? '—' : NUMBER_FORMATTER.format(value)
}

export function formatCost(value?: number): string {
  return value === undefined ? '—' : `$${value.toFixed(value < 1 ? 3 : 2)}`
}

export function formatDuration(seconds?: number): string {
  if (seconds === undefined) return '—'
  if (seconds < 60) return `${Math.round(seconds)} 秒`
  const minutes = Math.floor(seconds / 60)
  const remainder = Math.round(seconds % 60)
  return `${minutes} 分 ${remainder} 秒`
}

const stageAliases: Record<string, string> = {
  import: 'ingest',
  importing: 'ingest',
  split: 'analysis',
  splitting: 'analysis',
  summarize: 'analysis',
  summarizing: 'analysis',
  planning: 'outline',
  outline_approval: 'outline',
  awaiting_outline_approval: 'outline',
  chapter: 'writing',
  revising: 'writing',
  reviewing: 'review',
  qa: 'review',
  audit: 'review',
  export: 'delivery',
  completed: 'delivery',
}

export function workflowStageIndex(stage?: string, status?: RunStatus): number {
  if (status === 'completed') return WORKFLOW_STAGES.length - 1
  if (!stage) return status === 'awaiting_outline_approval' ? 2 : 0
  const normalized = stage.toLowerCase().replaceAll('-', '_')
  const stageId = stageAliases[normalized] ?? normalized
  const index = WORKFLOW_STAGES.findIndex((item) => item.id === stageId)
  return index === -1 ? 0 : index
}

export function charCount(text?: string): number {
  return text?.replace(/\s/g, '').length ?? 0
}
