import './style.css';
import { renderFooter } from './components/footer';
import { renderHeader } from './components/header';
import { parseRoute } from './lib/router';
import { renderAbout } from './pages/about';
import { renderHome } from './pages/home';

const root = document.querySelector<HTMLDivElement>('#app');
if (!root) throw new Error('#app element not found');
const app: HTMLDivElement = root;

function render(): void {
  const route = parseRoute(window.location.hash);
  const page = route === 'about' ? renderAbout() : renderHome();
  app.innerHTML = `${renderHeader(route)}<main>${page}</main>${renderFooter()}`;
}

window.addEventListener('hashchange', render);
render();
