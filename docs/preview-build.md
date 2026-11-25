# Offline preview build: Node and Python inputs

The preview controller separates networked acquisition from offline hook
execution. This stage delivers the showcase UI build, not a runnable preview:
`--target preview` keeps its non-root user (UID/GID 10001),
`ARTEMIS_PREVIEW_PROFILE=1` and Uvicorn command. It installs the Python app
and its locked dependencies from a hash-locked wheelhouse (see "Python
inputs"), but no system libraries yet. Do not activate this target until
the app-start proof lands. The live console remains the
default target and its Dockerfile stages are unchanged.

## 1. Acquire without running hooks

Copy only the two package metadata files from the approved checkout into
`apps/showcase_ui/` in a fresh acquisition directory. In the acquisition
namespace, call `acquire_node_plan(work_dir, proxy_url)` from
`scripts/preview_build.py` on that directory. The plan hashes `package.json`
and `package-lock.json` before execution. The lockfile must be regular JSON
with integer `lockfileVersion` 3 or newer. The controller runs the returned
command in the returned working directory:

```sh
npm ci --ignore-scripts --no-audit --no-fund
```

Use the returned environment as a replacement, not an overlay. It sets
`HTTPS_PROXY` and `npm_config_https_proxy` to the acquisition proxy, disables
user/global npm configuration with two distinct paths to the null device
(npm rejects loading the same path twice), and rejects a project `.npmrc`.
Use the trusted toolchain and a private npm cache/home; supply only controlled runtime values
such as `PATH` and `HOME`. Do not inherit registry, bypass or proxy settings.
The acquisition namespace must enforce proxy-only egress; the plan itself
does not configure or enforce a network policy.

## 2. Stage, verify, and build offline

Stage the checkout in `builder/work/source` and the acquired Node inputs
(both metadata files and `apps/showcase_ui/node_modules`) in
`builder/work/node-inputs`. Preserve relative symlinks and permissions. Call
`stage_manifest(node_inputs, plan, npm_version)` on the latter directory with
the version reported by the npm executable used for acquisition. It refuses changes to either package
metadata file since planning. Keep the resulting JSON record in controller
custody, separate from the staged files; it is not an authenticity signature.

The record contains both metadata SHA256s, the npm version and a SHA256 over
newline-delimited records sorted by relative POSIX path. Each record covers
the entry type, mode, size, and SHA256 of file bytes or of a relative symlink's
target text. Directories have size zero and no content hash. Internal npm
`.bin` symlinks are preserved; dangling/escaping links and special files are
refused. Acquire for the same OS, architecture and libc as the builder image.

Call `verify_staged(node_inputs, manifest)` on the controller-owned input tree
immediately before building. A missing dependency, modified input, or
unreadable entry stops the build; do not reacquire from the builder. Keep the
verified tree private with no concurrent writers through BuildKit ingestion.

Use the run's offline BuildKit socket and pre-imported base images. From
`builder/work`, bind the verified input directory as a local named context:

```sh
docker buildx build --network=none --target preview \
  --build-context preview-node-inputs=./node-inputs ./source
```

The named context contains no checkout `.dockerignore`. Preview copies the
verified package metadata and `node_modules` from that context. The checkout
still excludes `node_modules` from the default context and live wheel build.
Without the named context,
the empty `preview-node-inputs` stage has no dependency files and the copy
fails. The frontend stage runs `npm rebuild`, explicitly runs the
`patch-tango.mjs` postinstall via npm, then runs `npm run build`; every `RUN`
has `--network=none`. Before compilation, preview removes Google Fonts links
from its copy of `index.html` so Angular does not fetch the stylesheet during
optimization and the browser uses the existing font fallbacks. The live
console keeps its fonts. A hook that requires network access fails the build.
The preview image gets the resulting `/app/apps/showcase_ui/dist/` only from
the offline frontend stage. Base-image import and Python installation are
separate from this stage.

## Python inputs

Same split: a networked acquisition, then a build with no network in any
`RUN`. The `preview-python-inputs` named context holds `requirements.txt`,
`build-requirements.txt` and `wheelhouse/`.

1. **Acquire.** Copy only `pyproject.toml` and `uv.lock` into a fresh
   acquisition directory. Run on CPython 3.12, Linux x86_64 (pip evaluates
   requirement markers on the host; the wheels target the `python:3.12-slim`
   builder). Call `acquire_python_plan(work_dir, proxy_url)`. It hashes both
   files, refuses a registry package that has no wheel in the lock or none
   whose tags fit CPython 3.12 on glibc x86_64 (it names the package and
   version), and returns `commands` and a replacement `env`
   (proxy only, `PIP_CONFIG_FILE` set to the null device). Run the commands in
   `plan.cwd`, in order:
   `uv export --frozen --offline --no-config --no-dev --no-emit-project
   --no-emit-workspace --format requirements-txt` (runtime dependencies with
   hashes); a copy of `scripts/preview_build_requirements.txt` (hash-locked
   `setuptools` and `cython`, the build requirements of the two local
   projects); then `pip download --require-hashes --only-binary=:all: --no-deps`
   for both files, with `--python-version 3.12 --abi cp312` and one
   `--platform` per glibc tag up to 2.36 plus the legacy manylinux aliases
   (pip does not expand them). A package with no wheel for the builder fails
   here; there is no fallback to a source build. One exception: a package
   with no Linux wheel that only marker-conditioned dependencies reach (for
   example `pywin32` on `sys_platform == 'win32'`) is skipped by the plan;
   `pip download` skips it by marker, or fails if the marker holds.
2. **Stage and verify.** `stage_python_manifest(work_dir, plan, pip_version)`
   records the SHA256 of `pyproject.toml`, `uv.lock`, both requirement files,
   the wheelhouse tree (files and modes) and the pip version used. It refuses
   changed metadata and an empty wheelhouse. Keep the record in controller
   custody. `verify_python_staged(work_dir, manifest)` runs immediately
   before BuildKit; a changed, added or missing wheel or requirements file
   stops the build.
3. **Build offline.** Add `--build-context
   preview-python-inputs=<work_dir>/python-inputs` to the command above. The
   `preview-wheel-builder` stage installs the build requirements, then runs
   `pip wheel --no-index --no-build-isolation --no-deps` for
   `./packages/artemis-client` and `.`. The `preview` stage installs
   `requirements.txt` with `--no-index --require-hashes`, then the two local
   wheels with `--no-index --no-deps`. The live console stages are unchanged.

Known gap for the app-start proof: system libraries are absent (for example
`import cv2` needs `libxcb.so.1`), and the app writes `/app/traces` as UID
10001. Neither is handled here.

## Checks

```sh
python3 -m pytest --confcutdir=tests/unit/preview_host tests/unit/preview_host -q
```

The unit tests do not use a network or start Docker. For the X99 integration
proof, preserve the `patch-tango.mjs` build output and inspect `dist/` in the
exported image. Repeat in a disposable checkout with a postinstall hook that
requires an HTTPS request to the npm registry and assert the build fails.
