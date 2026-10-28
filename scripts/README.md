# PR base-image and coverage pipeline

The scripts process pull requests from `migrations-prs.json`. They resolve each
PR's base and head from the GitHub pull-request API, build from the base commit,
and checkpoint the JSON after each PR so a long run can be inspected or resumed.
The image contains only the base state as one synthetic Git commit, dated with
the original base commit's committer timestamp. The temporary Dockerfile and
its ignore file stay outside the source context so project builds and license
checks do not mistake pipeline helpers for historical project files.

## Prerequisites

- `gh` authenticated to GitHub (`gh auth status`)
- Docker available to the current shell
- Full local repository clones under `~/local-repos/<owner>/<repo>`; the image
  script clones a missing repository
- `j2kbench-base:latest`, built from `Dockerfile.base`, unless another compatible
  base image is selected
- `pandas` and `openpyxl` for the coverage workbook

The image script runs `docker version` once before processing any PR. If the
Docker executable or daemon is unavailable, it stops immediately instead of
repeating the same failure for every PR.

If a pipeline child script exits unsuccessfully, the scheduler now reports
that stage and exits with the same status without printing a Python traceback.

The `Dockerfile.base` builds an image with OpenJDK 8, 11, 17, 21, Temurin
25, JetBrains Runtime 21, an Android SDK, and Maven. Gradle runs with the JDK
selected from its wrapper version and detected Java requirements; Temurin 25
and JetBrains 21 are registered as compiler toolchains for projects that
request them. The base image sets `NODE_ENV=development`; the pipeline also explicitly sets
it for Dimpact Gradle commands so npm installs include development dependencies
such as Angular CLI, even when using an older base-image tag. For Gradle builds,
the pipeline selects the runtime JDK from
the wrapper version: Java 8 for Gradle before 5.0, Java 11 for Gradle 5.0
through 7.2, Java 17 for Gradle 7.3 through 8.4, and Java 21 for Gradle 8.5
or newer. Detected Java 25 requirements select Java 25 regardless of wrapper
version. Builds without a detectable wrapper version use Java 21. The same
selection is applied to build, test, cleanup, and offline verification, and is
recorded as `java_runtime_version` and `java_runtime_home` in the PR result.
The selected JDK's `bin` directory is prepended to `PATH` as well as setting
`JAVA_HOME`, so image-provided Java shims cannot override the selection.
Java source/target compatibility is separate from the JDK that runs Gradle.
The pipeline also records detected Java source/target, Gradle
`java-version` properties, Android Gradle Plugin, and `compileSdk` versions
from each PR's base commit. If a project needs a
runtime/toolchain outside this selection, use a compatible base image with
`--base-image`.

For Java 25 projects, detected Java 25 source/toolchain requirements select
the Temurin 25 runtime. For projects whose Gradle build fails before reporting
an actionable cause, pass `--gradle-stacktrace` to include stack traces in
online and offline Gradle build/test output.

The historical S3Mock Maven build has a `skipDocker` property that disables
its Docker image and Docker-backed integration-test profiles. The pipeline
passes `-DskipDocker` for this repository while continuing its Maven build,
unit-test, and offline checks; the Docker-backed integration tests are outside
the pipeline's unit-test gate.

## Build and verify images

### Private Maven packages

For private GitHub Packages dependencies, use a **classic personal access token**
with `read:packages` and access to the package's linked repository. Authorize it
for the organization's SSO if required. The Maven
repository IDs in the project POM must match the `<server><id>` values in
`settings.xml`. For `navikt/melosys-api`, configure both IDs if both package
repositories are needed:

```xml
<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">
  <servers>
    <server>
      <id>github-package-registry-navikt</id>
      <username>YOUR_GITHUB_USERNAME</username>
      <password>YOUR_READ_PACKAGES_TOKEN</password>
    </server>
    <server>
      <id>freg-abac</id>
      <username>YOUR_GITHUB_USERNAME</username>
      <password>YOUR_READ_PACKAGES_TOKEN</password>
    </server>
  </servers>
</settings>
```

Keep the file outside the repository and restrict it with `chmod 600
~/.m2/settings.xml`. Set the environment values in the shell that launches the
pipeline. Enter the classic PAT at the hidden prompt; do not use `gh auth token`
here, because the GitHub CLI OAuth token returned 401 against Maven Packages:

