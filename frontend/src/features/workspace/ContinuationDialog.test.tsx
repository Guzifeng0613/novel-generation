import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { ContinuationDialog } from './ContinuationDialog'

const providers = {
  providers: [
    { id: 'openai', name: 'OpenAI', configured: true },
    { id: 'deepseek', name: 'DeepSeek', configured: true },
  ],
}

describe('ContinuationDialog', () => {
  it('submits the approved defaults without exposing API keys', async () => {
    const user = userEvent.setup()
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    const onClose = vi.fn()
    render(
      <ContinuationDialog
        busy={false}
        onClose={onClose}
        onSubmit={onSubmit}
        open
        providers={providers}
      />,
    )

    expect((screen.getByRole('spinbutton', { name: /目标字数/ }) as HTMLInputElement).value).toBe('40000')
    expect((screen.getByRole('radio', { name: /混合 · 质量优先/ }) as HTMLInputElement).checked).toBe(true)

    await user.click(screen.getByRole('button', { name: /生成待审批章纲/ }))

    expect(onSubmit).toHaveBeenCalledWith({
      targetChars: 40_000,
      userRequirements: '',
      routingPreset: 'hybrid-quality',
      maxCost: undefined,
      branchName: 'main',
    })
    expect(onClose).toHaveBeenCalledOnce()
    expect(document.body.textContent).not.toContain('API_KEY')
  })

  it('blocks target lengths outside the allowed range', async () => {
    const user = userEvent.setup()
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    render(
      <ContinuationDialog
        busy={false}
        onClose={() => undefined}
        onSubmit={onSubmit}
        open
        providers={providers}
      />,
    )

    const target = screen.getByRole('spinbutton', { name: /目标字数/ })
    await user.clear(target)
    await user.type(target, '3999')
    await user.click(screen.getByRole('button', { name: /生成待审批章纲/ }))

    expect(screen.getByRole('alert').textContent).toContain('4,000–150,000')
    expect(onSubmit).not.toHaveBeenCalled()
  })
})
