import type { Chapter } from '../../types'
import { EmptyState } from '../../components/EmptyState'

function paragraphs(value: string) { return value.split(/\n{2,}/).map((item) => item.trim()).filter(Boolean) }

export function DiffView({ chapter }: { chapter?: Chapter }) {
  if (!chapter?.previousContent || !chapter.content) return <div className="paper-stage"><EmptyState detail="只有发生过定向修订且保留前一版本的章节才能比较。" icon="layers" title="暂无可比较版本" /></div>
  const before = paragraphs(chapter.previousContent)
  const after = paragraphs(chapter.content)
  const rows = Array.from({ length: Math.max(before.length, after.length) }, (_, index) => ({ before: before[index], after: after[index] }))
  return <div className="diff-view">
    <header><div><span className="diff-key diff-key--removed" />修订前 · v{Math.max(0, (chapter.revision ?? 1) - 1)}</div><div><span className="diff-key diff-key--added" />当前 · v{chapter.revision ?? 0} · {chapter.humanEdited ? '人工版本' : '自动修订'}</div></header>
    <div className="diff-rows">
      {rows.map((row, index) => {
        const changed = row.before !== row.after
        return <section className={changed ? 'is-changed' : ''} key={index}>
          <article aria-label={`修订前第 ${index + 1} 段`}>{row.before ? <p>{row.before}</p> : <em>此段为新增</em>}</article>
          <article aria-label={`当前第 ${index + 1} 段`}>{row.after ? <p>{row.after}</p> : <em>此段已删除</em>}</article>
        </section>
      })}
    </div>
  </div>
}
