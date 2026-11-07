import { useId, useState } from 'react'
import { errorText, type Api, type AutoResumeData } from './api'

// Tabs that did not come back after a tmux or machine restart. The server keeps them until Retry or Dismiss.
export function AutoResumeNotice({ api, data, onChange }: { api: Api; data: AutoResumeData; onChange: () => void }) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState('')
  const detailsId = useId()
  const act = async (action: 'retry' | 'dismiss') => {
    setBusy(true)
    setFailure('')
    try {
      await api('auto-resume', { action })
      setOpen(false)
      onChange()
    } catch (error) {
      setFailure(errorText(error))
    } finally {
      setBusy(false)
    }
  }
  return <div className="resume-notice" role="status">
    <div className="resume-notice-row">
      <span className="resume-notice-count">{data.failed === 1 ? '1 tab did not reopen.' : `${data.failed} tabs did not reopen.`}</span>
      <button type="button" className="resume-notice-quiet" aria-expanded={open} aria-controls={detailsId} onClick={() => setOpen(value => !value)}>Details</button>
      <button type="button" className="resume-notice-quiet" disabled={busy} title="Forget these tabs" onClick={() => void act('dismiss')}>Dismiss</button>
      <button type="button" className="resume-notice-retry" disabled={busy} onClick={() => void act('retry')}>Retry</button>
    </div>
    {failure && <p className="resume-notice-failure" role="alert">{failure}</p>}
    <ul id={detailsId} hidden={!open}>{data.tabs.map(tab => <li key={tab.thread}><strong>{tab.name || tab.folder}</strong><span>{tab.error}</span></li>)}</ul>
  </div>
}
