# Offline preview build: Node and Python inputs

The preview controller separates networked acquisition from offline hook
execution. `--target preview` keeps its non-root user (UID/GID 10001),
`ARTEMIS_PREVIEW_PROFILE=1` and Uvicorn command. It builds the showcase UI,
and installs the Python app and its locked dependencies from a hash-locked
wheelhouse (see "Python inputs"), with no network in any `RUN`. It adds no
system libraries: OpenCV comes from `opencv-python-headless`, which needs
none. The app-start proof passed on X99 (see "App-start proof"), so the
target is usable. The live console remains the default target and its
Dockerfile stages are unchanged.

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
does not configure or enforce a network policy. The ruleset, proxy unit and
bypass tests are in [`preview-egress.md`](preview-egress.md).

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
   with no Linux wheel that only marker-conditioned dependencies reach is
   skipped, but only if every marker is false for the builder (CPython 3.12,
   Linux, x86_64; `BUILDER_ENV` in `scripts/preview_build.py`), for example
   `pywin32` on `sys_platform == 'win32'`. A marker that is true there, such as
   `python_full_version < '3.13'`, keeps the package required and the plan
   fails. `packaging` must be importable where the plan runs.
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

## App-start proof

The preview image must start with no network, as UID 10001. The host daemon
supplies what the image does not: a writable `ARTEMIS_APP_DIR` parent (`/app`
is not writable by the preview user), a writable `/tmp`, the Cloudflare Access
settings and a public JWKS bundle (`ARTEMIS_PREVIEW_JWKS_BUNDLE`), and the
fixture emails. Run on X99 with a throwaway key; the token is signed by the
same key:

```sh
docker run -d --name proof --network none \
  --tmpfs /tmp:uid=10001,gid=10001 --tmpfs /scratch:uid=10001,gid=10001,mode=700 \
  -v "$PWD/proof:/proof:ro" -e ARTEMIS_APP_DIR=/scratch \
  -e ARTEMIS_AUTH_MODE=cloudflare -e ARTEMIS_CF_ACCESS_AUD=proof-aud \
  -e ARTEMIS_CF_ACCESS_TEAM_DOMAIN=proof -e ARTEMIS_PREVIEW_JWKS_BUNDLE=/proof/jwks.json \
  -e ARTEMIS_ADMIN_EMAILS=admin@example.test \
  -e ARTEMIS_PREVIEW_QA_EMAILS=qa1@example.test,qa2@example.test <image>
```

Result (image `sha256:ee11b581262fda4bcdf11bd8922deab0af6de8537aabbedd49768cbd86bb3641`,
`Config.User` `10001:10001`): the server logs "Application startup complete".
From inside the container, `GET /` returns 401 without the
`Cf-Access-Jwt-Assertion` header and 200 with a valid token;
`GET /api/system/readiness` returns 200 with the token.

Negative proof: append `notinwheelhouse==1.0 --hash=sha256:00...00` to a
copy of `requirements.txt` in the `preview-python-inputs` context. The build
fails at `pip install --no-index` with
`ERROR: No matching distribution found for notinwheelhouse==1.0` (exit 1).

## Checks

```sh
python3 -m pytest --confcutdir=tests/unit/preview_host tests/unit/preview_host -q
```

The unit tests do not use a network or start Docker. For the X99 integration
proof, preserve the `patch-tango.mjs` build output and inspect `dist/` in the
exported image. Repeat in a disposable checkout with a postinstall hook that
requires an HTTPS request to the npm registry and assert the build fails.
