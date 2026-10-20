export function renderHeader(active: 'home' | 'about'): string {
  const cur = (name: string): string => (name === active ? ' aria-current="page"' : '');
  return `<header class="site-header">
  <a class="brand" href="#/">Sample App</a>
  <nav>
    <a href="#/"${cur('home')}>Home</a>
    <a href="#/about"${cur('about')}>About</a>
  </nav>
</header>`;
}
