import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { RunDetail } from '../../types'
import { StatusDock } from './StatusDock'

const run: RunDetail = {
  id: 'run-1',
  kind: 'continuation',
  status: 'completed',
  chapters: [],
  reviewIssues: [],
  tasks: [],
  metrics: {},
  artifacts: [
    { id: 'draft', title: '内部章节稿', kind: 'chapter_final' },
    { id: 'delivery', title: '阅读包', kind: 'delivery_reading_zip' },
    { id: 'bundle', title: '旧命名项目包', kind: 'project_bundle' },
  ],
}

describe('StatusDock', () => {
  it('only exposes delivery artifacts in the download menu', () => {
    render(<StatusDock onControl={async () => undefined} run={run} />)

    expect(screen.getByText('阅读包')).toBeTruthy()
    expect(screen.queryByText('内部章节稿')).toBeNull()
    expect(screen.queryByText('旧命名项目包')).toBeNull()
  })
})
