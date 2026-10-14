import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api'
import type { ProjectDetail, RunDetail } from '../types'
import { useWorkspace } from './useWorkspace'

class FakeEventSource {
  onopen: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent<string>) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  readonly close = vi.fn()
  private readonly listeners = new Map<string, EventListener[]>()

  addEventListener(name: string, listener: EventListener) {
    const current = this.listeners.get(name) ?? []
    current.push(listener)
    this.listeners.set(name, current)
  }

  emit(name: string, payload: unknown) {
    const event = new MessageEvent(name, { data: JSON.stringify(payload) })
    for (const listener of this.listeners.get(name) ?? []) listener(event)
  }
}

const runningRun: RunDetail = {
  id: 'run-1',
  projectId: 'project-1',
  kind: 'continuation',
  status: 'running',
  stage: 'writing',
  chapters: [],
  reviewIssues: [],
  tasks: [],
  artifacts: [],
  metrics: {},
}

const completedRun: RunDetail = {
  ...runningRun,
  status: 'completed',
  stage: 'delivery',
}

function project(canonTitle: string): ProjectDetail {
  return {
    id: 'project-1',
    name: '雨站',
    activeBranch: 'main',
    sources: [],
    artifacts: [],
    runs: [{
      id: 'run-1',
      kind: 'continuation',
      status: canonTitle === '终态 Canon' ? 'completed' : 'running',
    }],
    storyState: {
      canon: [{ id: 'canon-1', title: canonTitle }],
      suspense: [],
      entities: [],
      conflicts: [],
      worldRules: [],
    },
  }
}

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('useWorkspace run recovery', () => {
  it('polls and reconnects after SSE failure, then refreshes project memory at terminal state', async () => {
    vi.useFakeTimers()
    const sources: FakeEventSource[] = []
    vi.spyOn(api, 'health').mockResolvedValue(true)
    vi.spyOn(api, 'listProjects').mockResolvedValue([])
    vi.spyOn(api, 'providers').mockResolvedValue({ providers: [] })
    vi.spyOn(api, 'promptVersions').mockResolvedValue({ prompts: [] })
    const getProject = vi.spyOn(api, 'getProject')
      .mockResolvedValueOnce(project('旧 Canon'))
      .mockResolvedValue(project('终态 Canon'))
    const getRun = vi.spyOn(api, 'getRun').mockResolvedValue(runningRun)
    vi.spyOn(api, 'eventSource').mockImplementation(() => {
      const source = new FakeEventSource()
      sources.push(source)
      return source as unknown as EventSource
    })

    const { result } = renderHook(() => useWorkspace())
    act(() => result.current.setSelectedProjectId('project-1'))
    await vi.waitFor(() => expect(sources).toHaveLength(1))

    act(() => sources[0].onerror?.(new Event('error')))
    await vi.waitFor(() => expect(getRun.mock.calls.length).toBeGreaterThanOrEqual(2))
    expect(sources[0].close).toHaveBeenCalled()

    await act(async () => vi.advanceTimersByTimeAsync(1_000))
    expect(sources).toHaveLength(2)

    act(() => sources[1].emit('complete', { type: 'run_completed', run: completedRun }))
    await vi.waitFor(() => expect(result.current.run?.status).toBe('completed'))
    await vi.waitFor(() => expect(getProject).toHaveBeenCalledTimes(2))
    await vi.waitFor(() => {
      expect(result.current.project?.storyState?.canon[0].title).toBe('终态 Canon')
    })

    await act(async () => vi.advanceTimersByTimeAsync(30_000))
    expect(sources).toHaveLength(2)
  })
})
