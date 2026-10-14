import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, api, parseRunEvent } from '../api'
import type {
  ContinuationInput,
  ProjectDetail,
  ProjectSummary,
  ProviderStatus,
  PromptVersionStatus,
  RunDetail,
  SummarizerInput,
} from '../types'
import { ACTIVE_RUN_STATUSES } from '../types'

type AsyncState = 'idle' | 'loading' | 'ready' | 'error'

const TERMINAL_RUN_STATUSES = new Set(['completed', 'failed', 'cancelled', 'needs_human', 'indeterminate'])
const SSE_RECONNECT_BASE_MS = 1_000
const SSE_RECONNECT_MAX_MS = 15_000
const DISCONNECTED_POLL_MS = 3_000

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return '发生未知错误'
}

function upsertProject(items: ProjectSummary[], project: ProjectSummary) {
  const index = items.findIndex((item) => item.id === project.id)
  if (index === -1) return [project, ...items]
  return items.map((item) => (item.id === project.id ? { ...item, ...project } : item))
}

export function useWorkspace() {
  const [projects, setProjects] = useState<ProjectSummary[]>([])
  const [projectsState, setProjectsState] = useState<AsyncState>('loading')
  const [projectsError, setProjectsError] = useState<string>()
  const [providers, setProviders] = useState<ProviderStatus>({ providers: [] })
  const [providersError, setProvidersError] = useState<string>()
  const [promptVersions, setPromptVersions] = useState<PromptVersionStatus>({ prompts: [] })
  const [promptVersionsError, setPromptVersionsError] = useState<string>()
  const [serverOnline, setServerOnline] = useState<boolean | undefined>()
  const [selectedProjectId, setSelectedProjectId] = useState<string>()
  const [project, setProject] = useState<ProjectDetail>()
  const [projectState, setProjectState] = useState<AsyncState>('idle')
  const [projectError, setProjectError] = useState<string>()
  const [run, setRun] = useState<RunDetail>()
  const [runState, setRunState] = useState<AsyncState>('idle')
  const [runError, setRunError] = useState<string>()
  const [action, setAction] = useState<string>()
  const [notice, setNotice] = useState<string>()
  const terminalRefreshKey = useRef<string | undefined>(undefined)

  const refreshInitial = useCallback(async () => {
    setProjectsState('loading')
    setProjectsError(undefined)
    const controller = new AbortController()
    const [healthResult, projectsResult, providersResult, promptsResult] = await Promise.allSettled([
      api.health(),
      api.listProjects(controller.signal),
      api.providers(controller.signal),
      api.promptVersions(controller.signal),
    ])

    setServerOnline(healthResult.status === 'fulfilled')
    if (projectsResult.status === 'fulfilled') {
      setProjects(projectsResult.value)
      setProjectsState('ready')
    } else {
      setProjects([])
      setProjectsState('error')
      setProjectsError(errorMessage(projectsResult.reason))
    }
    if (providersResult.status === 'fulfilled') {
      setProviders(providersResult.value)
      setProvidersError(undefined)
    } else {
      setProviders({ providers: [] })
      setProvidersError(errorMessage(providersResult.reason))
    }
    if (promptsResult.status === 'fulfilled') {
      setPromptVersions(promptsResult.value)
      setPromptVersionsError(undefined)
    } else {
      setPromptVersions({ prompts: [] })
      setPromptVersionsError(errorMessage(promptsResult.reason))
    }
  }, [])

  useEffect(() => {
    void refreshInitial()
  }, [refreshInitial])

  const loadRun = useCallback(async (runId: string) => {
    setRunState('loading')
    setRunError(undefined)
    try {
      const nextRun = await api.getRun(runId)
      setRun(nextRun)
      setRunState('ready')
      return nextRun
    } catch (error) {
      setRunError(errorMessage(error))
      setRunState('error')
      throw error
    }
  }, [])

  useEffect(() => {
    if (!selectedProjectId) {
      setProject(undefined)
      setProjectState('idle')
      setRun(undefined)
      setRunState('idle')
      return
    }

    const controller = new AbortController()
    setProjectState('loading')
    setProjectError(undefined)
    void api.getProject(selectedProjectId, controller.signal).then(async (nextProject) => {
      setProject(nextProject)
      setProjectState('ready')
      setProjects((current) => upsertProject(current, nextProject))
      const latestRunId = nextProject.latestRunId ?? nextProject.runs[0]?.id
      if (latestRunId) {
        try {
          await loadRun(latestRunId)
        } catch {
          // The run error is displayed independently; project navigation remains usable.
        }
      } else {
        setRun(undefined)
        setRunState('idle')
      }
    }).catch((error) => {
      if (controller.signal.aborted) return
      setProjectError(errorMessage(error))
      setProjectState('error')
    })
    return () => controller.abort()
  }, [loadRun, selectedProjectId])

  useEffect(() => {
    const runId = run?.id
    if (!runId || !ACTIVE_RUN_STATUSES.has(run.status)) return
    let disposed = false
    let source: EventSource | undefined
    let refreshTimer: number | undefined
    let reconnectTimer: number | undefined
    let pollTimer: number | undefined
    let reconnectAttempt = 0

    const clearTimer = (timer: number | undefined, kind: 'timeout' | 'interval') => {
      if (timer === undefined) return
      if (kind === 'timeout') window.clearTimeout(timer)
      else window.clearInterval(timer)
    }

    const stopTransport = () => {
      source?.close()
      source = undefined
      clearTimer(reconnectTimer, 'timeout')
      reconnectTimer = undefined
      clearTimer(pollTimer, 'interval')
      pollTimer = undefined
    }

    const applySnapshot = (nextRun: RunDetail) => {
      if (disposed) return
      setRun(nextRun)
      setRunState('ready')
      setRunError(undefined)
      if (!ACTIVE_RUN_STATUSES.has(nextRun.status)) stopTransport()
    }

    const refreshSnapshot = async () => {
      try {
        applySnapshot(await api.getRun(runId))
      } catch (error) {
        if (!disposed) setRunError(errorMessage(error))
      }
    }

    const refreshRunSoon = () => {
      clearTimer(refreshTimer, 'timeout')
      refreshTimer = window.setTimeout(() => void refreshSnapshot(), 250)
    }

    const onEvent = (event: MessageEvent<string>) => {
      const parsed = parseRunEvent(event.data, event.type)
      if (parsed.run) {
        applySnapshot(parsed.run)
      } else {
        refreshRunSoon()
      }
      if (parsed.message) setNotice(parsed.message)
    }

    const startPolling = () => {
      if (pollTimer !== undefined || disposed) return
      void refreshSnapshot()
      pollTimer = window.setInterval(() => void refreshSnapshot(), DISCONNECTED_POLL_MS)
    }

    const connect = () => {
      if (disposed) return
      let nextSource: EventSource
      try {
        nextSource = api.eventSource(runId)
      } catch {
        startPolling()
        const delay = Math.min(SSE_RECONNECT_BASE_MS * (2 ** reconnectAttempt), SSE_RECONNECT_MAX_MS)
        reconnectAttempt += 1
        reconnectTimer = window.setTimeout(connect, delay)
        return
      }
      source = nextSource
      nextSource.onopen = () => {
        if (disposed || source !== nextSource) return
        reconnectAttempt = 0
        clearTimer(pollTimer, 'interval')
        pollTimer = undefined
      }
      nextSource.onmessage = onEvent
      for (const eventName of ['snapshot', 'stage', 'task', 'usage', 'complete', 'failed']) {
        nextSource.addEventListener(eventName, onEvent as EventListener)
      }
      nextSource.onerror = () => {
        if (disposed || source !== nextSource) return
        nextSource.close()
        source = undefined
        startPolling()
        const delay = Math.min(SSE_RECONNECT_BASE_MS * (2 ** reconnectAttempt), SSE_RECONNECT_MAX_MS)
        reconnectAttempt += 1
        reconnectTimer = window.setTimeout(connect, delay)
      }
    }

    connect()
    return () => {
      disposed = true
      stopTransport()
      clearTimer(refreshTimer, 'timeout')
    }
  }, [run?.id, run?.status])

  const importProject = useCallback(async (files: File[], name?: string, order?: string[]) => {
    setAction('import')
    setNotice(undefined)
    try {
      const imported = await api.importProject(files, name, order)
      setProjects((current) => upsertProject(current, imported))
      setSelectedProjectId(imported.id)
      setProject(imported)
      setProjectState('ready')
      setNotice('素材已入库，项目清单已更新。')
      return imported
    } catch (error) {
      setNotice(errorMessage(error))
      throw error
    } finally {
      setAction(undefined)
    }
  }, [])

  const inspectImport = useCallback(async (files: File[]) => {
    setAction('inspect_import')
    setNotice(undefined)
    try {
      const inspection = await api.inspectImport(files)
      setNotice('素材预检完成，请核对编码、文档与警告后再创建项目。')
      return inspection
    } catch (error) {
      setNotice(errorMessage(error))
      throw error
    } finally {
      setAction(undefined)
    }
  }, [])

  const saveProviderKeys = useCallback(async (keys: { openaiApiKey?: string; deepseekApiKey?: string }) => {
    setAction('provider_keys')
    setNotice(undefined)
    try {
      const nextProviders = await api.saveProviderKeys(keys)
      setProviders(nextProviders)
      setProvidersError(undefined)
      setNotice('API 密钥已保存，模型路由配置已更新。')
      return nextProviders
    } catch (error) {
      const message = errorMessage(error)
      setNotice(message)
      throw error
    } finally {
      setAction(undefined)
    }
  }, [])

  const startAnalysis = useCallback(async (input: SummarizerInput) => {
    if (!selectedProjectId) return
    setAction('analysis')
    try {
      const nextRun = await api.startAnalysis(selectedProjectId, input)
      setRun(nextRun)
      setRunState('ready')
      setNotice('总结器任务已提交。')
      return nextRun
    } catch (error) {
      setNotice(errorMessage(error))
      throw error
    } finally {
      setAction(undefined)
    }
  }, [selectedProjectId])

  const startContinuation = useCallback(async (input: ContinuationInput) => {
    if (!selectedProjectId) return
    setAction('continuation')
    try {
      const nextRun = await api.startContinuation(selectedProjectId, input)
      setRun(nextRun)
      setRunState('ready')
      setNotice('续写计划已提交，主编正在生成章纲。')
      return nextRun
    } catch (error) {
      setNotice(errorMessage(error))
      throw error
    } finally {
      setAction(undefined)
    }
  }, [selectedProjectId])

  const runMutation = useCallback(async (name: string, mutation: () => Promise<RunDetail>, successMessage: string) => {
    setAction(name)
    try {
      const nextRun = await mutation()
      setRun(nextRun)
      setRunState('ready')
      setNotice(successMessage)
      return nextRun
    } catch (error) {
      setNotice(errorMessage(error))
      throw error
    } finally {
      setAction(undefined)
    }
  }, [])

  const approveOutline = useCallback(async (allowCanonOverrides = false) => {
    if (!run) return
    return runMutation(
      'approve',
      () => api.approveOutline(run.id, { planHash: run.plan?.planHash, allowCanonOverrides }),
      allowCanonOverrides ? '分支覆盖已确认，章纲冻结并开始写作。' : '章纲已冻结，写作队列开始执行。',
    )
  }, [run, runMutation])

  const reviseOutline = useCallback(async (feedback: string, chapterId?: string) => {
    if (!run) return
    return runMutation('revise', () => api.reviseOutline(run.id, feedback, chapterId), '退回意见已提交，等待新版章纲。')
  }, [run, runMutation])

  const controlRun = useCallback(async (control: 'pause' | 'resume' | 'cancel') => {
    if (!run) return
    const messages = { pause: '已停止派发新任务，在途调用将安全落盘。', resume: '运行已恢复。', cancel: '运行已取消，部分产物仍保留。' }
    return runMutation(control, () => api.controlRun(run.id, control), messages[control])
  }, [run, runMutation])

  const retryTask = useCallback(async (taskId: string, acknowledgeIndeterminate = false) => {
    if (!run) return
    return runMutation(
      `retry:${taskId}`,
      () => api.retryTask(run.id, taskId, { acknowledgeIndeterminate }),
      acknowledgeIndeterminate
        ? '已确认可能重复计费，任务重新进入队列。'
        : '任务已重新进入队列。',
    )
  }, [run, runMutation])

  const updateChapterContent = useCallback(async (
    chapterId: string,
    input: { content: string; baseRevision: number; baseSha256: string; reason?: string },
  ) => {
    if (!run) return
    return runMutation(
      `edit:${chapterId}`,
      () => api.updateChapterContent(run.id, chapterId, input),
      '正文已保存为新版本，原审校与交付已失效。',
    )
  }, [run, runMutation])

  const selectRun = useCallback((runId: string) => {
    void loadRun(runId).catch(() => undefined)
  }, [loadRun])

  const refreshProject = useCallback(async (silent = false) => {
    if (!selectedProjectId) return
    if (!silent) setProjectState('loading')
    try {
      const nextProject = await api.getProject(selectedProjectId)
      setProject(nextProject)
      setProjects((current) => upsertProject(current, nextProject))
      setProjectError(undefined)
      setProjectState('ready')
    } catch (error) {
      setProjectError(errorMessage(error))
      if (!silent) setProjectState('error')
    }
  }, [selectedProjectId])

  useEffect(() => {
    if (!run || !selectedProjectId || !TERMINAL_RUN_STATUSES.has(run.status)) return
    if (run.projectId && run.projectId !== selectedProjectId) return
    const refreshKey = `${selectedProjectId}:${run.id}:${run.status}`
    if (terminalRefreshKey.current === refreshKey) return
    terminalRefreshKey.current = refreshKey
    void refreshProject(true)
  }, [refreshProject, run?.id, run?.status, selectedProjectId])

  return {
    projects,
    projectsState,
    projectsError,
    providers,
    providersError,
    promptVersions,
    promptVersionsError,
    serverOnline,
    selectedProjectId,
    setSelectedProjectId,
    project,
    projectState,
    projectError,
    run,
    runState,
    runError,
    action,
    notice,
    setNotice,
    refreshInitial,
    refreshProject,
    loadRun,
    selectRun,
    importProject,
    inspectImport,
    saveProviderKeys,
    startAnalysis,
    startContinuation,
    approveOutline,
    reviseOutline,
    controlRun,
    retryTask,
    updateChapterContent,
  }
}
