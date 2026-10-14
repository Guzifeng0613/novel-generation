import { useCallback, useState } from 'react'
import { AppHeader, type AppView } from './components/AppHeader'
import { Button } from './components/Button'
import { EmptyState } from './components/EmptyState'
import { Icon } from './components/Icon'
import { ProjectLibrary } from './features/library/ProjectLibrary'
import { PromptPage, ProviderPage, RunCenter } from './features/system/SystemPages'
import { WorkspacePage } from './features/workspace/WorkspacePage'
import { useWorkspace } from './hooks/useWorkspace'

export default function App() {
  const workspace = useWorkspace()
  const [view, setView] = useState<AppView>('library')

  const openProject = useCallback((id: string) => {
    workspace.setSelectedProjectId(id)
    setView('workspace')
  }, [workspace.setSelectedProjectId])

  const importAndOpen = useCallback(async (files: File[], name?: string, order?: string[]) => {
    const project = await workspace.importProject(files, name, order)
    setView('workspace')
    return project
  }, [workspace.importProject])

  const navigate = useCallback((nextView: AppView) => {
    setView(nextView)
  }, [])

  const openRun = useCallback((runId: string, projectId?: string) => {
    if (projectId && projectId !== workspace.selectedProjectId) workspace.setSelectedProjectId(projectId)
    void workspace.loadRun(runId).catch(() => undefined)
    setView('workspace')
  }, [workspace.loadRun, workspace.selectedProjectId, workspace.setSelectedProjectId])

  let content
  if (view === 'workspace') {
    if (workspace.projectState === 'loading' || workspace.projectState === 'idle') {
      content = <main className="loading-page" aria-label="正在打开项目"><span className="loading-mark">续</span><p>正在铺开稿件与项目索引…</p></main>
    } else if (workspace.projectState === 'error' || !workspace.project) {
      content = (
        <main className="system-page">
          <EmptyState
            action={<Button icon="arrow-left" onClick={() => setView('library')}>返回书架</Button>}
            detail={workspace.projectError ?? '项目不存在或本地服务未返回项目详情。'}
            icon="warning"
            title="无法打开项目"
          />
        </main>
      )
    } else {
      content = (
        <WorkspacePage
          action={workspace.action}
          onApproveOutline={workspace.approveOutline}
          onBack={() => setView('library')}
          onControlRun={workspace.controlRun}
          onReviseOutline={workspace.reviseOutline}
          onRetryTask={workspace.retryTask}
          onUpdateChapter={workspace.updateChapterContent}
          onSelectRun={workspace.selectRun}
          onStartAnalysis={workspace.startAnalysis}
          onStartContinuation={workspace.startContinuation}
          project={workspace.project}
          providers={workspace.providers}
          run={workspace.run}
          runError={workspace.runError}
        />
      )
    }
  } else if (view === 'runs') {
    content = <RunCenter currentRun={workspace.run} onOpenProject={openProject} onOpenRun={openRun} projects={workspace.projects} />
  } else if (view === 'providers') {
    content = <ProviderPage busy={workspace.action === 'provider_keys'} error={workspace.providersError} onSaveKeys={workspace.saveProviderKeys} status={workspace.providers} />
  } else if (view === 'prompts') {
    content = <PromptPage error={workspace.promptVersionsError} status={workspace.promptVersions} />
  } else {
    content = (
      <ProjectLibrary
        error={workspace.projectsError}
        importing={workspace.action === 'import'}
        onInspect={workspace.inspectImport}
        onImport={importAndOpen}
        onOpen={openProject}
        onRefresh={() => void workspace.refreshInitial()}
        projects={workspace.projects}
        previewing={workspace.action === 'inspect_import'}
        serverOnline={workspace.serverOnline}
        state={workspace.projectsState}
      />
    )
  }

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">跳到主要内容</a>
      <AppHeader
        onNavigate={navigate}
        providers={workspace.providers}
        providersError={workspace.providersError}
        serverOnline={workspace.serverOnline}
        view={view}
      />
      {workspace.serverOnline === false ? (
        <div className="offline-banner" role="status">
          <Icon name="warning" size={16} />
          <span><strong>本地编排服务未连接。</strong> 当前页面不会显示演示数据；请启动 FastAPI 服务后重新连接。</span>
          <Button icon="refresh" onClick={() => void workspace.refreshInitial()} size="sm" variant="ghost">重新连接</Button>
        </div>
      ) : null}
      <div id="main-content">{content}</div>
      {workspace.notice ? (
        <div
          aria-live="polite"
          className={`toast${view === 'workspace' ? ' toast--workspace' : ''}`}
          role="status"
        >
          <span>{workspace.notice}</span>
          <button aria-label="关闭通知" onClick={() => workspace.setNotice(undefined)} type="button"><Icon name="close" size={16} /></button>
        </div>
      ) : null}
    </div>
  )
}
