import { BrowserRouter, Routes, Route } from 'react-router-dom'
import { QueryClientProvider } from '@tanstack/react-query'
import { queryClient } from './lib/queryClient'
import { Header } from './components/Header'
import { CommandPalette } from './components/CommandPalette'
import { useThemeSync } from './hooks/useThemeSync'
import { useCommandPaletteShortcut } from './hooks/useCommandPaletteShortcut'
import { useFocusModality } from './hooks/useFocusModality'
import { Dashboard } from './pages/Dashboard'
import { Activity } from './pages/Activity'
import { News } from './pages/News'
import { Markets } from './pages/Markets'
import { Research } from './pages/Research'
import { Account } from './pages/Account'
import { Settings } from './pages/Settings'
import { Design } from './pages/Design'
import { NotFound } from './pages/NotFound'

function AppShell() {
  useThemeSync()
  useCommandPaletteShortcut()
  useFocusModality()

  return (
    <BrowserRouter>
      <div className="min-h-screen bg-surface">
        <Header />
        <main>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/activity" element={<Activity />} />
            <Route path="/news" element={<News />} />
            <Route path="/markets" element={<Markets />} />
            <Route path="/research" element={<Research />} />
            <Route path="/account" element={<Account />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="/design" element={<Design />} />
            {/* Catch-all. Without it an unknown URL renders the header over
                an empty main, which reads as a crash rather than a wrong
                address. */}
            <Route path="*" element={<NotFound />} />
          </Routes>
        </main>
        <CommandPalette />
      </div>
    </BrowserRouter>
  )
}

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <AppShell />
    </QueryClientProvider>
  )
}

export default App
