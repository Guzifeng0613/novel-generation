import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { RunDetail } from '../../types'
import { OutlineView } from './OutlineView'

function conflictingRun(branchName: string): RunDetail {
  return {
    id: 'run-1',
    kind: 'continuation',
    status: 'awaiting_outline_approval',
    branchName,
    chapters: [],
    reviewIssues: [],
    tasks: [],
    artifacts: [],
    metrics: {},
    plan: {
      status: 'awaiting_outline_approval',
      planHash: 'a'.repeat(64),
      canonConflicts: ['既有角色已死亡，不能直接复活'],
      chapters: [],
    },
  }
}

describe('OutlineView canon override gate', () => {
  it('requires an explicit confirmation before approving on an independent branch', async () => {
    const user = userEvent.setup()
    const onApprove = vi.fn().mockResolvedValue(undefined)
    render(
      <OutlineView
        activeBranch="main"
        onApprove={onApprove}
        onCreateContinuation={() => undefined}
        onRevise={vi.fn().mockResolvedValue(undefined)}
        run={conflictingRun('alternate-ending')}
      />,
    )

    const approve = screen.getByRole('button', { name: /批准并开始写作/ }) as HTMLButtonElement
    expect(approve.disabled).toBe(true)

    await user.click(screen.getByRole('checkbox', { name: /确认在独立分支中创建 Canon 覆盖/ }))
    expect(approve.disabled).toBe(false)
    await user.click(approve)

    expect(onApprove).toHaveBeenCalledWith(true)
  })

  it('does not allow a conflict override on the formal branch', () => {
    render(
      <OutlineView
        activeBranch="main"
        onApprove={vi.fn().mockResolvedValue(undefined)}
        onCreateContinuation={() => undefined}
        onRevise={vi.fn().mockResolvedValue(undefined)}
        run={conflictingRun('main')}
      />,
    )

    expect((screen.getByRole('checkbox') as HTMLInputElement).disabled).toBe(true)
    expect(screen.getByText(/当前运行不在独立分支/)).toBeTruthy()
    expect((screen.getByRole('button', { name: /批准并开始写作/ }) as HTMLButtonElement).disabled).toBe(true)
  })
})
