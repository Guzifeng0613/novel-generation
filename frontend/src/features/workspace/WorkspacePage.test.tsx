import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { ProjectDetail, ProviderStatus, RunDetail } from '../../types'
import { WorkspacePage } from './WorkspacePage'

const providers: ProviderStatus = { providers: [] }

function project(): ProjectDetail {
  return {
    id: 'project-1',
    name: '雨站',
    sources: [],
    artifacts: [],
    runs: [],
    storyState: {
      canon: [{ id: 'canon-injury', title: '林岚左臂骨折' }],
      suspense: [{ id: 'mystery-watch', title: '坏掉的怀表' }],
      entities: [],
      conflicts: [],
      worldRules: [],
    },
  }
}

function run(): RunDetail {
  return {
    id: 'run-1',
    kind: 'continuation',
    status: 'completed',
    chapters: [
      { id: 'chapter-1', index: 1, title: '空站', content: '空站无事。' },
      { id: 'chapter-2', index: 2, title: '雨停', content: '雨停了。\n\n林岚抬起完好的左臂。' },
    ],
    reviewIssues: [{
      id: 'ISS-1',
      severity: 'major',
      dimension: 'canon_world_rules',
      message: '伤势无治疗恢复',
      title: '伤势消失',
      startOffset: 10,
      endOffset: 15,
      chapterId: 'chapter-2',
      references: [{ id: 'canon-injury', kind: 'canon', title: '林岚左臂骨折' }],
    }],
    tasks: [],
    artifacts: [],
    metrics: {},
  }
}

describe('WorkspacePage review issue location', () => {
  it('switches to the issue chapter and highlights the referenced manuscript range', async () => {
    const user = userEvent.setup()
    render(
      <WorkspacePage
        onApproveOutline={vi.fn().mockResolvedValue(undefined)}
        onBack={vi.fn()}
        onControlRun={vi.fn().mockResolvedValue(undefined)}
        onRetryTask={vi.fn().mockResolvedValue(undefined)}
        onReviseOutline={vi.fn().mockResolvedValue(undefined)}
        onSelectRun={vi.fn()}
        onStartAnalysis={vi.fn().mockResolvedValue(undefined)}
        onStartContinuation={vi.fn().mockResolvedValue(undefined)}
        project={project()}
        providers={providers}
        run={run()}
      />,
    )

    await user.click(screen.getByRole('button', { name: /定位正文/ }))

    expect(screen.getByRole('tab', { name: '章节正文' }).getAttribute('aria-selected')).toBe('true')
    expect(screen.getByRole('heading', { name: '雨停' })).toBeTruthy()

    const highlight = screen.getByText('完好的左臂')
    expect(highlight.tagName.toLowerCase()).toBe('mark')
    expect(within(screen.getByLabelText('审稿依据')).getByText(/Canon · 林岚左臂骨折/)).toBeTruthy()
  })
})
