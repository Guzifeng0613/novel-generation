import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { DiffView } from './DiffView'

describe('DiffView', () => {
  it('marks paragraph rows and identifies a human-authored version', () => {
    render(<DiffView chapter={{
      id: 'chapter-1', index: 1, title: '余波', revision: 2, humanEdited: true,
      previousContent: '相同段。\n\n旧段。', content: '相同段。\n\n新段。',
    }} />)
    expect(screen.getByText(/当前 · v2 · 人工版本/).textContent).toContain('人工版本')
    expect(screen.getByText('旧段。').closest('section')?.classList.contains('is-changed')).toBe(true)
    expect(screen.getAllByText('相同段。')[0].closest('section')?.classList.contains('is-changed')).toBe(false)
  })
})
