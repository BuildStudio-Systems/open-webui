# Reproducible THERE frontend releases

THERE releases use Node 22 and **pnpm 11.19.0 with the checked-in
`pnpm-lock.yaml`**. Do not reuse an arbitrary existing `node_modules` directory
or use npm's different dependency graph as evidence for this release graph.
The npm lock remains valid for upstream npm workflows, but it is not the THERE
release dependency baseline.

On a clean source checkout, using a verified Node 22 runtime:

```sh
pnpm install --frozen-lockfile --ignore-scripts
pnpm exec vitest run --config vitest.there.config.mjs --pool=forks --poolOptions.forks.singleFork
pnpm run build
```

The normal build prepares Pyodide assets. An offline release builder may reuse
a separately hash-verified Pyodide cache and call Vite directly; it must record
that cache, exact Git commit, runtime, package manager, dependency lock digest,
and every generated file. It must also verify tracked source bytes before and
after building. A successful build is not a deployment receipt.

The project enforces Node's declared engine range and uses a consistent
60-character virtual-store directory limit on Windows and Linux. Do not disable
engine checks, update package versions while deploying, omit missing imports
from the bundle, or relax immutable-resource checks to force a release.

## 2026-09-27 dependency repair

The previous npm lock differed from the existing pnpm installation (for example,
Svelte 5.56.0 versus 5.57.0 and Tailwind PostCSS 4.2.1 versus 4.3.3). All 129
previously declared dependencies matched that installation's pnpm lock. The
initial release lock preserves its complete package and snapshot graph.

Six CodeMirror packages and `@tiptap/extension-italic` were imported directly by
application code but undeclared. Vite also copied `onnxruntime-web` JavaScript
and WASM assets without declaring that package directly. Clean pnpm builds
exposed both missing imports and the missing asset path. All eight are now
explicit dependencies at versions already present
in the captured pnpm graph; no additional package or snapshot is introduced to
that graph. The older loose npm-installed copies in the local cache are not a
reproducible release source.

The npm lock was regenerated in an isolated Node 22 environment without install
scripts. CodeMirror and TipTap peer-related lock changes are recorded separately
from the canonical pnpm graph. The editor dependency tests exercise real
CodeMirror state/extension composition, alongside the existing THERE tests.
The release dependency contract also checks all eight direct declarations and
the ONNX JavaScript/WASM files required by Vite's static-copy target.

Do not copy credentials, user configuration, production environment files, or
application databases into a build workspace. Shared-host builds require memory,
CPU, runtime and filesystem limits and may not restart production services.
