import { useId, useMemo, useRef, useState, type ChangeEvent, type DragEvent, type FormEvent } from 'react'
import type { ImportInspection, ProjectSummary } from '../../types'
import { formatDate } from '../../utils'
import { Button } from '../../components/Button'
import { EmptyState } from '../../components/EmptyState'
import { Icon } from '../../components/Icon'

interface ProjectLibraryProps {
  projects: ProjectSummary[]
  state: 'idle' | 'loading' | 'ready' | 'error'
  error?: string
  importing: boolean
  previewing: boolean
  serverOnline?: boolean
  onInspect: (files: File[]) => Promise<ImportInspection>
  onImport: (files: File[], name?: string, order?: string[]) => Promise<unknown>
  onOpen: (id: string) => void
  onRefresh: () => void
}

const ALLOWED_EXTENSIONS = ['.txt', '.md', '.zip']
const IMPORT_KIND_LABELS: Record<string, string> = {
  raw_text: '原始小说稿',
  summary_bundle: '九维总结包',
  project_bundle: '可恢复项目包',
}

const DIMENSION_LABELS: Record<string, string> = {
  core_summary: '核心摘要',
  story_outline: '故事大纲',
  characters: '角色',
  conflicts: '冲突',
  core_mysteries: '核心悬念',
  worldbuilding: '背景 / 世界观',
  narrative_pov: '叙事视角',
  pacing: '节奏',
  foreshadowing: '伏笔',
}

function fileAllowed(file: File) {
  const name = file.name.toLowerCase()
  return ALLOWED_EXTENSIONS.some((extension) => name.endsWith(extension))
}

function sourceName(file: File) {
  return file.webkitRelativePath || file.name
}

const fileNameCollator = new Intl.Collator('zh-CN', { numeric: true, sensitivity: 'base' })

function formatKilobytes(bytes: number) {
  return `${Math.max(1, Math.round(bytes / 1024)).toLocaleString('zh-CN')} KB`
}

function sortFiles(files: File[]) {
  return [...files].sort((left, right) => fileNameCollator.compare(sourceName(left), sourceName(right)))
}

