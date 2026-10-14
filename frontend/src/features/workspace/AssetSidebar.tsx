import { useEffect, useMemo, useState } from 'react'
import { api } from '../../api'
import type { Artifact, Chapter, ProjectDetail, RunDetail } from '../../types'
import { formatDate } from '../../utils'
import { Button } from '../../components/Button'
import { Dialog } from '../../components/Dialog'
import { EmptyState } from '../../components/EmptyState'
import { Icon } from '../../components/Icon'
import { StatusMark } from '../../components/StatusMark'

const TEXT_PREVIEW_LIMIT = 140_000

function formatBytes(value?: number): string {
  if (!value) return ''
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

function isTextPreviewable(artifact: Artifact): boolean {
  const mediaType = artifact.mediaType?.toLowerCase() ?? ''
  const title = artifact.title.toLowerCase()
  return (
    mediaType.startsWith('text/')
    || mediaType.includes('json')
    || mediaType.includes('xml')
    || title.endsWith('.md')
    || title.endsWith('.txt')
    || title.endsWith('.json')
  )
}

export function AssetSidebar({
  project,
  run,
  selectedChapterId,
  onSelectChapter,
  onSelectRun,
}: {
  project: ProjectDetail
  run?: RunDetail
  selectedChapterId?: string
  onSelectChapter: (chapter: Chapter) => void
  onSelectRun: (id: string) => void
}) {
  const [previewArtifact, setPreviewArtifact] = useState<Artifact>()
  const projectArtifacts = useMemo(
    () => [...new Map([...project.sources, ...project.artifacts].map((item) => [item.id, item])).values()],
    [project.artifacts, project.sources],
  )
  return (
    <aside className="asset-sidebar" aria-label="项目资料">
      <section className="sidebar-section">
        <h2><Icon name="folder" size={15} /> 项目资产 <span>{projectArtifacts.length}</span></h2>
        {projectArtifacts.length === 0 ? (
          <EmptyState compact detail="导入完成后，原稿与九维档案会出现在这里。" title="暂无资产索引" />
        ) : (
          <ul className="asset-list">
            {projectArtifacts.map((artifact) => (
              <li key={artifact.id}>
                <button onClick={() => setPreviewArtifact(artifact)} type="button">
                  <span className="asset-list__icon"><Icon name={artifact.kind.includes('source') ? 'book' : 'file'} size={15} /></span>
                  <span>
                    <strong>{artifact.title}</strong>
                    <small>
                      {artifact.kind}
                      {artifact.version ? ` · v${artifact.version}` : ''}
                      {artifact.size ? ` · ${formatBytes(artifact.size)}` : ''}
                    </small>
                  </span>
                  <Icon name="search" size={14} />
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="sidebar-section sidebar-section--chapters">
        <h2><Icon name="layers" size={15} /> 章节树 <span>{run?.chapters.length ?? 0}</span></h2>
        {run?.chapters.length ? (
          <ol className="chapter-tree">
            {run.chapters.map((chapter) => (
              <li key={chapter.id}>
                <button
                  aria-current={selectedChapterId === chapter.id ? 'page' : undefined}
                  className={selectedChapterId === chapter.id ? 'is-active' : ''}
                  onClick={() => onSelectChapter(chapter)}
                  type="button"
                >
                  <span className="chapter-tree__index">{String(chapter.index).padStart(2, '0')}</span>
                  <span><strong>{chapter.title}</strong><small>{chapter.status ?? '等待生产'}{chapter.revision ? ` · 修订 ${chapter.revision}` : ''}</small></span>
                </button>
              </li>
            ))}
          </ol>
        ) : (
          <p className="sidebar-hint">章纲获批且正文开始生成后显示。</p>
        )}
      </section>

      <section className="sidebar-section sidebar-section--runs">
        <h2><Icon name="clock" size={15} /> 运行历史 <span>{project.runs.length}</span></h2>
        {project.runs.length ? (
          <ul className="run-list">
            {project.runs.map((item) => (
              <li key={item.id}>
                <button aria-current={item.id === run?.id ? 'page' : undefined} onClick={() => onSelectRun(item.id)} type="button">
                  <StatusMark compact status={item.status} />
                  <span><strong>{item.kind === 'analysis' ? '拆分总结' : '小说续写'}</strong><small>{formatDate(item.updatedAt ?? item.createdAt)}</small></span>
                </button>
              </li>
            ))}
          </ul>
        ) : <p className="sidebar-hint">尚未启动任何运行。</p>}
      </section>

      <AssetPreviewDialog
        artifact={previewArtifact}
        onClose={() => setPreviewArtifact(undefined)}
      />
    </aside>
  )
}

function AssetPreviewDialog({
  artifact,
  onClose,
}: {
  artifact?: Artifact
  onClose: () => void
}) {
  const [text, setText] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const previewable = artifact ? isTextPreviewable(artifact) : false

  useEffect(() => {
    if (!artifact || !previewable) {
      setText('')
      setError('')
      setLoading(false)
      return
    }
    const controller = new AbortController()
    setLoading(true)
    setError('')
    fetch(api.artifactPreviewUrl(artifact.id), { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(`预览读取失败（${response.status}）`)
        return response.text()
      })
      .then((value) => setText(value.length > TEXT_PREVIEW_LIMIT ? `${value.slice(0, TEXT_PREVIEW_LIMIT)}\n\n...内容过长，已截断预览。` : value))
      .catch((requestError: unknown) => {
        if (!controller.signal.aborted) setError(requestError instanceof Error ? requestError.message : '预览读取失败')
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [artifact, previewable])

  if (!artifact) return null

  const previewUrl = api.artifactPreviewUrl(artifact.id)
  const downloadUrl = api.artifactDownloadUrl(artifact.id)
  const description = [
    artifact.kind,
    artifact.version ? `v${artifact.version}` : '',
    formatBytes(artifact.size),
    artifact.mediaType,
  ].filter(Boolean).join(' · ')

  return (
    <Dialog description={description} onClose={onClose} open title={artifact.title} wide>
      <div className="asset-preview">
        {previewable ? (
          <div className="asset-preview__text" aria-busy={loading}>
            {loading ? <p>正在读取预览...</p> : error ? <p>{error}</p> : <pre>{text || '暂无可预览内容。'}</pre>}
          </div>
        ) : (
          <div className="asset-preview__fallback">
            <Icon name={artifact.kind.includes('archive') || artifact.kind.includes('zip') ? 'archive' : 'file'} size={32} />
            <strong>此素材将使用浏览器原生预览</strong>
            <p>{artifact.mediaType ?? '未知文件类型'}</p>
          </div>
        )}
        <div className="dialog__actions">
          <Button icon="search" onClick={() => window.open(previewUrl, '_blank', 'noopener,noreferrer')}>打开预览</Button>
          <Button icon="download" onClick={() => window.open(downloadUrl, '_blank', 'noopener,noreferrer')} variant="primary">下载</Button>
        </div>
      </div>
    </Dialog>
  )
}