```bash
export GITHUB_USERNAME="$(gh api user --jq .login)"
read -rsp 'GitHub classic PAT: ' GITHUB_PACKAGES_TOKEN; echo
export GITHUB_PACKAGES_TOKEN
```

If Maven reports `401 Unauthorized`, the repository was contacted but rejected
the credentials; that does not establish whether the artifact/version exists.
Check that the repository ID used by Maven matches a `<server><id>` in the
settings file, then replace the stored password with a classic PAT that has
`read:packages`, package/repository access, and any required organization SSO
authorization.

Pass the settings file with `--maven-settings`; the orchestrator copies it only
into temporary Maven build containers, forwards the two environment variables,
and removes the file before committing a PR image. Resolved artifacts remain
cached in the image, but the token does not. To retry this repository after
setting up access:

```bash
docker login ghcr.io
python -u scripts/pr_tagging_script.py migrations-prs.json \
  --repo navikt/melosys-api \
  --retry-failed-images \
  --maven-settings "$HOME/.m2/settings.xml" \
  --batch-size 3
```

### SkyHanni backup repository archive

SkyHanni's `includeBackupRepo` and `includeBackupNeuRepo` tasks download
archives from moving branches. To make offline verification reproducible, the
pipeline pins `SkyHanni-Repo` to commit
`8a224024a157523e71f9a314204438b3e5f62222` and `NotEnoughUpdates-Repo` to
commit `c4f7d4757641f3a11c74d3d77cec76c100abaf98`, then verifies each
SHA-256 before use. Keep both archives in the user cache, outside this
repository:

```bash
archive="$HOME/.cache/multi-repos-pipeline/skyhanni-repo-8a224024a157523e71f9a314204438b3e5f62222.zip"
mkdir -p "$(dirname "$archive")"
curl -fL \
  "https://codeload.github.com/hannibal002/SkyHanni-Repo/zip/8a224024a157523e71f9a314204438b3e5f62222" \
  -o "$archive"
echo "062de7f67f0d014665914ceab3e4300008f7f498287527015fb117cdf48d6ab8  $archive" \
  | sha256sum --check

neu_archive="$HOME/.cache/multi-repos-pipeline/skyhanni-neu-repo-c4f7d4757641f3a11c74d3d77cec76c100abaf98.tar.gz"
mkdir -p "$(dirname "$neu_archive")"
curl -fL \
  "https://codeload.github.com/NotEnoughUpdates/NotEnoughUpdates-Repo/tar.gz/c4f7d4757641f3a11c74d3d77cec76c100abaf98" \
  -o "$neu_archive"
echo "b729636e7fdb4df2960d8bf40d24f2a814b7dbfce421cdfed496850e1a2000f3  $neu_archive" \
  | sha256sum --check
```

Both pipeline scripts use that cache path by default; pass
`--skyhanni-repo-archive /path/to/archive.zip` or
`--skyhanni-neu-repo-archive /path/to/archive.tar.gz` to use another copy of
the corresponding pinned file. Both archives are copied into the temporary
build container. A Gradle init script supplies them to their corresponding
tasks and disables both network-fetch tasks for the build and the offline
build/test checks. The historical SkyHanni checkout is not patched.

To rebuild and re-verify SkyHanni images with the pinned archive:

```bash
python -u scripts/pr_tagging_script.py migrations-prs.json \
  --repo hannibal002/SkyHanni \
  --base-image j2kbench-base:toolchains-20261008 \
  --retry-failed-images \
  --batch-size 3
```

Use that explicit base tag for the current retry: the existing SkyHanni PR
#6410 failure needs JDK 25, and the default `j2kbench-base:latest` is a
different image that does not include it. The updated `Dockerfile.base` only
takes effect after building/tagging the shared image.

First validate a small sample and build the shared image if needed:

```bash
python scripts/pr_image_script.py migrations-prs.json \
  --build-base-image --limit 1 --repo Futsch1/medTimer
```

After reviewing that result, process every PR:

```bash
python scripts/pr_image_script.py migrations-prs.json
```

If a run is interrupted, resume from the saved JSON checkpoint. PRs with a
completed run and built image are skipped; PRs that failed before an image was
created are retried by default:

```bash
python scripts/pr_image_script.py migrations-prs.json --resume
```

To leave completed image-build failures aside temporarily and continue with
PRs that have not completed yet, add `--skip-failed-images`:

```bash
python scripts/pr_image_script.py migrations-prs.json \
  --resume --skip-failed-images
```

For each PR, the script:

1. Reads `.base.sha` and `.head.sha` from `gh api repos/{owner}/{repo}/pulls/{number}`.
2. Builds from a detached worktree at the base SHA, then removes `.git` and
  initializes a single-commit history in the image. The generated snapshot
  Dockerfile and its empty Dockerfile-specific ignore file live inside the
  build context, so repository ignore rules do not accidentally omit source
  files; repository files are left unchanged.
3. Runs a clean build and unit tests with build caching disabled, preserves
   downloaded dependencies while cleaning build outputs, and commits those
   dependencies into the per-PR image.
4. Re-runs the build and tests offline with `--network none`, checks the
   one-commit history and probes outbound connectivity.
5. Stores the local `docker_tag`, build results, image size, and execution tier
   in the input JSON. Only PRs passing all checks are marked
   `execution-verified`.

GitHub PR API requests time out after 45 seconds. A timed-out lookup is
recorded for that PR and processing continues. Pressing Ctrl+C stops the image
run cleanly, saves progress, and marks the active PR as interrupted.

Gradle and Maven roots are detected at each PR's base SHA. Repositories with
ambiguous or absent build roots are explicitly marked unsupported. A missing
JaCoCo task/report is also reported as unsupported rather than as zero
coverage.

Gradle clean and build commands run separately. This avoids removing generated
resources in repositories whose Gradle configuration creates them before the
`clean` task executes.
Android Gradle projects build the Debug variant (`assembleDebug`) and run
the conventional unit-test task, rather than packaging Release variants that may
require signing credentials unavailable to the pipeline. The selected
`build_target` is recorded for each PR.
Gradle test tasks are selected by convention (`test` for JVM projects and
`testDebugUnitTest` for Android projects) instead of enumerating every task,
which can realize unrelated custom tasks with unavailable external tools.
Projects without tracked Gradle unit-test source files are marked as having
unsupported tests rather than failing test discovery.

### Build, publish, and remove one repository at a time

To avoid accumulating PR images locally across the whole dataset, authenticate
to GHCR and run the repository-scoped orchestrator:

```bash
docker login ghcr.io
python scripts/pr_tagging_script.py migrations-prs.json
```

It resumes the existing checkpoint, skips earlier failed image builds, then
processes each repository in order: build its remaining PR images, push only
execution-verified images, and remove that repository's known local PR image
tags and build containers before moving on. A failed eligible-image push keeps
that PR's local image and stops the run so it can be retried. Select a subset
with one or more `--repo owner/name` options. Add `--build-base-image` if the
shared base image has not been built. Previously failed image builds are
skipped by default, and completed-but-unverified images are reused as
checkpoints. Pass `--retry-failed-images` to retry failed and completed but
unverified PRs for the selected repositories (for example, after fixing a
missing tool in the shared base image). To retry only image builds recorded as
failed while reusing every image that already built successfully (including
unverified ones), use `--retry-image-build-failures-only`.
To continue at a particular PR in one repository, use `--repo` with
`--start-pr` (inclusive); PRs before that point in the selected repository and
all other repositories are left untouched:

```bash
python scripts/pr_tagging_script.py migrations-prs.json \
  --repo thunderbird/thunderbird-android \
  --start-pr 9270 \
  --retry-failed-images
```

Use `--batch-size` to limit how many PR images are built before eligible images
are pushed and that batch's local PR images are removed. This limits temporary
image accumulation for large repositories. For example, process a repository
in batches of three PRs:

```bash
python scripts/pr_tagging_script.py migrations-prs.json \
  --repo nextcloud/android \
  --retry-failed-images \
  --batch-size 3
```

To retry recorded image-build failures across the dataset without rebuilding
already-passed but unverified images, use:

```bash
python scripts/pr_tagging_script.py migrations-prs.json \
  --retry-image-build-failures-only \
  --batch-size 3
```

This mode reuses all PRs with a previously passed image build (even if their
tests were unsupported or the run was interrupted after image creation) and
retries only recorded image-build failures.

To retry only checkpoint entries classified as network/dependency, Java
toolchain, Android SDK/AGP, or repository/Docker infrastructure failures, use:

```bash
docker login ghcr.io
python -u scripts/pr_tagging_script.py migrations-prs.json \
  --retry-infrastructure-failures-only \
  --batch-size 3 2>&1 | tee -a infrastructure-retries.log
```