function ImportPanel({ importing, previewing, disabled, onInspect, onImport }: Pick<ProjectLibraryProps, 'importing' | 'previewing' | 'onInspect' | 'onImport'> & { disabled: boolean }) {
  const inputId = useId()
  const directoryInputId = useId()
  const inputRef = useRef<HTMLInputElement>(null)
  const directoryInputRef = useRef<HTMLInputElement>(null)
  const [files, setFiles] = useState<File[]>([])
  const [name, setName] = useState('')
  const [dragging, setDragging] = useState(false)
  const [draggedSource, setDraggedSource] = useState<string>()
  const [error, setError] = useState<string>()
  const [preview, setPreview] = useState<ImportInspection>()
  const [selectedSource, setSelectedSource] = useState<string>()

  const totalBytes = useMemo(() => files.reduce((total, item) => total + item.size, 0), [files])
  const selectedDocument = preview?.documents.find((item) => item.sourceName === selectedSource) ?? preview?.documents[0]

  const invalidatePreview = () => {
    setPreview(undefined)
    setSelectedSource(undefined)
  }

  const addFiles = (incoming: File[]) => {
    setError(undefined)
    const invalid = incoming.filter((item) => !fileAllowed(item))
    const valid = incoming.filter(fileAllowed)
    if (invalid.length) setError(`已忽略 ${invalid.length} 个不支持的文件；仅支持 .txt、.md 或 .zip。`)
    if (!valid.length) return
    const duplicates = valid.filter((item) => files.some((current) => sourceName(current) === sourceName(item)))
    if (duplicates.length) {
      setError(`文件名重复：${duplicates.map(sourceName).join('、')}`)
      return
    }
    invalidatePreview()
    const nextFiles = sortFiles([...files, ...valid])
    setFiles(nextFiles)
    if (!name && nextFiles.length === 1) setName(nextFiles[0].name.replace(/\.(txt|md|zip)$/i, ''))
  }

  const handleChange = (event: ChangeEvent<HTMLInputElement>) => {
    addFiles(Array.from(event.target.files ?? []))
    event.target.value = ''
  }

  const handleDirectoryChange = (event: ChangeEvent<HTMLInputElement>) => {
    addFiles(Array.from(event.target.files ?? []))
    event.target.value = ''
  }

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault()
    setDragging(false)
    addFiles(Array.from(event.dataTransfer.files))
  }

  const removeFile = (fileName: string) => {
    invalidatePreview()
    setFiles((current) => current.filter((item) => sourceName(item) !== fileName))
  }

  const reorderPreview = (targetSource: string) => {
    if (!draggedSource || draggedSource === targetSource) return
    setPreview((current) => {
      if (!current) return current
      const documents = [...current.documents]
      const from = documents.findIndex((item) => item.sourceName === draggedSource)
      const to = documents.findIndex((item) => item.sourceName === targetSource)
      if (from < 0 || to < 0) return current
      documents.splice(to, 0, documents.splice(from, 1)[0])
      return { ...current, documents }
    })
    setDraggedSource(undefined)
  }

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!files.length) {
      setError('请先选择素材文件。')
      return
    }
    if (!preview) {
      setError('请先完成素材预检。')
      return
    }
    try {
      await onImport(files, name, preview.documents.map((item) => item.sourceName))
      setFiles([])
      setName('')
      setPreview(undefined)
    } catch {
      // The shared action notice contains the server error.
    }
  }

  const handleInspect = async () => {
    if (!files.length) {
      setError('请先选择素材文件。')
      return
    }
    setError(undefined)
    try {
      const inspection = await onInspect(files)
      setPreview(inspection)
      setSelectedSource(inspection.documents[0]?.sourceName)
      if (!name.trim() && inspection.inferredTitle) setName(inspection.inferredTitle)
    } catch {
      // The shared action notice contains the server error.
    }
  }

  return (
    <section className="import-panel" aria-labelledby="import-title">
      <div className="section-heading">
        <div>
          <h2 id="import-title">素材入库</h2>
          <p>导入原稿或恢复项目包。文件只会发送到本机服务。</p>
        </div>
        <span className="section-index" aria-hidden="true">01</span>
      </div>
      <form className="import-form" onSubmit={handleSubmit}>
        <div
          className={`drop-zone ${dragging ? 'drop-zone--dragging' : ''} ${disabled ? 'drop-zone--disabled' : ''}`}
          onDragEnter={(event) => { event.preventDefault(); setDragging(true) }}
          onDragLeave={() => setDragging(false)}
          onDragOver={(event) => event.preventDefault()}
          onDrop={handleDrop}
        >
          <input
            accept=".txt,.md,.zip,text/plain,text/markdown,application/zip"
            disabled={disabled || importing || previewing}
            id={inputId}
            onChange={handleChange}
            ref={inputRef}
            type="file"
            multiple
          />
          <input
            data-import-source="directory"
            disabled={disabled || importing || previewing}
            id={directoryInputId}
            onChange={handleDirectoryChange}
            ref={(node) => {
              directoryInputRef.current = node
              if (node) node.setAttribute('webkitdirectory', '')
            }}
            type="file"
            multiple
          />
          <button className="drop-zone__file-button" disabled={disabled || importing || previewing} onClick={() => inputRef.current?.click()} type="button">
            <span className="drop-zone__mark"><Icon name="upload" size={25} /></span>
            <strong>{files.length ? `已选择 ${files.length} 份素材` : '拖入原稿，或从电脑选择文件'}</strong>
            <span>{files.length ? `${formatKilobytes(totalBytes)} · 默认按路径排序` : 'TXT · Markdown · 项目 ZIP'}</span>
          </button>
          <Button className="drop-zone__directory-button" disabled={disabled || importing || previewing} icon="folder" onClick={() => directoryInputRef.current?.click()} size="sm">
            选择文件夹
          </Button>
        </div>
        {files.length ? (
          <ul aria-label="待导入文件" className="import-queue">
            {files.map((item) => (
              <li key={sourceName(item)}>
                <span><Icon name="file" size={15} /><strong>{sourceName(item)}</strong><small>{formatKilobytes(item.size)}</small></span>
                <button aria-label={`移除 ${sourceName(item)}`} disabled={importing || previewing} onClick={() => removeFile(sourceName(item))} type="button"><Icon name="close" size={14} /></button>
              </li>
            ))}
          </ul>
        ) : null}
        <label className="field">
          <span>项目名称 <small>可选</small></span>
          <input
            disabled={disabled || importing || previewing}
            maxLength={80}
            onChange={(event) => { setName(event.target.value); if (preview) invalidatePreview() }}
            placeholder="默认使用文件名"
            type="text"
            value={name}
          />
        </label>
        {preview ? (
          <section aria-label="素材预检结果" className="import-preview">
            <header>
              <span><Icon name={preview.recoverable ? 'archive' : 'review'} size={16} /></span>
              <div>
                <strong>{IMPORT_KIND_LABELS[preview.kind] ?? preview.kind}</strong>
                <small>{preview.documentCount} 份文档 · {(preview.totalBytes / 1024).toFixed(1)} KB</small>
              </div>
              <em>预检通过</em>
            </header>
            <dl>
              <div><dt>推断书名</dt><dd>{preview.inferredTitle || '未识别'}</dd></div>
              <div><dt>文本编码</dt><dd>{[...new Set(preview.documents.map((item) => item.encoding))].join(' / ') || '无文本成员'}</dd></div>
              <div><dt>九维档案</dt><dd>{preview.dimensionsFound.length ? preview.dimensionsFound.map((item) => DIMENSION_LABELS[item] ?? item).join('、') : '导入后由分析器生成'}</dd></div>
              <div><dt>恢复能力</dt><dd>{preview.recoverable ? '正式分支、StoryState、源稿与交付物' : '不适用'}</dd></div>
            </dl>
            {preview.warnings.length ? (
              <ul className="import-preview__warnings">
                {preview.warnings.map((warning) => <li key={warning}>{warning}</li>)}
              </ul>
            ) : <p className="import-preview__clean">未发现阻断性警告</p>}
            {preview.documents.length ? (
              <div className="import-document-preview">
                <div className="import-document-preview__list" aria-label="预检文件列表">
                  {preview.documents.map((item, index) => (
                    <button
                      aria-current={selectedDocument?.sourceName === item.sourceName ? 'true' : undefined}
                      aria-label={item.sourceName}
                      className={selectedDocument?.sourceName === item.sourceName ? 'is-selected' : ''}
                      draggable={!preview.recoverable}
                      key={item.sourceName}
                      onClick={() => setSelectedSource(item.sourceName)}
                      onDragEnd={() => setDraggedSource(undefined)}
                      onDragOver={(event) => event.preventDefault()}
                      onDragStart={() => setDraggedSource(item.sourceName)}
                      onDrop={() => reorderPreview(item.sourceName)}
                      type="button"
                    >
                      <small>{String(index + 1).padStart(2, '0')}</small><span>{item.sourceName}</span><em>{formatKilobytes(item.byteSize)}</em>
                    </button>
                  ))}
                </div>
                <article className="import-document-preview__reader" aria-label={`${selectedDocument?.sourceName ?? '素材'} 完整正文`}>
                  <header><strong>{selectedDocument?.sourceName}</strong><small>{selectedDocument?.encoding} · {formatKilobytes(selectedDocument?.byteSize ?? 0)}</small></header>
                  <pre>{selectedDocument?.text || '此成员不包含可阅读文本。'}</pre>
                </article>
              </div>
            ) : null}
          </section>
        ) : null}
        {error ? <p className="field-error" role="alert">{error}</p> : null}
        {preview ? (
          <Button busy={importing} disabled={disabled || previewing} icon="archive" type="submit" variant="primary">
            {importing ? '正在入库' : '确认并创建项目'}
          </Button>
        ) : (
          <Button busy={previewing} disabled={disabled || !files.length || importing} icon="review" onClick={() => void handleInspect()} type="button" variant="primary">
            {previewing ? '正在预检' : '预检素材'}
          </Button>
        )}
      </form>
    </section>
  )
}

