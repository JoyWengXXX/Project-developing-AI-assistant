import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

// 參考清單已改存後端資料庫（GET /api/v1/references）。舊版存在瀏覽器 localStorage 的清單不遷移、不再使用，
// 這裡把殘留的鍵清掉（localStorage 被瀏覽器設定封鎖時，存取會拋出例外，忽略即可）。
try {
  window.localStorage.removeItem('repopilot-reference-repos')
} catch {
  // 忽略
}

// 刻意不使用 <StrictMode>：它在開發模式會讓 effect 執行兩次，
// 每個 GET 請求都會在後端日誌重複出現，干擾以 request_id 對照日誌的除錯方式。
createRoot(document.getElementById('root')!).render(<App />)
