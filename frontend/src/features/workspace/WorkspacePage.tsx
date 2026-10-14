import { useState } from 'react'
import type { ProjectDetail, ProviderStatus, ReviewIssue, RunDetail } from '../../types'
import { Button } from '../../components/Button'
import { EmptyState } from '../../components/EmptyState'
import { Icon } from '../../components/Icon'
import { StatusMark } from '../../components/StatusMark'
import { AssetSidebar } from './AssetSidebar'
import { ChapterView } from './ChapterView'
import { ContinuationDialog } from './ContinuationDialog'
import { DiffView } from './DiffView'
import { Inspector } from './Inspector'
import { OutlineView } from './OutlineView'
import { StatusDock } from './StatusDock'
import { SummarizerDialog } from './SummarizerDialog'
import { TaskRecoveryPanel } from './TaskRecoveryPanel'
import { WorkflowRail } from './WorkflowRail'

type CenterTab = 'outline' | 'chapter' | 'diff'

export function WorkspacePage({
  project,
  run,
  providers,
  runError,
  action,
  onBack,
  onSelectRun,
  onStartAnalysis,
  onStartContinuation,
  onApproveOutline,
  onReviseOutline,
  onControlRun,
  onRetryTask,
  onUpdateChapter,
}: {
  project: ProjectDetail
  run?: RunDetail
  providers: ProviderStatus
  runError?: string
  action?: string
  onBack: () => void
  onSelectRun: (id: string) => void
  onStartAnalysis: Parameters<typeof SummarizerDialog>[0]['onSubmit']
  onStartContinuation: Parameters<typeof ContinuationDialog>[0]['onSubmit']
  onApproveOutline: (allowCanonOverrides?: boolean) => Promise<unknown>
  onReviseOutline: (feedback: string, chapterId?: string) => Promise<unknown>
  onControlRun: (action: 'pause' | 'resume' | 'cancel') => Promise<unknown>
  onRetryTask: (taskId: string, acknowledgeIndeterminate?: boolean) => Promise<unknown>
  onUpdateChapter?: (chapterId: string, input: { content: string; baseRevision: number; baseSha256: string; reason?: string }) => Promise<unknown>
}) {
  const [tab, setTab] = useState<CenterTab>('outline')
  const [selectedChapterId, setSelectedChapterId] = useState<string>()
  const [highlightedIssue, setHighlightedIssue] = useState<ReviewIssue>()
  const [summarizerOpen, setSummarizerOpen] = useState(false)
  const [continuationOpen, setContinuationOpen] = useState(false)
  const [inspectorOpen, setInspectorOpen] = useState(false)
  const currentChapter = run?.chapters.find((item) => item.id === selectedChapterId) ?? run?.chapters[0]
  const currentHighlight = highlightedIssue?.chapterId === currentChapter?.id ? highlightedIssue : undefined

  function locateIssue(issue: ReviewIssue) {
    if (!issue.chapterId || !run?.chapters.some((chapter) => chapter.id === issue.chapterId)) return
    setSelectedChapterId(issue.chapterId)
    setHighlightedIssue(issue)
    setTab('chapter')
    setInspectorOpen(false)
  }

  function compareIssue(issue: ReviewIssue) {
    if (!issue.chapterId || !run?.chapters.some((chapter) => chapter.id === issue.chapterId)) return
    setSelectedChapterId(issue.chapterId)
    setHighlightedIssue(issue)
    setTab('diff')
    setInspectorOpen(false)
  }

  return (
    <main className="workspace-page">
      <header className="project-bar">
        <div className="project-bar__identity">
          <Button aria-label="返回项目书架" icon="arrow-left" onClick={onBack} size="sm" variant="ghost">书架</Button>
          <span aria-hidden="true" />
          <div><h1>{project.name}</h1><p>{project.description ?? '本地小说项目'}</p></div>
        </div>
        <div className="project-bar__actions">
          {run ? <StatusMark status={run.status} /> : null}
          <Button busy={action === 'analysis'} icon="layers" onClick={() => setSummarizerOpen(true)}>总结器</Button>
          <Button icon="spark" onClick={() => setContinuationOpen(true)} variant="primary">新建续写</Button>
          <Button className="inspector-trigger" icon="review" onClick={() => setInspectorOpen(true)}>审稿台</Button>
        </div>
      </header>
      <WorkflowRail run={run} />

      <div className="workspace-notices">
        {runError ? (
          <div className="workspace-alert" role="alert"><Icon name="warning" size={17} /><span><strong>运行详情读取失败</strong>{runError}</span></div>
        ) : null}
        {run ? <TaskRecoveryPanel action={action} onRetry={onRetryTask} tasks={run.tasks} /> : null}
        {run?.contentRevisionInvalidated ? (
          <div className="workspace-alert workspace-alert--warning" role="status"><Icon name="warning" size={17} /><span><strong>审校与交付已失效</strong>正文包含人工修改，重新审校前不能下载旧交付物。</span></div>
        ) : null}
        <div className="mobile-monitor-note">
          <Icon name="book" size={18} />
          <p><strong>移动端监看模式</strong>正文编辑与版本差异请在桌面端查看；当前设备仍可审批章纲、控制运行和下载交付物。</p>
        </div>
      </div>

      <div className="workspace-grid">
        <AssetSidebar
          onSelectChapter={(chapter) => { setSelectedChapterId(chapter.id); setHighlightedIssue(undefined); setTab('chapter') }}
          onSelectRun={onSelectRun}
          project={project}
          run={run}
          selectedChapterId={currentChapter?.id}
        />
        <section className="editor-center">
          <header className="editor-tabs">
            <nav aria-label="内容视图">
              <button aria-selected={tab === 'outline'} className={tab === 'outline' ? 'is-active' : ''} onClick={() => setTab('outline')} role="tab" type="button">章纲审批</button>
              <button aria-selected={tab === 'chapter'} className={tab === 'chapter' ? 'is-active' : ''} onClick={() => setTab('chapter')} role="tab" type="button">章节正文</button>
              <button aria-selected={tab === 'diff'} className={tab === 'diff' ? 'is-active' : ''} onClick={() => setTab('diff')} role="tab" type="button">版本差异</button>
            </nav>
            <span className="editor-tabs__context">{tab === 'outline'
              ? run?.plan?.approvedHash
                ? `已冻结 · ${run.plan.approvedHash.slice(0, 8)}`
                : run?.plan?.planHash
                  ? `待批准 · ${run.plan.planHash.slice(0, 8)}`
                  : '待总编决定'
              : currentChapter?.title ?? '未选择章节'}</span>
          </header>
          <div className={`editor-scroll ${tab !== 'outline' ? 'desktop-authoring' : ''}`} role="tabpanel">
            {tab === 'outline' ? (
              <OutlineView
                activeBranch={project.activeBranch ?? 'main'}
                action={action}
                onApprove={onApproveOutline}
                onCreateContinuation={() => setContinuationOpen(true)}
                onRevise={onReviseOutline}
                run={run}
              />
            ) : tab === 'chapter' ? (
              <ChapterView
                busy={action === `edit:${currentChapter?.id}`}
                chapter={currentChapter}
                highlightedIssue={currentHighlight}
                onSave={(input) => currentChapter && onUpdateChapter ? onUpdateChapter(currentChapter.id, input) : Promise.resolve()}
              />
            ) : <DiffView chapter={currentChapter} />}
          </div>
        </section>
        <Inspector onCompareIssue={compareIssue} onLocateIssue={locateIssue} project={project} run={run} />
      </div>
      <StatusDock action={action} onControl={onControlRun} run={run} />

      {inspectorOpen ? (
        <>
          <div aria-hidden="true" className="drawer-backdrop" onClick={() => setInspectorOpen(false)} />
          <div className="inspector-drawer-shell is-open">
            <Inspector
              drawer
              onClose={() => setInspectorOpen(false)}
              onLocateIssue={locateIssue}
              onCompareIssue={compareIssue}
              project={project}
              run={run}
            />
          </div>
        </>
      ) : null}
      <SummarizerDialog
        busy={action === 'analysis'}
        onClose={() => setSummarizerOpen(false)}
        onSubmit={onStartAnalysis}
        open={summarizerOpen}
        providers={providers}
      />
      <ContinuationDialog
        activeBranch={project.activeBranch ?? 'main'}
        busy={action === 'continuation'}
        onClose={() => setContinuationOpen(false)}
        onSubmit={onStartContinuation}
        open={continuationOpen}
        providers={providers}
      />
    </main>
  )
}
