import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { TaskSummary } from '../../types'
import { TaskRecoveryPanel } from './TaskRecoveryPanel'

const indeterminateTask: TaskSummary = {
  id: 'task-1',
  operation: 'write_chapter',
  status: 'indeterminate',
  attemptCount: 1,
  maxAttempts: 3,
  retryable: true,
  requiresAcknowledgement: true,
  provider: 'deepseek',
  model: 'deepseek-v4-pro',
  errorMessage: '响应落盘前连接中断',
}

describe('TaskRecoveryPanel', () => {
  it('requires an explicit duplicate-charge acknowledgement before retrying', async () => {
    const user = userEvent.setup()
    const onRetry = vi.fn().mockResolvedValue(undefined)
    render(<TaskRecoveryPanel onRetry={onRetry} tasks={[indeterminateTask]} />)

    await user.click(screen.getByRole('button', { name: '确认风险后重试' }))
    const submit = screen.getByRole('button', { name: '确认风险并重试' }) as HTMLButtonElement
    expect(submit.disabled).toBe(true)
    expect(screen.getByText('再次调用可能产生第二次费用。')).toBeTruthy()

    await user.click(screen.getByRole('checkbox', { name: /我确认此次重试可能造成重复 API 调用与重复计费/ }))
    expect(submit.disabled).toBe(false)
    await user.click(submit)

    expect(onRetry).toHaveBeenCalledWith('task-1', true)
  })

  it('retries an ordinary failed task without the billing-risk dialog', async () => {
    const user = userEvent.setup()
    const onRetry = vi.fn().mockResolvedValue(undefined)
    render(<TaskRecoveryPanel onRetry={onRetry} tasks={[{
      ...indeterminateTask,
      id: 'task-2',
      status: 'failed',
      requiresAcknowledgement: false,
    }]} />)

    await user.click(screen.getByRole('button', { name: '重试任务' }))

    expect(onRetry).toHaveBeenCalledWith('task-2', false)
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})
