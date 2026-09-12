// The entry point `index.html` loads. A placeholder until the app shell exists: it renders a
// heading and nothing else, so `vite build` and the dev server have a page to serve.
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <h1>Grounded Class Tutor</h1>
  </StrictMode>,
);