This mode can retry a PR even when its image was built successfully but its
recorded build or verification error matches an infrastructure category.
Execution-verified PRs and errors that do not match those categories, such as
project compile failures, are skipped. SDK metadata warnings alone are not
treated as Android failures. Review each retry result in the checkpoint before
starting another pass.

The batch limit counts PRs the image builder actually attempts; PRs skipped by
resume logic do not count. GHCR publishing failures still stop processing, and
the failed-to-publish verified images are retained locally for retry.

To rebuild every PR image from scratch, including previously successful builds,
use `--rebuild-all-images` instead:

```bash
python scripts/pr_tagging_script.py migrations-prs.json \
  --rebuild-all-images
```

This does not erase dataset history first; each PR's saved status is refreshed
as it is processed. The full-rebuild option is mutually exclusive with
`--retry-failed-images`.

Removing PR tags does not delete the shared base image or Docker's build cache.
The cache is shared with other Docker projects and is left alone by default.
`--prune-build-cache` explicitly removes all unused Docker builder cache after
each repository, or after each batch when `--batch-size` is used; this can
reclaim space but can slow later builds in any project using Docker. If cleanup
or Docker fails, the orchestrator stops instead of proceeding to the next
repository or batch.

## Publish and collect coverage

Configure GHCR credentials before publishing:

```bash
docker login ghcr.io
python scripts/pr_tagging_script.py migrations-prs.json
```

Only execution-verified images are pushed. Confirm package visibility in GHCR
after the first successful push.

Run JaCoCo tasks for the verified base images and generate per-file JSON and
Excel reports:

```bash
python scripts/jacoco_debug_script.py migrations-prs.json
python scripts/jacoco_parser_script.py migrations-prs.json
```

The collector processes verified PRs one at a time. If a verified image is no
longer present locally after publishing, it pulls the recorded `ghcr_tag`
before running JaCoCo; local images are used directly when available. A failed
pull is recorded in the PR's coverage status instead of being silently
treated as zero coverage.
To limit disk usage, pass `--delete-image-after`; this removes both the local
tag and any GHCR-tagged copy pulled for that PR after its report is collected.
The reports remain under `reports/` and can be parsed afterward.

The scripts follow the same five-stage layout as the original medTimer
workflow: image building, image tagging, JaCoCo collection, JaCoCo parsing,
and migrated-file detection. The enhanced retry, toolchain, GHCR, offline
verification, and repository batching logic is consolidated into those
scripts rather than separate helper modules.

Coverage reports are grouped by repository and PR using the layout
`reports/<owner>__<repo>/pr_<number>/`. For example, medTimer PR 887 is stored
at `reports/futsch1__medtimer/pr_887/jacoco.xml`.

For `Futsch1/medTimer`, collection uses the repository's historical fallback
task sequence and copies `app/build/reports/` from the live container. Other
Gradle repositories use discovered JaCoCo tasks and the same
first-successful-task and report-copy strategy where applicable.

Only the selected JaCoCo XML is retained under
`reports/<owner__repo>/pr_<number>/jacoco.xml`; intermediate XML and HTML
artifacts are discarded.
Coverage summaries are written to `coverage-reports.json` and
`coverage-reports.xlsx`. The workbook's manual-validation column remains
`pending` until a person reviews the result. Source files added only after the
base commit are marked `not_in_base`; files absent from the JaCoCo report are
marked `not_in_report`, not assigned fabricated zero coverage.

The workbook includes a `PR Coverage Summary` worksheet with one row for every
PR instance, including instances where JaCoCo was not run or did not produce a
usable report. Line and branch percentages aggregate only changed Java/Kotlin
source files that map to the base-state JaCoCo report; they are not whole-project
coverage or changed-line coverage. The remaining repository worksheets retain
the per-file detail, including whether a corresponding test file exists.
Statuses distinguish `passed`, `unsupported`, `not_found`, `failed`,
`not_run`, and parser errors; unavailable coverage is left blank rather than
reported as zero.

Coverage results record a `coverage_failure_type` when collection does not
produce a report. Values distinguish missing images, unavailable dependencies,
task discovery failures, task execution failures, missing reports, and
projects without a coverage task.

To rebuild completed PR images whose local tags were removed, use the targeted
retry mode:

```bash
python -u scripts/pr_image_script.py migrations-prs.json \
  --repo Chiloven945/lukosBot2 \
  --repo ab5tract/raku-intellij-plugin \
  --resume \
  --retry-missing-images
```

Test augmentation is a separate follow-up: this pipeline identifies changed
files and their measured base-state coverage but does not author new tests.
