import { FolderOpen } from 'lucide-react'
import { useModal } from './hooks'

export type Media = { name: string; url: string; kind?: 'image' | 'video' | 'audio'; error?: string }

// Full-screen view of an image, video or audio file (an attachment, a screenshot the agent looked at, a path in a reply).
// A tap outside the player closes it; taps on video controls do not.
/** onShow: a "Show in Files" button (a chat on desktop). */
export function ImageViewer({ image, close, onShow }: { image: Media | null; close: () => void; onShow?: () => void }) {
  const modal = useModal(!!image, close)
  const kind = image?.kind ?? 'image'
  const keep = (event: React.MouseEvent) => event.stopPropagation()
  return <dialog ref={modal.ref} onCancel={modal.onCancel} className="image-viewer" aria-label={image?.name ?? 'Image'} onClick={close}>{image && <>
    {image.error ? <p className="viewer-error" role="alert">{image.error}</p>
      : kind === 'video' ? <video src={image.url} controls autoPlay playsInline onClick={keep} />
      : kind === 'audio' ? <audio src={image.url} controls autoPlay onClick={keep} />
      : <img src={image.url} alt={image.name} />}
    <p>{image.name}</p>
    {onShow && <button type="button" className="viewer-files" onClick={event => { event.stopPropagation(); onShow() }}><FolderOpen size={15} aria-hidden="true" />Show in Files</button>}
  </>}</dialog>
}
