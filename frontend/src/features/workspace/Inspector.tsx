import { useMemo, useState } from 'react'
import type { ProjectDetail, ReviewIssue, RunDetail, StoryFact } from '../../types'
import { Button } from '../../components/Button'
import { EmptyState } from '../../components/EmptyState'
import { Icon } from '../../components/Icon'

function IssueItem({ issue, onLocate, onCompare }: { issue: ReviewIssue; onLocate?: (issue: ReviewIssue) => void; onCompare?: (issue: ReviewIssue) => void }) {
  const locatable = Boolean(issue.chapterId && issue.startOffset !== undefined && issue.endOffset !== undefined)
  return (
    <li className={`issue issue--${issue.severity}`}>
      <header>
        <span>{issue.severity.toUpperCase()}</span>
        <strong>{issue.dimension}</strong>
        {issue.status === 'resolved' ? <em>已解决</em> : null}
        {issue.stale ? <em>已过期</em> : null}
      </header>
      <p>{issue.message}</p>
      {locatable && onLocate ? (
        <div className="issue__actions"><Button icon="search" onClick={() => onLocate(issue)} size="sm" variant="secondary">定位正文</Button>{onCompare ? <Button icon="layers" onClick={() => onCompare(issue)} size="sm" variant="ghost">查看差异</Button> : null}</div>
      ) : null}
      {issue.location ? <small>位置 · {issue.location}</small> : null}
      {issue.references.length ? (
        <div className="issue__references" aria-label="审稿依据">
          {issue.references.map((reference) => (
            <span key={`${reference.kind}-${reference.id}`} title={reference.detail}>
              {reference.kind === 'canon' ? 'Canon' : '悬念'} · {reference.title}
            </span>
          ))}
        </div>
      ) : null}
      {issue.evidence ? <blockquote>{issue.evidence}</blockquote> : null}
      {issue.instruction ? <div className="issue__instruction"><strong>修改指令</strong>{issue.instruction}</div> : null}
    </li>
  )
}

function FactList({ facts, issues, kind, onLocate }: { facts: StoryFact[]; issues: ReviewIssue[]; kind: string; onLocate?: (issue: ReviewIssue) => void }) {
  return <ul className={`fact-list${kind === 'suspense' ? ' fact-list--suspense' : ''}`}>{facts.map((fact) => {
    const linked = issues.filter((issue) => issue.references.some((reference) => reference.kind === kind && reference.id === fact.id))
    return <li key={fact.id}><strong>{fact.title}</strong>{fact.detail ? <p>{fact.detail}</p> : null}<small>{fact.source ?? fact.status}</small>{linked.length ? <details className="fact-links"><summary>关联问题 {linked.length}</summary>{linked.map((issue) => <button key={issue.id} onClick={() => onLocate?.(issue)} type="button">{issue.dimension} · {issue.message}</button>)}</details> : null}</li>
  })}</ul>
}

