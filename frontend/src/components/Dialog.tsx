import { useEffect, useId, useRef, type ReactNode } from 'react'
import { Button } from './Button'

export function Dialog({
  open,
  title,
  description,
  children,
  onClose,
  wide = false,
}: {
  open: boolean
  title: string
  description?: string
  children: ReactNode
  onClose: () => void
  wide?: boolean
}) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const descriptionId = useId()

  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return
    if (open && !dialog.open) dialog.showModal()
    if (!open && dialog.open) dialog.close()
  }, [open])

  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return
    const handleClose = () => onClose()
    dialog.addEventListener('close', handleClose)
    return () => dialog.removeEventListener('close', handleClose)
  }, [onClose])

  return (
    <dialog aria-describedby={description ? descriptionId : undefined} aria-label={title} className={`dialog ${wide ? 'dialog--wide' : ''}`} ref={dialogRef}>
      <header className="dialog__header">
        <div>
          <h2>{title}</h2>
          {description ? <p id={descriptionId}>{description}</p> : null}
        </div>
        <Button aria-label="关闭" icon="close" onClick={onClose} size="sm" variant="ghost">关闭</Button>
      </header>
      <div className="dialog__body">{children}</div>
    </dialog>
  )
}
