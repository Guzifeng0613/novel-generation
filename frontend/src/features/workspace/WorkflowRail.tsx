import type { RunDetail } from '../../types'
import { WORKFLOW_STAGES } from '../../types'
import { workflowStageIndex } from '../../utils'
import { Icon } from '../../components/Icon'

export function WorkflowRail({ run }: { run?: RunDetail }) {
  const activeIndex = workflowStageIndex(run?.stage, run?.status)
  return (
    <nav aria-label="小说生产流程" className="workflow-rail">
      {WORKFLOW_STAGES.map((stage, index) => {
        const state = index < activeIndex ? 'complete' : index === activeIndex ? 'active' : 'future'
        return (
          <div aria-current={state === 'active' ? 'step' : undefined} className={`workflow-step workflow-step--${state}`} key={stage.id}>
            <span className="workflow-step__number">
              {state === 'complete' ? <Icon name="check" size={13} /> : String(index + 1).padStart(2, '0')}
            </span>
            <span className="workflow-step__label" data-short={stage.short}>{stage.label}</span>
          </div>
        )
      })}
    </nav>
  )
}
