import { CopyButton } from './Messages'

export function LoginLink({ url, label = 'Open sign-in page' }: { url: string; label?: string }) {
  return <div className="login-address">
    <a className="login-url" href={url} target="_blank" rel="noopener noreferrer">{url}</a>
    <div className="login-card-links"><a className="primary login-link" href={url} target="_blank" rel="noopener noreferrer">{label}</a><CopyButton text={url} label="Copy link" className="copy-chip" wide /></div>
  </div>
}