export function Inspector({
  project,
  run,
  onClose,
  onLocateIssue,
  onCompareIssue,
  drawer = false,
}: {
  project: ProjectDetail
  run?: RunDetail
  onClose?: () => void
  onLocateIssue?: (issue: ReviewIssue) => void
  onCompareIssue?: (issue: ReviewIssue) => void
  drawer?: boolean
}) {
  const storyState = project.storyState
  const canon = storyState?.canon ?? []
  const suspense = storyState?.suspense ?? []
  const issues = run?.reviewIssues ?? []
  const [severity, setSeverity] = useState('all')
  const [dimension, setDimension] = useState('all')
  const [chapter, setChapter] = useState('all')
  const [issueStatus, setIssueStatus] = useState('open')
  const dimensions = [...new Set(issues.map((issue) => issue.dimension))].sort()
  const filteredIssues = useMemo(() => issues.filter((issue) =>
    (severity === 'all' || issue.severity === severity)
    && (dimension === 'all' || issue.dimension === dimension)
    && (chapter === 'all' || issue.chapterId === chapter)
    && (issueStatus === 'all' || (issue.status ?? 'open') === issueStatus),
  ), [issues, severity, dimension, chapter, issueStatus])
  const grouped = Object.entries(filteredIssues.reduce<Record<string, ReviewIssue[]>>((result, issue) => {
    const key = issue.chapterId || 'global'
    result[key] = [...(result[key] ?? []), issue]
    return result
  }, {}))
  const openCount = issues.filter((issue) => issue.status !== 'resolved').length

  return (
    <aside className={`inspector ${drawer ? 'inspector--drawer' : ''}`} aria-label="Canon 与审稿栏">
      <header className="inspector__header">
        <div><span>EDITOR'S DESK</span><h2>审稿台</h2></div>
        {onClose ? <button aria-label="关闭审稿台" onClick={onClose} type="button"><Icon name="close" size={18} /></button> : null}
      </header>
      <details className="inspector-section" open>
        <summary><span>Canon 事实</span><em>{canon.length}</em><Icon name="chevron-down" size={15} /></summary>
        {canon.length ? (
          <FactList facts={canon} issues={issues} kind="canon" onLocate={onLocateIssue} />
        ) : <EmptyState compact detail="完成拆分总结后显示不可变事实。" title="尚无 Canon 索引" />}
      </details>
      <details className="inspector-section" open>
        <summary><span>悬念台账</span><em>{suspense.length}</em><Icon name="chevron-down" size={15} /></summary>
        {suspense.length ? (
          <FactList facts={suspense} issues={issues} kind="suspense" onLocate={onLocateIssue} />
        ) : <EmptyState compact detail="总结器写入悬念与伏笔后显示。" title="暂无悬念记录" />}
      </details>
      <details className="inspector-section inspector-section--issues" open>
        <summary><span>审稿问题 · 待处理 {openCount}</span><em>{filteredIssues.length}/{issues.length}</em><Icon name="chevron-down" size={15} /></summary>
        {issues.length ? (
          <><div className="issue-filters">
            <select aria-label="严重级别" onChange={(event) => setSeverity(event.target.value)} value={severity}><option value="all">全部级别</option><option value="blocker">Blocker</option><option value="major">Major</option><option value="minor">Minor</option><option value="note">Note</option></select>
            <select aria-label="审稿维度" onChange={(event) => setDimension(event.target.value)} value={dimension}><option value="all">全部维度</option>{dimensions.map((item) => <option key={item}>{item}</option>)}</select>
            <select aria-label="章节" onChange={(event) => setChapter(event.target.value)} value={chapter}><option value="all">全部章节</option>{run?.chapters.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}</select>
            <select aria-label="处理状态" onChange={(event) => setIssueStatus(event.target.value)} value={issueStatus}><option value="open">待处理</option><option value="resolved">已解决</option><option value="all">全部状态</option></select>
          </div>{grouped.length ? grouped.map(([chapterId, items]) => <section className="issue-group" key={chapterId}><h3>{run?.chapters.find((item) => item.id === chapterId)?.title ?? '全局问题'} <span>{items?.length ?? 0}</span></h3><ul className="issue-list">{items?.map((issue) => <IssueItem issue={issue} key={issue.id} onCompare={onCompareIssue} onLocate={onLocateIssue} />)}</ul></section>) : <EmptyState compact detail="调整筛选条件后重试。" title="没有匹配问题" />}</>
        )
          : <EmptyState compact detail={run ? '当前运行尚未产出审稿问题。' : '选择一个运行后查看九维审稿结果。'} title="暂无问题记录" />}
      </details>
      <section className="inspector-routing">
        <span>本次模型路由</span>
        <strong>{run?.routingPreset ?? '未启动续写运行'}</strong>
        <small>密钥状态只在服务端检查，不会进入浏览器。</small>
      </section>
    </aside>
  )
}
