// PROTOTYPE — screenshot server: same app, proxied to an isolated test backend on 7795.
import base from './vite.config'
export default { ...base, server: { port: 5191, strictPort: true, proxy: { '/mobile': { target: 'http://127.0.0.1:7795', changeOrigin: false } } } }
