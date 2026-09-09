import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { applyTheme, loadTheme } from './lib/theme'

// Before the first paint, not in an effect. An effect runs after React has
// already rendered once, and that render would be light — a white flash on
// every reload for anyone using the dark theme.
applyTheme(loadTheme())

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
