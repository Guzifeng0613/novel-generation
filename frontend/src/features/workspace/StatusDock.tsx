import type { Artifact, RunDetail } from '../../types'
import { formatCost, formatDuration, formatNumber } from '../../utils'
import { api } from '../../api'
import { Button } from '../../components/Button'
import { Icon } from '../../components/Icon'
import { StatusMark } from '../../components/StatusMark'

function DownloadMenu({ artifacts }: { artifacts: Artifact[] }) {
  const deliveryArtifacts = artifacts.filter((artifact) => artifact.kind.startsWith('delivery_'))
  if (deliveryArtifacts.length === 0) return <Button disabled icon="download" size="sm">暂无交付物</Button>
  return (
    <details className="download-menu">
      <summary className="button button--secondary button--sm"><Icon name="download" size={15} /><span>下载交付物</span></summary>
      <div>
        {deliveryArtifacts.map((artifact) => (
          <a download href={api.artifactDownloadUrl(artifact.id)} key={artifact.id}>{artifact.title}<small>{artifact.kind}</small></a>
        ))}
      </div>
    </details>
  )
}

export function StatusDock({
  run,
  action,
  onControl,
}: {
  run?: RunDetail
  action?: string
  onControl: (action: 'pause' | 'resume' | 'cancel') => Promise<unknown>
}) {
  if (!run) {
    return <footer className="status-dock status-dock--empty"><span>尚无运行</span><p>启动拆分总结或续写后，这里将显示真实队列、token、费用与耗时。</p></footer>
  }
  const canPause = run.status === 'running' || run.status === 'queued'
  const canResume = run.status === 'paused'
  const canCancel = !['completed', 'failed', 'cancelled', 'cancelling'].includes(run.status)
  const deliveryInvalid = run.deliveryValid === false
  return (
    <footer className="status-dock">
      <div className="status-dock__run">
        <StatusMark status={run.status} />
        <span><strong>{run.kind === 'analysis' ? '拆分总结运行' : '续写生产运行'}</strong><small>{run.stage ? `阶段 · ${run.stage}` : '阶段尚未上报'}</small></span>
      </div>
      <dl className="status-metrics">
        <div><dt>队列</dt><dd>{formatNumber(run.metrics.queued)}</dd></div>
        <div><dt>API 调用</dt><dd>{formatNumber(run.metrics.calls)}</dd></div>
        <div><dt>Token</dt><dd>{formatNumber(run.metrics.tokens)}</dd></div>
        <div><dt>费用</dt><dd>{formatCost(run.metrics.cost)}{run.metrics.maxCost !== undefined ? <small> / {formatCost(run.metrics.maxCost)}</small> : null}</dd></div>
        <div><dt>耗时</dt><dd>{formatDuration(run.metrics.elapsedSeconds)}</dd></div>
        <div><dt>正文</dt><dd>{Math.max(0, ...run.chapters.map((chapter) => chapter.revision ?? 0))} 版</dd></div>
        <div><dt>审校</dt><dd>{run.contentRevisionInvalidated ? '已过期' : '有效'}</dd></div>
      </dl>
      <div className="status-actions">
        {canPause ? <Button busy={action === 'pause'} icon="pause" onClick={() => void onControl('pause')} size="sm">暂停</Button> : null}
        {canResume ? <Button busy={action === 'resume'} icon="play" onClick={() => void onControl('resume')} size="sm" variant="primary">恢复</Button> : null}
        {canCancel ? <Button busy={action === 'cancel'} icon="stop" onClick={() => void onControl('cancel')} size="sm" variant="danger">取消</Button> : null}
        {deliveryInvalid ? <Button disabled icon="warning" size="sm">交付已失效</Button> : <DownloadMenu artifacts={run.artifacts} />}
      </div>
    </footer>
  )
}
