import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

import { apiProxy } from './dev-proxy.ts';

export default defineConfig({
  plugins: [react()],
  // `vite preview` inherits this proxy (Vite's `preview.proxy` defaults to `server.proxy`).
  server: { proxy: apiProxy(process.env) },
});
