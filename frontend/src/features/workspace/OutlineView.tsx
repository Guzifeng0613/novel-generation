import { useEffect, useMemo, useState, type FormEvent } from 'react'
import type { OutlineChapter, RunDetail } from '../../types'
import { formatNumber } from '../../utils'
import { Button } from '../../components/Button'
import { Dialog } from '../../components/Dialog'
import { EmptyState } from '../../components/EmptyState'
import { Icon } from '../../components/Icon'

export function OutlineView({
  run,
  action,
  onApprove,
  onRevise,
  onCreateContinuation,
  activeBranch,
}: {
  run?: RunDetail
  action?: string
  activeBranch: string
  onApprove: (allowCanonOverrides?: boolean) => Promise<unknown>
  onRevise: (feedback: string, chapterId?: string) => Promise<unknown>
  onCreateContinuation: () => void
}) {
  const [revisionTarget, setRevisionTarget] = useState<OutlineChapter | 'all'>()
  const [feedback, setFeedback] = useState('')
  const [overrideConfirmed, setOverrideConfirmed] = useState(false)
  const plan = run?.plan
  const conflicts = useMemo(() => [
    ...(plan?.canonConflicts ?? []),
    ...(plan?.chapters.flatMap((chapter) => chapter.canonConflicts.map((item) => `${chapter.title}：${item}`)) ?? []),
  ], [plan])
  const awaitingApproval = run?.status === 'awaiting_outline_approval' || plan?.status === 'awaiting_approval' || plan?.status === 'draft'
  const hasCanonConflicts = conflicts.length > 0
  const isOverrideBranch = Boolean(run?.branchName && run.branchName !== activeBranch)

  useEffect(() => {
    setOverrideConfirmed(false)
  }, [run?.id, plan?.planHash])

  const submitRevision = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!feedback.trim()) return
    try {
      await onRevise(feedback.trim(), revisionTarget === 'all' ? undefined : revisionTarget?.id)
      setRevisionTarget(undefined)
      setFeedback('')
    } catch {
      // Shared notice handles the API error.
    }
  }

  if (!plan) {
    return (
      <div className="paper-stage">
        <EmptyState
          action={<Button icon="spark" onClick={onCreateContinuation} variant="primary">新建续写计划</Button>}
          detail={run?.kind === 'analysis' ? '当前是拆分总结运行。完成后即可新建续写计划。' : '先填写目标字数、续写要求与模型路由，再由主 A 生成章纲。'}
          icon="layers"
          title="还没有可审批的章纲"
        />
      </div>
    )
  }

  return (
    <>
      <article className="outline-sheet">
        <header className="outline-sheet__header">
          <div>
            <span className="document-type">RUN PLAN / {plan.version ? `VERSION ${plan.version}` : 'DRAFT'}</span>
            <h2>续写章纲</h2>
            <p>{plan.premise ?? '主编未附加起点说明。'}</p>
          </div>
          <dl className="outline-summary">
            <div><dt>章节</dt><dd>{plan.chapters.length}</dd></div>
            <div><dt>目标</dt><dd>{formatNumber(run.targetChars)} 字</dd></div>
            <div><dt>路由</dt><dd>{run.routingPreset ?? '未记录'}</dd></div>
          </dl>
        </header>

        {conflicts.length > 0 ? (
          <section className="canon-conflicts" aria-labelledby="conflict-title">
            <h3 id="conflict-title"><Icon name="warning" size={17} /> Canon 冲突需要确认</h3>
            <ul>{conflicts.map((conflict, index) => <li key={`${conflict}-${index}`}>{conflict}</li>)}</ul>
          </section>
        ) : null}

        {hasCanonConflicts && awaitingApproval ? (
          <section className="branch-override" aria-labelledby="branch-override-title">
            <div className="branch-override__context">
              <Icon name="route" size={18} />
              <div>
                <h3 id="branch-override-title">Canon 覆盖只能写入独立分支</h3>
                <p>当前运行分支：<strong>{run?.branchName ?? '未上报'}</strong> · 正式分支：<strong>{activeBranch}</strong></p>
              </div>
            </div>
            <label className={isOverrideBranch ? '' : 'is-disabled'}>
              <input
                checked={overrideConfirmed}
                disabled={!isOverrideBranch}
                onChange={(event) => setOverrideConfirmed(event.target.checked)}
                type="checkbox"
              />
              <span className="branch-override__check"><Icon name={overrideConfirmed ? 'check' : 'warning'} size={15} /></span>
              <span>
                <strong>确认在独立分支中创建 Canon 覆盖</strong>
                <small>该确认会随批准请求发送，审计记录中保留覆盖事实与章纲哈希。</small>
              </span>
            </label>
            {!isOverrideBranch ? (
              <p className="branch-override__blocked">当前运行不在独立分支，不能批准冲突章纲。请新建续写运行并填写不同于“{activeBranch}”的分支名。</p>
            ) : null}
          </section>
        ) : null}

        {plan.styleCard ? (
          <section className="style-card">
            <h3>本轮风格卡</h3>
            <p>{plan.styleCard}</p>
          </section>
        ) : null}

        <ol className="outline-chapters">
          {plan.chapters.map((chapter) => (
            <li key={chapter.id}>
              <div className="outline-chapter__number">{String(chapter.index).padStart(2, '0')}</div>
              <div className="outline-chapter__body">
                <header>
                  <div><h3>{chapter.title}</h3>{chapter.pov ? <span>POV · {chapter.pov}</span> : null}</div>
                  <span>{chapter.targetChars ? `${formatNumber(chapter.targetChars)} 字` : '字数待规划'}</span>
                </header>
                <p>{chapter.summary || '本章 brief 暂无正文。'}</p>
                {chapter.expectedEndState ? <div className="end-state"><strong>预期终态</strong>{chapter.expectedEndState}</div> : null}
                {awaitingApproval ? (
                  <Button icon="refresh" onClick={() => setRevisionTarget(chapter)} size="sm" variant="ghost">退回本章</Button>
                ) : null}
              </div>
            </li>
          ))}
        </ol>
      </article>

      <footer className="approval-bar">
        <div>
          <strong>{awaitingApproval ? '章纲等待总编决定' : `计划状态：${plan.status ?? run.status}`}</strong>
          <span>{plan.approvedHash
            ? `冻结哈希 ${plan.approvedHash.slice(0, 12)}…`
            : plan.planHash
              ? `待批哈希 ${plan.planHash.slice(0, 12)}…`
              : '批准后将冻结计划、路由与预算。'}</span>
        </div>
        {awaitingApproval ? (
          <div className="approval-bar__actions">
            <Button busy={action === 'revise'} icon="refresh" onClick={() => setRevisionTarget('all')}>整纲退回</Button>
            <Button
              busy={action === 'approve'}
              disabled={hasCanonConflicts && (!isOverrideBranch || !overrideConfirmed)}
              icon="check"
              onClick={() => void onApprove(hasCanonConflicts && overrideConfirmed)}
              variant="primary"
            >批准并开始写作</Button>
          </div>
        ) : null}
      </footer>

      <Dialog
        description={revisionTarget === 'all' ? '说明整版章纲需要调整的方向。' : `说明《${revisionTarget?.title ?? ''}》需要调整的内容。`}
        onClose={() => setRevisionTarget(undefined)}
        open={revisionTarget !== undefined}
        title={revisionTarget === 'all' ? '退回整版章纲' : '退回单章'}
      >
        <form className="revision-form" onSubmit={submitRevision}>
          <label className="field">
            <span>修改意见</span>
            <textarea autoFocus maxLength={3000} onChange={(event) => setFeedback(event.target.value)} placeholder="明确指出剧情方向、角色行为或节奏需要如何调整……" rows={7} value={feedback} />
          </label>
          <div className="dialog__actions">
            <Button onClick={() => setRevisionTarget(undefined)} variant="ghost">取消</Button>
            <Button busy={action === 'revise'} disabled={!feedback.trim()} icon="refresh" type="submit" variant="primary">提交退回意见</Button>
          </div>
        </form>
      </Dialog>
    </>
  )
}
