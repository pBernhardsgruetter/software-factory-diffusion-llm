export type Route = 'home' | 'about';

/** Map a location hash such as "#/about" to a route; unknown hashes go home. */
export function parseRoute(hash: string): Route {
  return hash === '#/about' ? 'about' : 'home';
}
