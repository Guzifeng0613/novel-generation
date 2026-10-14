import { useState, type FormEvent } from 'react'
import type { ProviderStatus, RoutingPreset, SummarizerInput } from '../../types'
import { Button } from '../../components/Button'
import { Dialog } from '../../components/Dialog'
import { Icon } from '../../components/Icon'

const ROUTES: { id: RoutingPreset; title: string; detail: string }[] = [
  { id: 'hybrid-quality', title: '混合 · 质量优先', detail: 'DeepSeek 承担抽取，强模型用于结构化兜底。' },
  { id: 'openai-only', title: '仅 OpenAI', detail: '所有总结器任务使用 OpenAI。' },
  { id: 'deepseek-only', title: '仅 DeepSeek', detail: '所有总结器任务使用 DeepSeek。' },
]

export function SummarizerDialog({
  open,
  providers,
  busy,
  onClose,
  onSubmit,
}: {
  open: boolean
  providers: ProviderStatus
  busy: boolean
  onClose: () => void
  onSubmit: (input: SummarizerInput) => Promise<unknown>
}) {
  const [splitterEnabled, setSplitterEnabled] = useState(true)
  const [preset, setPreset] = useState<RoutingPreset>('hybrid-quality')
  const [maxCost, setMaxCost] = useState('')
  const [maxTokens, setMaxTokens] = useState('')
  const [maxCalls, setMaxCalls] = useState('')
  const [error, setError] = useState<string>()
  const configuredIds = new Set(providers.providers.filter((item) => item.configured).map((item) => item.id))

  const parseOptionalNumber = (value: string, label: string) => {
    if (!value.trim()) return undefined
    const parsed = Number(value)
    if (!Number.isFinite(parsed) || parsed <= 0) {
      throw new Error(`${label}必须是大于 0 的数字。`)
    }
    return parsed
  }

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    try {
      const parsedCost = parseOptionalNumber(maxCost, '费用上限')
      const parsedTokens = parseOptionalNumber(maxTokens, 'Token 上限')
      const parsedCalls = parseOptionalNumber(maxCalls, '调用次数上限')
      setError(undefined)
      await onSubmit({
        splitterEnabled,
        routingPreset: preset,
        maxCost: parsedCost,
        maxTokens: parsedTokens === undefined ? undefined : Math.floor(parsedTokens),
        maxCalls: parsedCalls === undefined ? undefined : Math.floor(parsedCalls),
      })
      onClose()
    } catch (error) {
      if (error instanceof Error) setError(error.message)
    }
  }

  return (
    <Dialog description="总结器只生成九类总结产物；精简版会作为续写器默认输入。" onClose={onClose} open={open} title="启动总结器" wide>
      <form className="continuation-form" noValidate onSubmit={submit}>
        <label className="toggle-field">
          <input checked={splitterEnabled} onChange={(event) => setSplitterEnabled(event.target.checked)} type="checkbox" />
          <span><strong>启用拆分器</strong><small>长原文会生成可追踪 source chunks，再保守合并为九类总结。</small></span>
        </label>
        <div className="form-grid">
          <label className="field">
            <span>费用上限 <small>美元，可选</small></span>
            <input inputMode="decimal" min="0.01" onChange={(event) => setMaxCost(event.target.value)} placeholder="默认 30" step="0.01" type="number" value={maxCost} />
          </label>
          <label className="field">
            <span>Token 上限 <small>可选</small></span>
            <input inputMode="numeric" min="1" onChange={(event) => setMaxTokens(event.target.value)} placeholder="默认 2,000,000" step="1" type="number" value={maxTokens} />
          </label>
        </div>
        <label className="field">
          <span>调用次数上限 <small>可选</small></span>
          <input inputMode="numeric" min="1" onChange={(event) => setMaxCalls(event.target.value)} placeholder="默认 250" step="1" type="number" value={maxCalls} />
        </label>
        <fieldset className="route-picker">
          <legend>模型路由</legend>
          {ROUTES.map((route) => {
            const missing = route.id === 'openai-only' ? !configuredIds.has('openai')
              : route.id === 'deepseek-only' ? !configuredIds.has('deepseek')
                : !configuredIds.has('openai') || !configuredIds.has('deepseek')
            return (
              <label className={preset === route.id ? 'is-selected' : ''} key={route.id}>
                <input checked={preset === route.id} name="summary-routing-preset" onChange={() => setPreset(route.id)} type="radio" value={route.id} />
                <span className="route-picker__check"><Icon name={preset === route.id ? 'check' : 'route'} size={16} /></span>
                <span><strong>{route.title}</strong><small>{route.detail}</small></span>
                {missing ? <em><Icon name="warning" size={13} /> 密钥未确认</em> : <em className="route-ready"><Icon name="check" size={13} /> 可用</em>}
              </label>
            )
          })}
        </fieldset>
        {error ? <p className="field-error" role="alert">{error}</p> : null}
        <div className="dialog__actions">
          <Button onClick={onClose} variant="ghost">取消</Button>
          <Button busy={busy} icon="layers" type="submit" variant="primary">生成九类总结</Button>
        </div>
      </form>
    </Dialog>
  )
}
