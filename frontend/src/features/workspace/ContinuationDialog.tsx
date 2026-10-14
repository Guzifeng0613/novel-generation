import { useState, type FormEvent } from 'react'
import type { ContinuationInput, ProviderStatus, RoutingPreset } from '../../types'
import { Button } from '../../components/Button'
import { Dialog } from '../../components/Dialog'
import { Icon } from '../../components/Icon'

const ROUTES: { id: RoutingPreset; title: string; detail: string }[] = [
  { id: 'hybrid-quality', title: '混合 · 质量优先', detail: '强模型负责规划与终审，DeepSeek 承担分析、写作与维审。' },
  { id: 'openai-only', title: '仅 OpenAI', detail: '所有角色使用 OpenAI，结构化输出与终审保持同一模型族。' },
  { id: 'deepseek-only', title: '仅 DeepSeek', detail: '所有角色使用 DeepSeek，适合控制成本的完整运行。' },
]

export function ContinuationDialog({
  open,
  providers,
  activeBranch = 'main',
  busy,
  onClose,
  onSubmit,
}: {
  open: boolean
  providers: ProviderStatus
  activeBranch?: string
  busy: boolean
  onClose: () => void
  onSubmit: (input: ContinuationInput) => Promise<unknown>
}) {
  const [targetChars, setTargetChars] = useState(40_000)
  const [requirements, setRequirements] = useState('')
  const [preset, setPreset] = useState<RoutingPreset>('hybrid-quality')
  const [maxCost, setMaxCost] = useState('')
  const [branchName, setBranchName] = useState(activeBranch)
  const [error, setError] = useState<string>()
  const configuredIds = new Set(providers.providers.filter((item) => item.configured).map((item) => item.id))

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!Number.isInteger(targetChars) || targetChars < 4_000 || targetChars > 150_000) {
      setError('目标字数必须是 4,000–150,000 之间的整数。')
      return
    }
    const parsedCost = maxCost.trim() ? Number(maxCost) : undefined
    if (parsedCost !== undefined && (!Number.isFinite(parsedCost) || parsedCost <= 0)) {
      setError('费用上限必须是大于 0 的数字。')
      return
    }
    setError(undefined)
    try {
      const normalizedBranch = branchName.trim()
      if (!normalizedBranch) {
        setError('分支名不能为空。')
        return
      }
      await onSubmit({
        targetChars,
        userRequirements: requirements.trim(),
        routingPreset: preset,
        maxCost: parsedCost,
        branchName: normalizedBranch,
      })
      onClose()
    } catch {
      // The shared notice displays the server response.
    }
  }

  return (
    <Dialog description="主 A 将根据目标字数、续写要求与当前 Canon 生成可审批章纲。" onClose={onClose} open={open} title="新建续写运行" wide>
      <form className="continuation-form" noValidate onSubmit={submit}>
        <div className="form-grid">
          <label className="field">
            <span>目标字数 <small>4,000–150,000</small></span>
            <input
              inputMode="numeric"
              max={150000}
              min={4000}
              onChange={(event) => setTargetChars(Number(event.target.value))}
              required
              step={1}
              type="number"
              value={targetChars}
            />
          </label>
          <label className="field">
            <span>费用上限 <small>美元，可选</small></span>
            <input inputMode="decimal" min="0.01" onChange={(event) => setMaxCost(event.target.value)} placeholder="由系统按预测上界设置" step="0.01" type="number" value={maxCost} />
          </label>
        </div>
        <label className="field">
          <span>写入分支 <small>正式分支：{activeBranch}</small></span>
          <input
            maxLength={120}
            onChange={(event) => setBranchName(event.target.value)}
            placeholder={activeBranch}
            required
            type="text"
            value={branchName}
          />
          <small className="branch-field-note">预计会改写既有 Canon 时，请在运行前填写新的独立分支名。</small>
        </label>
        <label className="field">
          <span>续写要求 <small>可选</small></span>
          <textarea maxLength={6000} onChange={(event) => setRequirements(event.target.value)} placeholder="例如：从上一章雨夜离开车站后继续；维持第三人称限知；本轮不要揭开信封的真实来源……" rows={5} value={requirements} />
          <small className="field-counter">{requirements.length} / 6,000</small>
        </label>
        <fieldset className="route-picker">
          <legend>模型路由</legend>
          {ROUTES.map((route) => {
            const missing = route.id === 'openai-only' ? !configuredIds.has('openai')
              : route.id === 'deepseek-only' ? !configuredIds.has('deepseek')
                : !configuredIds.has('openai') || !configuredIds.has('deepseek')
            return (
              <label className={preset === route.id ? 'is-selected' : ''} key={route.id}>
                <input checked={preset === route.id} name="routing-preset" onChange={() => setPreset(route.id)} type="radio" value={route.id} />
                <span className="route-picker__check"><Icon name={preset === route.id ? 'check' : 'route'} size={16} /></span>
                <span><strong>{route.title}</strong><small>{route.detail}</small></span>
                {missing ? <em><Icon name="warning" size={13} /> 密钥未确认</em> : <em className="route-ready"><Icon name="check" size={13} /> 可用</em>}
              </label>
            )
          })}
        </fieldset>
        <p className="form-note"><Icon name="clock" size={15} /> 章纲生成后会暂停等待批准；在批准前不会调用章节写作模型。</p>
        {error ? <p className="field-error" role="alert">{error}</p> : null}
        <div className="dialog__actions">
          <Button onClick={onClose} variant="ghost">取消</Button>
          <Button busy={busy} icon="spark" type="submit" variant="primary">生成待审批章纲</Button>
        </div>
      </form>
    </Dialog>
  )
}
