import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { StatusMark } from './StatusMark'

describe('StatusMark', () => {
  it('always exposes a textual status in addition to color', () => {
    render(<StatusMark status="needs_human" />)

    expect(screen.getByText('需人工处理')).toBeTruthy()
    expect(screen.getByTitle('需人工处理').textContent).toContain('!')
  })

  it('keeps compact status text available to assistive technology', () => {
    render(<StatusMark compact status="completed" />)

    expect(screen.getByText('已完成')).toBeTruthy()
  })

  it('distinguishes in-flight controls from their terminal state', () => {
    render(<><StatusMark status="pausing" /><StatusMark status="cancelling" /></>)

    expect(screen.getByText('正在暂停')).toBeTruthy()
    expect(screen.getByText('正在取消')).toBeTruthy()
  })
})
