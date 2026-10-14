import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, normalizeImportInspection, normalizePromptVersions, normalizeRunDetail } from './api'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('run response normalization', () => {
  it('preserves structured plan context and derives its approval hash', () => {
    const run = normalizeRunDetail({
      id: 'run-1',
      kind: 'continuation',
      status: 'awaiting_outline_approval',
      plan: {
        plan_id: 'plan-1',
        style_card: {
          pov_strategy: ['第三人称限知'],
          dialogue_rules: ['对白保持克制'],
        },
        canon_conflicts: [{ explanation: '死亡角色不能复活' }],
        chapters: [],
      },
      artifacts: [{ id: 'artifact-1', kind: 'run_plan', title: '章纲', sha256: 'abc123' }],
    })

    expect(run.plan?.styleCard).toContain('视角策略：第三人称限知')
    expect(run.plan?.canonConflicts).toEqual(['死亡角色不能复活'])
    expect(run.plan?.planHash).toBe('abc123')
  })

  it('keeps actionable indeterminate task metadata', () => {
    const run = normalizeRunDetail({
      id: 'run-1',
      kind: 'continuation',
      status: 'indeterminate',
      tasks: [{
        id: 'task-1',
        operation: 'write_chapter',
        status: 'indeterminate',
        attempt_count: 1,
        max_attempts: 3,
        retryable: true,
        requires_acknowledgement: true,
        error_message: 'response persistence is uncertain',
      }],
    })

    expect(run.tasks).toEqual([expect.objectContaining({
      id: 'task-1',
      retryable: true,
      requiresAcknowledgement: true,
      attemptCount: 1,
      maxAttempts: 3,
    })])
  })

  it('sends explicit acknowledgement when retrying an indeterminate task', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      run: { id: 'run-1', kind: 'continuation', status: 'queued' },
    }), { status: 202, headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)

    await api.retryTask('run-1', 'task-1', { acknowledgeIndeterminate: true })

    expect(fetchMock).toHaveBeenCalledOnce()
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(JSON.parse(String(init.body))).toEqual({ acknowledge_indeterminate: true })
  })
})

describe('provider key API', () => {
  it('sends only the keys supplied by the form and normalizes the returned status', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      providers: [{ id: 'openai', name: 'OpenAI', configured: true }],
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)

    const providers = await api.saveProviderKeys({ openaiApiKey: ' key-value ' })

    expect(fetchMock).toHaveBeenCalledOnce()
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/providers/keys')
    expect(JSON.parse(String(init.body))).toEqual({ openai_api_key: 'key-value' })
    expect(providers.providers).toEqual([expect.objectContaining({ id: 'openai', configured: true })])
  })
})

describe('prompt version normalization', () => {
  it('keeps the immutable server version and content hash', () => {
    const status = normalizePromptVersions({
      prompts: [{ file: 'planner.md', name: '规划 / 主 A', version: '1.2.0', sha256: 'b'.repeat(64) }],
    })

    expect(status.prompts).toEqual([{
      file: 'planner.md',
      name: '规划 / 主 A',
      version: '1.2.0',
      sha256: 'b'.repeat(64),
    }])
  })

  it('normalizes review issue offsets and resolved references while keeping legacy fields', () => {
    const run = normalizeRunDetail({
      id: 'run-1',
      kind: 'continuation',
      status: 'completed',
      review_issues: [{
        issue_id: 'ISS-1',
        severity: 'major',
        dimension: 'canon_world_rules',
        description: '伤势无治疗恢复',
        location: '左臂段落',
        start_offset: 8,
        end_offset: 16,
        chapter_id: 'chapter-1',
        references: [{
          id: 'canon-injury',
          kind: 'canon',
          title: '林岚左臂骨折',
          detail: '伤势',
        }],
      }],
    })

    expect(run.reviewIssues[0]).toEqual(expect.objectContaining({
      id: 'ISS-1',
      startOffset: 8,
      endOffset: 16,
      chapterId: 'chapter-1',
      references: [expect.objectContaining({ id: 'canon-injury', kind: 'canon' })],
    }))
  })
})

describe('import inspection normalization', () => {
  it('keeps relative directory paths in multipart upload names', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      inspection: { kind: 'raw_text', documents: [] },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)
    const file = new File(['正文'], '第1章.txt', { type: 'text/plain' })
    Object.defineProperty(file, 'webkitRelativePath', { value: '原稿/第一卷/第1章.txt' })

    await api.inspectImport([file])

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    const uploaded = (init.body as FormData).get('files') as File
    expect(uploaded.name).toBe('原稿/第一卷/第1章.txt')
  })

  it('keeps encoding, dimensions and recovery capabilities', () => {
    const inspection = normalizeImportInspection({
      inspection: {
        kind: 'project_bundle',
        inferred_title: '雨站',
        warnings: ['历史调用不会恢复'],
        document_count: 2,
        total_bytes: 2048,
        documents: [{ source_name: 'source.txt', encoding: 'utf-16-le', byte_size: 1024, text: '完整正文' }],
        dimensions_found: ['characters'],
        recoverable: true,
        recovery_capabilities: { story_state: true, run_history: false },
      },
    })

    expect(inspection.inferredTitle).toBe('雨站')
    expect(inspection.documents[0].encoding).toBe('utf-16-le')
    expect(inspection.documents[0].text).toBe('完整正文')
    expect(inspection.dimensionsFound).toEqual(['characters'])
    expect(inspection.recoveryCapabilities).toEqual({ story_state: true, run_history: false })
  })
})
