# Public read-only preview

This entry reuses native frontend components with fictional browser-local fixtures.
It is a separate build; production login, business APIs and deployed services are
unchanged. The preview is published by BuildStudio Systems under its static
`/showcase/` directory and opened from the public catalog's preview link.

Run the sibling Systems repository's `showcase/build.py` to build and stage all
four previews. This build imports Systems' shared `showcase/runtime.ts` guard.
No backend, credential, production database or sample account is required.
Only viewing/navigation/filtering is enabled. Writes, execution, uploads and
downloads are blocked, with no network fallback from fixture resolution.

See `buildstudio-systems/portal/showcase/README.md` and
`docs/public-previews.md` in that repository for the design and deployment
contract. The regular build configuration remains the production entry.
