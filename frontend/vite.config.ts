import { defineConfig } from 'vite'

export default defineConfig({
  server: {
    port: 5173,  // no.1-semantic-doc-searchは5173ポートを使用
    proxy: {
      '/api': {
        target: 'http://localhost:8081',
        changeOrigin: true,
        rewrite: (path) => path,
      }
    }
  },
  build: {
    outDir: 'dist',
    assetsDir: 'assets',
  }
})
