import { formatDate } from '../lib/format';

export function renderHome(today: Date = new Date()): string {
  return `<h1>Welcome</h1>
<p>This is a tiny sample app used to try out the software factory.</p>
<p>Today is <time datetime="${formatDate(today)}">${formatDate(today)}</time>.</p>`;
}