export function ProjectLibrary(props: ProjectLibraryProps) {
  const { projects, state, error, serverOnline, onOpen, onRefresh } = props
  return (
    <main className="library-page">
      <header className="library-hero">
        <div>
          <h1>项目书架</h1>
          <p className="library-hero__copy">{state === 'ready' ? `${projects.length} 个本地项目` : '正在同步本地项目索引'}</p>
        </div>
        <div className="library-hero__summary" aria-label="工作区状态">
          <span><strong>{projects.length}</strong><small>本地项目</small></span>
          <span><strong>{serverOnline === true ? '在线' : serverOnline === false ? '离线' : '连接中'}</strong><small>编排服务</small></span>
        </div>
      </header>

      <div className="library-grid">
        <ImportPanel {...props} disabled={serverOnline === false} />
        <section className="project-shelf" aria-labelledby="shelf-title">
          <div className="section-heading">
            <div>
              <h2 id="shelf-title">项目书架</h2>
              <p>{state === 'ready' ? `${projects.length} 个本地项目` : '正在读取本地项目'}</p>
            </div>
            <Button aria-label="刷新项目" icon="refresh" onClick={onRefresh} size="sm" variant="ghost">刷新</Button>
          </div>

          {state === 'loading' ? (
            <div className="skeleton-list" aria-label="正在加载项目"><i /><i /><i /></div>
          ) : state === 'error' ? (
            <EmptyState
              action={<Button icon="refresh" onClick={onRefresh}>重新连接</Button>}
              detail={error ?? '请确认 FastAPI 服务已在 127.0.0.1 启动。'}
              icon="warning"
              title="无法读取项目书架"
            />
          ) : projects.length === 0 ? (
            <EmptyState detail="从左侧导入第一份原稿。这里不会展示演示项目或虚构进度。" icon="book" title="书架还是空的" />
          ) : (
            <div className="project-list">
              {projects.map((project, index) => (
                <button className="project-row" key={project.id} onClick={() => onOpen(project.id)} type="button">
                  <span className="project-row__number">{String(index + 1).padStart(2, '0')}</span>
                  <span className="project-row__main">
                    <strong>{project.name}</strong>
                    <span>更新于 {formatDate(project.updatedAt ?? project.createdAt)}</span>
                  </span>
                  <span className="project-row__meta">
                    {project.sourceCount === undefined ? '素材数待同步' : `${project.sourceCount} 份素材`}
                    <Icon name="chevron-right" size={17} />
                  </span>
                </button>
              ))}
            </div>
          )}
        </section>
      </div>
    </main>
  )
}
