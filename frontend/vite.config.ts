import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 构建产物直接落到 Python 服务的静态目录，`python -m lodestar_agent --web` 就能打开。
// 开发时用 `npm run dev`：Vite 起 5174，把 API 和 SSE 代理到 8788 的 Python 进程。
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../lodestar_agent/static',
    emptyOutDir: true,
  },
  server: {
    port: 5174,
    proxy: {
      '/state': 'http://127.0.0.1:8788',
      '/config': 'http://127.0.0.1:8788',
      '/command': 'http://127.0.0.1:8788',
      '/history': 'http://127.0.0.1:8788',
      '/events': { target: 'http://127.0.0.1:8788', changeOrigin: true },
    },
  },
});
