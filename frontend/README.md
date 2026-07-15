# Twitch Farm control-room frontend

React 19, TypeScript, Tailwind CSS v4, and source-owned shadcn/ui Base UI/Nova
components power the operator SPA. Django serves the production bundle and the
same-origin `/api/v1/` API.

Use Node 24 from the repository `.nvmrc`:

```sh
nvm use
npm ci
npm run typecheck
npm run lint
npm test
npm run build
```

The production build writes the stable `app.js` and `app.css` entry points into
`controller/static/controller/app/`; Django fingerprints them during
`collectstatic`.

Add or update official components with the pinned CLI:

```sh
npm exec shadcn -- add <component>
```

Keep general primitives under `src/components/ui/` and domain compositions
under `src/components/`.
