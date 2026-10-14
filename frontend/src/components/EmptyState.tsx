import type { ReactNode } from 'react'
import { Icon, type IconName } from './Icon'

export function EmptyState({
  icon = 'file',
  title,
  detail,
  action,
  compact = false,
}: {
  icon?: IconName
  title: string
  detail: string
  action?: ReactNode
  compact?: boolean
}) {
  return (
    <div className={`empty-state ${compact ? 'empty-state--compact' : ''}`}>
      <span className="empty-state__icon"><Icon name={icon} size={compact ? 19 : 24} /></span>
      <div>
        <h3>{title}</h3>
        <p>{detail}</p>
      </div>
      {action ? <div className="empty-state__action">{action}</div> : null}
    </div>
  )
}
