import { useEffect, useMemo, useState } from 'react'
import type { TaskSummary } from '../../types'
import { Button } from '../../components/Button'
import { Dialog } from '../../components/Dialog'
import { Icon } from '../../components/Icon'

const OPERATION_LABELS: Record<string, string> = {
  analyze_chunk: '分段分析',
  build_plan: '生成章纲',
  revise_plan: '修订章纲',
  write_chapter: '章节写作',
  revise_chapter: '章节定向修订',
  extract_chapter_delta: '提取章节状态',
  review_dimension_group: '九维审稿',
  consolidate_reviews: '汇总审稿',
  arbitrate_chapter: '主 A 仲裁',
  build_memory: '构建分支记忆',
}

function taskLabel(task: TaskSummary) {
  return OPERATION_LABELS[task.operation] ?? task.operation.replaceAll('_', ' ')
}

export function TaskRecoveryPanel({
  tasks,
  action,
  onRetry,
}: {
  tasks: TaskSummary[]
  action?: string
  onRetry: (taskId: string, acknowledgeIndeterminate?: boolean) => Promise<unknown>
}) {
  const actionableTasks = useMemo(() => tasks.filter((task) => task.retryable), [tasks])
  const [confirmingTask, setConfirmingTask] = useState<TaskSummary>()
  const [acknowledged, setAcknowledged] = useState(false)

  useEffect(() => {
    if (!confirmingTask || tasks.some((task) => task.id === confirmingTask.id && task.retryable)) return
    setConfirmingTask(undefined)
  }, [confirmingTask, tasks])

  const closeConfirmation = () => {
    setConfirmingTask(undefined)
    setAcknowledged(false)
  }

  const retry = async (task: TaskSummary) => {
    if (task.requiresAcknowledgement) {
      setAcknowledged(false)
      setConfirmingTask(task)
      return
    }
    await onRetry(task.id, false)
  }

  const confirmRetry = async () => {
    if (!confirmingTask || !acknowledged) return
    try {
      await onRetry(confirmingTask.id, true)
      closeConfirmation()
    } catch {
      // The shared notice keeps the server's actionable error visible.
    }
  }

  if (actionableTasks.length === 0) return null

  return (
    <section aria-labelledby="task-recovery-title" className="task-recovery">
      <div className="task-recovery__heading">
        <Icon name="warning" size={18} />
        <div>
          <strong id="task-recovery-title">有 {actionableTasks.length} 个任务需要处理</strong>
          <span>任务不会被静默重放；请核对失败原因后决定是否重试。</span>
        </div>
      </div>
      <ul>
        {actionableTasks.map((task) => (
          <li key={task.id}>
            <div>
              <strong>{taskLabel(task)}</strong>
              <span>
                {task.requiresAcknowledgement ? '调用结果不确定' : '任务失败'}
                {' · '}尝试 {task.attemptCount} / {task.maxAttempts}
                {task.provider ? ` · ${task.provider}${task.model ? ` / ${task.model}` : ''}` : ''}
              </span>
              {task.errorMessage ? <small>{task.errorMessage}</small> : null}
            </div>
            <Button
              busy={action === `retry:${task.id}`}
              icon="refresh"
              onClick={() => void retry(task)}
              size="sm"
              variant={task.requiresAcknowledgement ? 'danger' : 'secondary'}
            >{task.requiresAcknowledgement ? '确认风险后重试' : '重试任务'}</Button>
          </li>
        ))}
      </ul>

      <Dialog
        description="上一次模型请求可能已经被服务商执行，但响应未能可靠落盘。"
        onClose={closeConfirmation}
        open={confirmingTask !== undefined}
        title="确认可能重复调用与计费"
      >
        <div className="indeterminate-confirmation">
          <div className="indeterminate-confirmation__warning" role="alert">
            <Icon name="warning" size={19} />
            <p><strong>再次调用可能产生第二次费用。</strong>两次请求也可能返回不同正文；系统不会用未落盘的结果覆盖本次重试。</p>
          </div>
          {confirmingTask ? (
            <dl>
              <div><dt>任务</dt><dd>{taskLabel(confirmingTask)}</dd></div>
              <div><dt>模型</dt><dd>{confirmingTask.provider ?? '未记录'}{confirmingTask.model ? ` / ${confirmingTask.model}` : ''}</dd></div>
              <div><dt>错误</dt><dd>{confirmingTask.errorMessage ?? confirmingTask.errorCode ?? '响应状态不确定'}</dd></div>
            </dl>
          ) : null}
          <label className="risk-acknowledgement">
            <input
              checked={acknowledged}
              onChange={(event) => setAcknowledged(event.target.checked)}
              type="checkbox"
            />
            <span className="risk-acknowledgement__mark"><Icon name={acknowledged ? 'check' : 'warning'} size={15} /></span>
            <span><strong>我确认此次重试可能造成重复 API 调用与重复计费</strong><small>勾选后，前端会发送显式确认字段并重新排队该任务。</small></span>
          </label>
          <div className="dialog__actions">
            <Button onClick={closeConfirmation} variant="ghost">取消</Button>
            <Button
              busy={Boolean(confirmingTask && action === `retry:${confirmingTask.id}`)}
              disabled={!acknowledged}
              icon="refresh"
              onClick={() => void confirmRetry()}
              variant="danger"
            >确认风险并重试</Button>
          </div>
        </div>
      </Dialog>
    </section>
  )
}
