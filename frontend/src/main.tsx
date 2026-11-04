import { createRoot } from 'react-dom/client';
import App from './App';
import './styles.css';

const root = document.getElementById('root');
if (root) {
  // 不用 StrictMode：它在 dev 会把 effect 跑两遍，SSE 会重复订阅一遍
  createRoot(root).render(<App />);
}
