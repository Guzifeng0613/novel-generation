import { useEffect, useRef, useState, type FormEvent } from 'react'
import type { Chapter, ReviewIssue } from '../../types'
import { charCount, formatNumber } from '../../utils'
import { Button } from '../../components/Button'
import { EmptyState } from '../../components/EmptyState'

function paragraphRanges(content: string) {
  const ranges: { text: string; start: number; end: number }[] = []
  const pattern = /\S[\s\S]*?(?=\n{2,}|$)/g
  for (const match of content.matchAll(pattern)) {
    const text = match[0].trim()
    if (!text) continue
    const start = (match.index ?? 0) + match[0].indexOf(text)
    ranges.push({ text, start, end: start + text.length })
  }
  return ranges
}

function HighlightedParagraph({ paragraph, issue }: { paragraph: ReturnType<typeof paragraphRanges>[number]; issue?: ReviewIssue }) {
  const markRef = useRef<HTMLElement>(null)
  const intersects = issue?.startOffset !== undefined && issue.endOffset !== undefined
    && issue.endOffset > paragraph.start && issue.startOffset < paragraph.end
  useEffect(() => {
    if (intersects && typeof markRef.current?.scrollIntoView === 'function') {
      markRef.current.scrollIntoView({ block: 'center', behavior: 'smooth' })
    }
  }, [intersects, issue?.id])
  if (!intersects || issue?.startOffset === undefined || issue.endOffset === undefined) return paragraph.text
  const start = Math.max(issue.startOffset, paragraph.start) - paragraph.start
  const end = Math.min(issue.endOffset, paragraph.end) - paragraph.start
  const label = issue.title || issue.message
  return <>{paragraph.text.slice(0, start)}<mark ref={markRef} aria-label={`审稿问题：${label}`} className="manuscript-highlight" title={label}>{paragraph.text.slice(start, end)}</mark>{paragraph.text.slice(end)}</>
}

export function ChapterView({
  chapter,
  highlightedIssue,
  busy,
  onSave,
}: {
  chapter?: Chapter
  highlightedIssue?: ReviewIssue
  busy?: boolean
  onSave: (input: { content: string; baseRevision: number; baseSha256: string; reason?: string }) => Promise<unknown>
}) {
  const [editing, setEditing] = useState(false)
  const [content, setContent] = useState('')
  const [reason, setReason] = useState('')
  useEffect(() => { setEditing(false); setContent(chapter?.content ?? ''); setReason('') }, [chapter?.id, chapter?.revision])
  if (!chapter) return <div className="paper-stage"><EmptyState detail="正文生成后，可从左侧章节树打开。" icon="book" title="尚未选择章节" /></div>
  if (!chapter.content) return <div className="paper-stage"><EmptyState detail={`《${chapter.title}》当前状态：${chapter.status ?? '等待生产'}`} icon="clock" title="正文尚未落盘" /></div>

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!chapter?.contentSha256 || !content.trim()) return
    await onSave({ content, baseRevision: chapter.revision ?? 0, baseSha256: chapter.contentSha256, reason })
    setEditing(false)
  }

  return <article className="manuscript">
    <header className="manuscript__header">
      <span>第 {String(chapter.index).padStart(2, '0')} 章</span>
      <h2>{chapter.title}</h2>
      <dl>
        {chapter.pov ? <div><dt>视角</dt><dd>{chapter.pov}</dd></div> : null}
        <div><dt>字数</dt><dd>{formatNumber(chapter.actualChars ?? charCount(chapter.content))}</dd></div>
        <div><dt>修订</dt><dd>{chapter.revision ?? 0}</dd></div>
        <div><dt>来源</dt><dd>{chapter.humanEdited ? '人工' : '模型'}</dd></div>
      </dl>
      <div className="manuscript__actions">
        {!editing ? <Button disabled={!chapter.editable} icon="file" onClick={() => { setContent(chapter.content ?? ''); setEditing(true) }} size="sm">编辑正文</Button> : null}
        {!chapter.editable ? <small>运行中不能编辑，请先暂停</small> : null}
      </div>
    </header>
    {editing ? <form className="chapter-editor" onSubmit={submit}>
      <textarea aria-label="章节正文" autoFocus onChange={(event) => setContent(event.target.value)} value={content} />
      <label><span>编辑说明（可选）</span><input maxLength={2000} onChange={(event) => setReason(event.target.value)} value={reason} /></label>
      <footer><span>{formatNumber(charCount(content))} 字</span><Button onClick={() => { setEditing(false); setContent(chapter.content ?? '') }} variant="ghost">取消</Button><Button busy={busy} disabled={!content.trim() || content === chapter.content} icon="check" type="submit" variant="primary">保存新版本</Button></footer>
    </form> : <div className="manuscript__body">
      {paragraphRanges(chapter.content).map((paragraph, index) => <p key={`${index}-${paragraph.text.slice(0, 16)}`}><HighlightedParagraph issue={highlightedIssue} paragraph={paragraph} /></p>)}
    </div>}
  </article>
}
