import type { Look } from './layouts'
import { endTrial, useTrial, useTrialClock } from './lookTrial'

// The bar under a layout trial (lookTrial.ts). A trial goes back on its own unless the user keeps it; an undo offer
// (after Restore default) goes away on its own. App renders it only while Settings is closed: Settings is modal.
export function TrialBar({ onRevert }: { onRevert: (previous: Look | null) => void }) {
  const trial = useTrial()
  const left = useTrialClock(trial, onRevert)
  if (!trial) return null
  const back = () => { endTrial(); onRevert(trial.previous) }
  return <div className="look-trial" role="status" aria-live="polite">
    {trial.kind === 'trial'
      ? <><span>Trying “{trial.name}”. It goes back in {left} s.</span><button type="button" className="secondary" onClick={back}>Go back</button><button type="button" className="primary" onClick={endTrial}>Keep</button></>
      : <><span>Back to {trial.name}.</span><button type="button" className="secondary" onClick={back}>Undo</button><button type="button" className="ghost" onClick={endTrial}>Close</button></>}
  </div>
}
