import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { Chapter } from '../../types'
import { ChapterView } from './ChapterView'

const chapter: Chapter = {
  id: 'chapter-1',
  index: 1,
  title: '余波',
  content: '第一段。\n\n第二段。',
  revision: 2,
  contentSha256: 'a'.repeat(64),
  editable: true,
  humanEdited: false,
}

describe('ChapterView human editing', () => {
  it('saves an edited chapter using optimistic-lock metadata', async () => {
    const user = userEvent.setup()
    const onSave = vi.fn().mockResolvedValue(undefined)
    render(<ChapterView chapter={chapter} onSave={onSave} />)

    await user.click(screen.getByRole('button', { name: '编辑正文' }))
    await user.clear(screen.getByLabelText('章节正文'))
    await user.type(screen.getByLabelText('章节正文'), '人工修订后的正文。')
    await user.type(screen.getByLabelText('编辑说明（可选）'), '修正动机')
    await user.click(screen.getByRole('button', { name: '保存新版本' }))

    expect(onSave).toHaveBeenCalledWith({
      content: '人工修订后的正文。',
      baseRevision: 2,
      baseSha256: 'a'.repeat(64),
      reason: '修正动机',
    })
  })

  it('disables editing while the run is active', () => {
    render(<ChapterView chapter={{ ...chapter, editable: false }} onSave={vi.fn()} />)
    expect((screen.getByRole('button', { name: '编辑正文' }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText('运行中不能编辑，请先暂停').textContent).toBe('运行中不能编辑，请先暂停')
  })
})
