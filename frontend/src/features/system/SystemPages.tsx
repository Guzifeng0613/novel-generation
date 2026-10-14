import { useState, type FormEvent } from 'react'
import type { ProjectSummary, PromptVersionStatus, ProviderStatus, RunDetail, RunSummary } from '../../types'
import { formatDate } from '../../utils'
import { Button } from '../../components/Button'
import { Dialog } from '../../components/Dialog'
import { EmptyState } from '../../components/EmptyState'
import { Icon } from '../../components/Icon'
import { StatusMark } from '../../components/StatusMark'

function PageHeader({ title, detail }: { title: string; detail: string }) {
  return <header className="system-page__header"><h1>{title}</h1><p>{detail}</p></header>
}

export function RunCenter({
  projects,
  currentRun,
  onOpenProject,
  onOpenRun,
}: {
  projects: ProjectSummary[]
  currentRun?: RunDetail
  onOpenProject: (id: string) => void
  onOpenRun: (runId: string, projectId?: string) => void
}) {
  const projectRuns = currentRun ? [currentRun] : []
  return (
    <main className="system-page">
      <PageHeader detail="查看本机已同步的运行。没有后端数据时不展示模拟队列。" title="运行中心" />
      {projectRuns.length === 0 ? (
        <EmptyState action={projects[0] ? <Button onClick={() => onOpenProject(projects[0].id)}>打开最近项目</Button> : undefined} detail="打开项目或启动一次拆分 / 续写运行后，状态会在这里同步。" icon="clock" title="没有可显示的运行" />
      ) : (
        <div className="system-table" role="table" aria-label="运行列表">
          <div className="system-table__head" role="row"><span>运行</span><span>类型</span><span>阶段</span><span>更新时间</span><span /></div>
          {projectRuns.map((run: RunSummary) => (
            <div className="system-table__row" role="row" key={run.id}>
              <span><StatusMark status={run.status} /></span>
              <strong>{run.kind === 'analysis' ? '拆分总结' : '小说续写'}</strong>
              <span>{run.stage ?? '未上报'}</span>
              <span>{formatDate(run.updatedAt ?? run.createdAt)}</span>
              <Button onClick={() => onOpenRun(run.id, run.projectId)} size="sm" variant="ghost">打开</Button>
            </div>
          ))}
        </div>
      )}
    </main>
  )
}

export function ProviderPage({
  status,
  error,
  busy,
  onSaveKeys,
}: {
  status: ProviderStatus
  error?: string
  busy: boolean
  onSaveKeys: (keys: { openaiApiKey?: string; deepseekApiKey?: string }) => Promise<unknown>
}) {
  const [dialogOpen, setDialogOpen] = useState(false)
  const [openaiApiKey, setOpenaiApiKey] = useState('')
  const [deepseekApiKey, setDeepseekApiKey] = useState('')
  const [formError, setFormError] = useState<string>()

  const submitKeys = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!openaiApiKey.trim() && !deepseekApiKey.trim()) {
      setFormError('请至少填写一个 API 密钥。')
      return
    }
    setFormError(undefined)
    try {
      await onSaveKeys({ openaiApiKey, deepseekApiKey })
      setOpenaiApiKey('')
      setDeepseekApiKey('')
      setDialogOpen(false)
    } catch (saveError) {
      setFormError(saveError instanceof Error ? saveError.message : 'API 密钥保存失败。')
    }
  }

  return (
    <main className="system-page">
      <header className="system-page__header">
        <div><h1>模型路由</h1><p>密钥保存在本机服务配置中，不会显示或回传到页面。</p></div>
        <Button icon="plus" onClick={() => setDialogOpen(true)} variant="primary">添加 API 密钥</Button>
      </header>
      {error ? <EmptyState detail={error} icon="warning" title="模型状态暂不可用" /> : status.providers.length === 0 ? (
        <EmptyState detail="服务端尚未返回 Provider 状态。配置环境变量后刷新页面。" icon="route" title="没有 Provider 数据" />
      ) : (
        <div className="provider-grid">
          {status.providers.map((provider) => (
            <article className="provider-card" key={provider.id}>
              <header><span className="provider-card__mark">{provider.name.slice(0, 1)}</span><div><h2>{provider.name}</h2><p>{provider.id}</p></div></header>
              <dl>
                <div><dt>密钥</dt><dd className={provider.configured ? 'positive' : 'negative'}>{provider.configured ? '✓ 已在服务端配置' : '× 未配置'}</dd></div>
                <div><dt>连通性</dt><dd>{provider.available === undefined ? '尚未探测' : provider.available ? '✓ 可连接' : '× 不可连接'}</dd></div>
                <div><dt>默认模型</dt><dd>{provider.model ?? '后端未返回'}</dd></div>
              </dl>
              {provider.message ? <p className="provider-card__message">{provider.message}</p> : null}
            </article>
          ))}
        </div>
      )}
      <section className="routing-notes">
        <Icon name="route" size={20} />
        <div><h2>路由预设</h2><p>OpenAI-only、DeepSeek-only 与 Hybrid-quality 由每次续写运行独立选择并在章纲批准时冻结。模型名称和密钥由服务端控制。</p></div>
      </section>
      <Dialog description="保存后立即更新本机运行时的 Provider 配置。已保存的密钥不会再次显示。" onClose={() => setDialogOpen(false)} open={dialogOpen} title="添加 API 密钥">
        <form className="provider-key-form" noValidate onSubmit={submitKeys}>
          <label className="field">
            <span>OpenAI API Key <small>可选</small></span>
            <input autoComplete="off" disabled={busy} onChange={(event) => setOpenaiApiKey(event.target.value)} placeholder="sk-..." spellCheck={false} type="password" value={openaiApiKey} />
          </label>
          <label className="field">
            <span>DeepSeek API Key <small>可选</small></span>
            <input autoComplete="off" disabled={busy} onChange={(event) => setDeepseekApiKey(event.target.value)} placeholder="sk-..." spellCheck={false} type="password" value={deepseekApiKey} />
          </label>
          {formError ? <p className="field-error" role="alert">{formError}</p> : null}
          <div className="dialog__actions">
            <Button disabled={busy} onClick={() => setDialogOpen(false)} variant="ghost">取消</Button>
            <Button busy={busy} icon="check" type="submit" variant="primary">保存密钥</Button>
          </div>
        </form>
      </Dialog>
    </main>
  )
}

export function PromptPage({ status, error }: { status: PromptVersionStatus; error?: string }) {
  return (
    <main className="system-page">
      <PageHeader detail="运行提示词的可追溯版本将由服务端索引。" title="提示词版本" />
      {error ? (
        <EmptyState detail={error} icon="warning" title="提示词版本暂不可用" />
      ) : status.prompts.length === 0 ? (
        <EmptyState detail="服务端没有返回运行提示词清单。" icon="file" title="没有提示词版本记录" />
      ) : (
        <section className="prompt-version-list" aria-label="运行时提示词版本">
          <header><span>提示词</span><span>文件</span><span>版本</span><span>内容哈希</span></header>
          {status.prompts.map((prompt, index) => (
            <article key={prompt.file}>
              <span>{String(index + 1).padStart(2, '0')}</span>
              <strong>{prompt.name}</strong>
              <code>{prompt.file}</code>
              <em>v{prompt.version}</em>
              <code title={prompt.sha256}>{prompt.sha256 ? `${prompt.sha256.slice(0, 12)}…` : '未上报'}</code>
            </article>
          ))}
        </section>
      )}
    </main>
  )
}
