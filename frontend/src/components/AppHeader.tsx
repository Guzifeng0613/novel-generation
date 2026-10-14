import type { ProviderStatus } from '../types'
import { Button } from './Button'

export type AppView = 'library' | 'workspace' | 'runs' | 'providers' | 'prompts'

const NAV_ITEMS: { id: AppView; label: string }[] = [
  { id: 'library', label: '项目书架' },
  { id: 'runs', label: '运行中心' },
  { id: 'providers', label: '模型路由' },
  { id: 'prompts', label: '提示词版本' },
]

export function AppHeader({
  view,
  providers,
  providersError,
  serverOnline,
  onNavigate,
}: {
  view: AppView
  providers: ProviderStatus
  providersError?: string
  serverOnline?: boolean
  onNavigate: (view: AppView) => void
}) {
  const configured = providers.providers.filter((provider) => provider.configured).length
  return (
    <header className="app-header">
      <button className="brand" onClick={() => onNavigate('library')} type="button">
        <span aria-hidden="true" className="brand__seal">续</span>
        <span className="brand__wordmark"><strong>续章</strong><small>NOVEL ROOM</small></span>
      </button>
      <nav aria-label="全局导航" className="global-nav">
        {NAV_ITEMS.map((item) => (
          <button
            aria-current={view === item.id ? 'page' : undefined}
            className={view === item.id ? 'is-active' : ''}
            key={item.id}
            onClick={() => onNavigate(item.id)}
            type="button"
          >
            {item.label}
          </button>
        ))}
      </nav>
      <div className="header-status">
        <span className={`connection-state connection-state--${serverOnline === true ? 'online' : serverOnline === false ? 'offline' : 'checking'}`}>
          <i aria-hidden="true" />
          {serverOnline === true ? '本机服务在线' : serverOnline === false ? '服务离线' : '正在连接'}
        </span>
        <span className="provider-count" title={providersError ?? undefined}>
          {providersError ? '模型状态未知' : `${configured}/${providers.providers.length || 2} 模型已配置`}
        </span>
        <Button aria-label="打开模型路由" className="header-settings" icon="settings" onClick={() => onNavigate('providers')} size="sm" variant="ghost">模型路由</Button>
      </div>
    </header>
  )
}
