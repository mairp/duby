import { Routes, Route } from 'react-router-dom'
import Dashboard from './pages/Dashboard'
import PositionPage from './pages/Position'
import Overview from './pages/Overview'

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Dashboard />} />
      <Route path="/overview" element={<Overview />} />
      <Route path="/position/:ticker" element={<PositionPage />} />
    </Routes>
  )
}
