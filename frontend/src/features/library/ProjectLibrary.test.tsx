import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { ProjectLibrary } from './ProjectLibrary'

const inspection = {
  kind: 'raw_text',
  inferredTitle: '雨站',
  warnings: [],
  documentCount: 2,
  totalBytes: 256,
  documents: [
    { sourceName: '第2章.txt', encoding: 'utf-8', byteSize: 128, text: '第二章完整正文' },
    { sourceName: '第10章.txt', encoding: 'utf-8', byteSize: 128, text: '第十章完整正文' },
  ],
  dimensionsFound: [],
  recoverable: false,
  recoveryCapabilities: {},
}

describe('ProjectLibrary import preflight', () => {
  it('previews a batch and sends its confirmed document order', async () => {
    const user = userEvent.setup()
    const onInspect = vi.fn().mockResolvedValue(inspection)
    const onImport = vi.fn().mockResolvedValue({ id: 'project-1' })
    const { container } = render(
      <ProjectLibrary importing={false} onImport={onImport} onInspect={onInspect} onOpen={vi.fn()} onRefresh={vi.fn()} previewing={false} projects={[]} serverOnline state="ready" />,
    )
    const fileInput = container.querySelector<HTMLInputElement>('input[type="file"]')
    await user.upload(fileInput!, [
      new File(['第十章'], '第10章.txt', { type: 'text/plain' }),
      new File(['第二章'], '第2章.txt', { type: 'text/plain' }),
    ])

    expect(screen.getByText('已选择 2 份素材')).toBeTruthy()
    await user.click(screen.getByRole('button', { name: '预检素材' }))
    expect(onInspect).toHaveBeenCalledWith(expect.any(Array))
    expect(await screen.findByText('第二章完整正文')).toBeTruthy()

    await user.click(screen.getByRole('button', { name: '第10章.txt' }))
    expect(screen.getByText('第十章完整正文')).toBeTruthy()
    await user.click(screen.getByRole('button', { name: '确认并创建项目' }))
    expect(onImport).toHaveBeenCalledWith(expect.any(Array), '雨站', ['第2章.txt', '第10章.txt'])
  })

  it('collects directory files recursively by relative path and ignores unsupported files', async () => {
    const user = userEvent.setup()
    const onInspect = vi.fn().mockResolvedValue(inspection)
    const { container } = render(
      <ProjectLibrary importing={false} onImport={vi.fn()} onInspect={onInspect} onOpen={vi.fn()} onRefresh={vi.fn()} previewing={false} projects={[]} serverOnline state="ready" />,
    )
    const directoryInput = container.querySelector<HTMLInputElement>('input[data-import-source="directory"]')
    const firstChapter = new File(['第一卷'], '第1章.txt', { type: 'text/plain' })
    const secondChapter = new File(['第二卷'], '第1章.txt', { type: 'text/plain' })
    Object.defineProperty(firstChapter, 'webkitRelativePath', { value: '原稿/第一卷/第1章.txt' })
    Object.defineProperty(secondChapter, 'webkitRelativePath', { value: '原稿/第二卷/第1章.txt' })
    const image = new File(['image'], '封面.png', { type: 'image/png' })
    Object.defineProperty(image, 'webkitRelativePath', { value: '原稿/封面.png' })

    await user.upload(directoryInput!, [secondChapter, image, firstChapter])

    expect(screen.getByText('已选择 2 份素材')).toBeTruthy()
    expect(screen.getByText('原稿/第一卷/第1章.txt')).toBeTruthy()
    expect(screen.getByText('原稿/第二卷/第1章.txt')).toBeTruthy()
    expect(screen.getByRole('alert').textContent).toContain('已忽略 1 个不支持的文件')
    await user.click(screen.getByRole('button', { name: '预检素材' }))
    expect(onInspect).toHaveBeenCalledWith([firstChapter, secondChapter])
  })
})
