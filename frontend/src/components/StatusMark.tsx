import type { RunStatus } from '../types'

const labels: Record<RunStatus, string> = {
  draft: '草稿',
  awaiting_outline_approval: '待审批',
  queued: '排队中',
  running: '运行中',
  pausing: '正在暂停',
  paused: '已暂停',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
  cancelling: '正在取消',
  needs_human: '需人工处理',
  indeterminate: '状态不确定',
}

export function runStatusLabel(status: RunStatus) {
  return labels[status]
}

export function StatusMark({ status, compact = false }: { status: RunStatus; compact?: boolean }) {
  return (
    <span className={`status-mark status-mark--${status}`} title={labels[status]}>
      <span aria-hidden="true" className="status-mark__symbol">
        {status === 'completed' ? '✓'
          : status === 'running' || status === 'pausing' || status === 'cancelling' ? '↻'
            : status === 'failed' || status === 'needs_human' ? '!'
              : '•'}
      </span>
      {compact ? <span className="sr-only">{labels[status]}</span> : <span>{labels[status]}</span>}
    </span>
  )
}
