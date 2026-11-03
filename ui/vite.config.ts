import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { copyFileSync, mkdirSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'

// Copy from the installed versions so dev and production use the same local assets.
const vadAssets = {
  name: 'vad-assets',
  configResolved(config: { publicDir: string }) {
    const require = createRequire(import.meta.url)
    const vad = require.resolve('@ricky0123/vad-web')
    const runtime = createRequire(vad)
    const target = resolve(config.publicDir, 'vad')
    mkdirSync(target, { recursive: true })
    for (const name of ['silero_vad_v5.onnx', 'vad.worklet.bundle.min.js']) {
      copyFileSync(resolve(dirname(vad), name), resolve(target, name))
    }
    for (const name of ['ort-wasm-simd-threaded.wasm', 'ort-wasm-simd-threaded.mjs']) {
      copyFileSync(runtime.resolve(`onnxruntime-web/${name}`), resolve(target, name))
    }
  },
}

export default defineConfig({
  base: '/chat/',
  plugins: [react(), tailwindcss(), vadAssets],
  server: {
    proxy: { '/mobile': { target: 'http://127.0.0.1:7683', changeOrigin: false } },
  },
})
