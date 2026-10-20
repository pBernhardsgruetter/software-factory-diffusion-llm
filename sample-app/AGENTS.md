# Sample App

## Overview

A tiny Vite + vanilla TypeScript site with two pages (Home and About), a shared header and a footer. Routing is hash-based (`#/`, `#/about`). It exists as a small, realistic target for software-factory jobs. There is no backend.

## Important paths

- `index.html`: page shell, loads `src/main.ts`.
- `src/main.ts`: renders the current route into `#app` on load and on `hashchange`.
- `src/pages/`: `home.ts`, `about.ts`. Each exports a `render*()` function returning an HTML string.
- `src/components/`: `header.ts`, `footer.ts`. Shared page chrome, same string-render style.
- `src/lib/`: pure helpers (`format.ts`, `router.ts`) with tests next to them.
- `src/style.css`: all styles.
- `eslint.config.js`, `vite.config.ts`, `tsconfig.json`: tooling config.

## Fresh-checkout setup

Run once in a new checkout or git worktree (this directory is the project root):

```
npm ci
```

## Dev server

Run on the port you were assigned. `--strictPort` makes it fail instead of silently picking another port:

```
npm run dev -- --port <port> --strictPort
```

It listens on `http://127.0.0.1:<port>/`. Pages: `/#/` (Home) and `/#/about` (About).

## Validation commands

```
npm test
npm run build
npx eslint <changed files>
```

Lint only the files you changed. Never run `eslint --fix` across the repo.

## Conventions

- TypeScript strict mode. No `any`.
- Small modules in `src/`. Pages live in `src/pages/`, shared UI in `src/components/`, pure logic in `src/lib/`.
- Put a test next to the code as `*.test.ts` (for example `src/lib/format.test.ts`). Tests run with Vitest in a node environment, so keep render functions pure (return strings) and test those.
- Add dependencies only when the task needs them, and commit the updated `package-lock.json`.
