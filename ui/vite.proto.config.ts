// PROTOTYPE — dev server for the Share prototype, reached through a local HTTPS proxy on port 8443.
import base from './vite.config'
export default { ...base, cacheDir: '.vite-proto', server: { ...base.server, port: 5190, strictPort: true, hmr: { clientPort: 8443, protocol: 'wss' } } }
