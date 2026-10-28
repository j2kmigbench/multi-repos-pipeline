import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def run_command(command, *, cwd=None, check=False, timeout=None):
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


GH_API_TIMEOUT = 45
DOCKER_PREFLIGHT_TIMEOUT = 15

JAVA_RUNTIME_HOMES = {
    8: "/usr/lib/jvm/java-8-openjdk-amd64",
    11: "/usr/lib/jvm/java-11-openjdk-amd64",
    17: "/usr/lib/jvm/java-17-openjdk-amd64",
    21: "/usr/lib/jvm/java-21-openjdk-amd64",
    25: "/usr/lib/jvm/java-25-openjdk-amd64",
}

SKYHANNI_REPO = "hannibal002/SkyHanni"
NEXTCLOUD_ANDROID = "nextcloud/android"
ADOBE_S3MOCK = "adobe/S3Mock"
INFINL_ZAAKAFHANDEL_COMPONENT = "infonl/dimpact-zaakafhandelcomponent"
SKYHANNI_REPO_ARCHIVE_COMMIT = "8a224024a157523e71f9a314204438b3e5f62222"
SKYHANNI_REPO_ARCHIVE_SHA256 = (
    "062de7f67f0d014665914ceab3e4300008f7f498287527015fb117cdf48d6ab8"
)
SKYHANNI_NEU_REPO_ARCHIVE_COMMIT = "c4f7d4757641f3a11c74d3d77cec76c100abaf98"
SKYHANNI_NEU_REPO_ARCHIVE_SHA256 = (
    "b729636e7fdb4df2960d8bf40d24f2a814b7dbfce421cdfed496850e1a2000f3"
)
DEFAULT_SKYHANNI_REPO_ARCHIVE = (
    Path.home()
    / ".cache"
    / "multi-repos-pipeline"
    / f"skyhanni-repo-{SKYHANNI_REPO_ARCHIVE_COMMIT}.zip"
)
DEFAULT_SKYHANNI_NEU_REPO_ARCHIVE = (
    Path.home()
    / ".cache"
    / "multi-repos-pipeline"
    / f"skyhanni-neu-repo-{SKYHANNI_NEU_REPO_ARCHIVE_COMMIT}.tar.gz"
)
SKYHANNI_REPO_ARCHIVE_CONTAINER_PATH = (
    "/opt/pipeline-assets/skyhanni-repo.zip"
)
SKYHANNI_NEU_REPO_ARCHIVE_CONTAINER_PATH = (
    "/opt/pipeline-assets/skyhanni-neu-repo.tar.gz"
)
SKYHANNI_REPO_ARCHIVE_INIT_CONTAINER_PATH = (
    "/opt/pipeline-assets/skyhanni_archive.init.gradle"
)


def _validate_pinned_archive(path, expected_sha256, description, commit):
    archive_path = Path(path).expanduser().resolve()
    if not archive_path.is_file():
        raise ValueError(
            f"Pinned {description} archive not found: {archive_path}. "
            "Download the archive instructions from scripts/README.md."
        )

    digest = hashlib.sha256()
    with archive_path.open("rb") as archive:
        for chunk in iter(lambda: archive.read(1024 * 1024), b""):
            digest.update(chunk)
    actual_sha256 = digest.hexdigest()
    if actual_sha256 != expected_sha256:
        raise ValueError(
            f"Pinned {description} archive has SHA-256 {actual_sha256}; "
            f"expected {expected_sha256} for commit {commit}."
        )
    return archive_path


def validate_skyhanni_repo_archive(path):
    return _validate_pinned_archive(
        path,
        SKYHANNI_REPO_ARCHIVE_SHA256,
        "SkyHanni repo",
        SKYHANNI_REPO_ARCHIVE_COMMIT,
    )


def validate_skyhanni_neu_repo_archive(path):
    return _validate_pinned_archive(
        path,
        SKYHANNI_NEU_REPO_ARCHIVE_SHA256,
        "SkyHanni NEU repo",
        SKYHANNI_NEU_REPO_ARCHIVE_COMMIT,
    )


def check_docker_available():
    command = ["docker", "version"]
    try:
        result = run_command(command, timeout=DOCKER_PREFLIGHT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"Docker preflight could not run `docker version`: {error}") from error

    if result.returncode:
        details = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            "Docker preflight failed; check the Docker installation and daemon. "
            f"{details}"
        )


def save_json(path, data):
    json_path = Path(path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=json_path.parent,
        prefix=f".{json_path.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary_file:
        json.dump(data, temporary_file, indent=2)
        temporary_path = Path(temporary_file.name)
    os.replace(temporary_path, json_path)


def resolve_pr_shas(repo_name, pr_number):
    command = ["gh", "api", f"repos/{repo_name}/pulls/{pr_number}"]
    try:
        result = run_command(command, timeout=GH_API_TIMEOUT)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"GitHub API request timed out after {GH_API_TIMEOUT} seconds"
        ) from error
    if result.returncode:
        raise RuntimeError(
            result.stderr.strip()
            or f"gh api failed with exit code {result.returncode}"
        )
    try:
        pull_request = json.loads(result.stdout)
        base_sha = pull_request["base"]["sha"]
        head_sha = pull_request["head"]["sha"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise RuntimeError(f"GitHub PR response did not contain base/head SHAs: {error}") from error
    if not re.fullmatch(r"[0-9a-fA-F]{40}", base_sha) or not re.fullmatch(
        r"[0-9a-fA-F]{40}", head_sha
    ):
        raise RuntimeError("GitHub PR API returned an invalid base/head SHA")
    return base_sha.lower(), head_sha.lower()


def discover_build_project(repo_path):
    root = Path(repo_path)
    candidates = []
    root_gradle_file = any(
        (root / name).is_file()
        for name in ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")
    )
    if (root / "gradlew").is_file() or root_gradle_file:
        candidates.append(("gradle", root))
    elif (root / "mvnw").is_file() or (root / "pom.xml").is_file():
        candidates.append(("maven", root))
    else:
        for wrapper in root.rglob("gradlew"):
            if not any(part in {".git", "build", ".gradle"} for part in wrapper.parts):
                candidates.append(("gradle", wrapper.parent))
        if not candidates:
            for pom in root.rglob("pom.xml"):
                if not any(part in {".git", "target"} for part in pom.parts):
                    candidates.append(("maven", pom.parent))

    unique = {(kind, path.resolve()) for kind, path in candidates}
    if not unique:
        return None
    if len(unique) != 1:
        return {
            "kind": "ambiguous",
            "path": "",
            "candidates": [
                {"kind": kind, "path": path.relative_to(root.resolve()).as_posix()}
                for kind, path in sorted(unique, key=lambda value: str(value[1]))
            ],
        }

    kind, project_path = unique.pop()
    relative_path = project_path.relative_to(root.resolve()).as_posix()
    return {
        "kind": kind,
        "path": "" if relative_path == "." else relative_path,
        "gradle_wrapper": (project_path / "gradlew").is_file() if kind == "gradle" else False,
        "maven_wrapper": (project_path / "mvnw").is_file() if kind == "maven" else False,
        "candidates": [],
    }


def build_command(project, *, offline=False, skip_docker=False):
    relative_path = project.get("path", "")
    if project["kind"] == "gradle":
        command = [
            "./gradlew" if project.get("gradle_wrapper", True) else "gradle",
            "--no-daemon",
            "--no-build-cache",
            "--rerun-tasks",
        ]
        if offline:
            command.append("--offline")
        return command + ["clean", "build"], relative_path
    if project["kind"] == "maven":
        wrapper = "./mvnw" if project.get("maven_wrapper", True) else "mvn"
        command = [wrapper, "--batch-mode", "--no-transfer-progress"]
        if offline:
            command.append("--offline")
        if skip_docker:
            command.append("-DskipDocker")
        return command + ["clean", "verify"], relative_path
    raise ValueError(f"Unsupported build system: {project['kind']}")


def clean_command(project):
    relative_path = project.get("path", "")
    if project["kind"] == "gradle":
        wrapper = "./gradlew" if project.get("gradle_wrapper", True) else "gradle"
        return [wrapper, "--no-daemon", "clean"], relative_path
    if project["kind"] == "maven":
        wrapper = "./mvnw" if project.get("maven_wrapper", True) else "mvn"
        return [wrapper, "--batch-mode", "--no-transfer-progress", "clean"], relative_path
    raise ValueError(f"Unsupported build system: {project['kind']}")


def container_workdir(relative_path):
    return "/workspace/repo" + (f"/{relative_path}" if relative_path else "")


def detect_java_target(repo_path, project):
    root = Path(repo_path)
    project_root = root / project.get("path", "")
    version_patterns = (
        re.compile(r"JavaVersion\.VERSION_(?:1[._])?(\d+)"),
        re.compile(
            r"(?:sourceCompatibility|targetCompatibility)\s*[=:]\s*"
            r"(?:JavaVersion\.VERSION_)?(?:1[._])?['\"]?(\d+)"
        ),
        re.compile(
            r"<maven\.compiler\.(?:source|target|release)>\s*(?:1[._])?(\d+)"
        ),
        re.compile(r"<(?:source|target|release)>\s*(?:1[._])?(\d+)"),
        re.compile(r"jvmTarget\s*[=:]\s*['\"]?(?:JVM_)?(?:1[._])?(\d+)"),
        re.compile(r"JavaLanguageVersion\.of\((\d+)\)"),
        re.compile(r"(?m)^\s*java-version\s*=\s*['\"]?(?:1[._])?(\d+)"),
    )
    target_versions = set()
    build_files = []
    for name in ("pom.xml", "build.gradle", "build.gradle.kts", "gradle.properties"):
        build_files.extend(project_root.rglob(name))
    for build_file in build_files:
        if any(part in {".git", ".gradle", "build", "target"} for part in build_file.parts):
            continue
        try:
            content = build_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pattern in version_patterns:
            target_versions.update(pattern.findall(content))
    return sorted(target_versions, key=lambda version: int(version))


def detect_android_requirements(repo_path, project):
    project_root = Path(repo_path) / project.get("path", "")
    compile_sdks = set()
    agp_versions = set()
    for build_file in project_root.rglob("*"):
        if build_file.name not in {"build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"}:
            continue
        if any(part in {".git", ".gradle", "build", "target"} for part in build_file.parts):
            continue
        try:
            content = build_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        compile_sdks.update(
            re.findall(r"\bcompileSdk(?:Version)?\s*(?:=\s*)?[\"']?(\d{2,3})", content)
        )
        agp_versions.update(
            re.findall(r"com\.android\.tools\.build:gradle:([0-9][0-9A-Za-z.+-]*)", content)
        )
        agp_versions.update(
            re.findall(
                r"id\s*\(?[\"']com\.android\.(?:application|library|test)[\"']\)?"
                r"\s*version\s*[\"']([0-9][0-9A-Za-z.+-]*)",
                content,
            )
        )

    wrapper_properties = (
        project_root / "gradle" / "wrapper" / "gradle-wrapper.properties"
    )
    gradle_version = None
    if wrapper_properties.is_file():
        content = wrapper_properties.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"gradle-([0-9]+(?:\.[0-9]+){1,2})-(?:all|bin)\.zip", content)
        if match:
            gradle_version = match.group(1)
    return {
        "android_compile_sdks": sorted(compile_sdks, key=int),
        "android_gradle_plugin_versions": sorted(agp_versions),
        "gradle_wrapper_version": gradle_version,
    }


def select_java_runtime_version(gradle_wrapper_version, java_target_versions=()):
    if "25" in {str(version) for version in java_target_versions}:
        return 25
    if not gradle_wrapper_version:
        return 21

    match = re.match(r"^(\d+)\.(\d+)", str(gradle_wrapper_version))
    if not match:
        raise ValueError(
            f"Unsupported Gradle wrapper version: {gradle_wrapper_version}"
        )

    version = tuple(map(int, match.groups()))
    if version < (5, 0):
        return 8
    if version < (7, 3):
        return 11
    if version < (8, 5):
        return 17
    return 21


def java_runtime_home(version):
    try:
        return JAVA_RUNTIME_HOMES[version]
    except KeyError as error:
        raise ValueError(f"Unsupported Java runtime version: {version}") from error


def has_gradle_test_sources(project_root):
    root = Path(project_root)
    source_extensions = {".java", ".kt", ".groovy", ".scala"}
    ignored_directories = {".git", ".gradle", "build", "target"}
    for test_root in root.glob("**/src/test"):
        if ignored_directories.intersection(test_root.relative_to(root).parts):
            continue
        if any(
            source.is_file() and source.suffix.lower() in source_extensions
            for source in test_root.rglob("*")
        ):
            return True
    return False


def inspect_commit_build(repo_path, commit_sha):
    with tempfile.TemporaryDirectory(prefix="build-detect-") as temporary_dir:
        worktree = Path(temporary_dir) / "repo"
        add_worktree = run_command(
            ["git", "worktree", "add", "--detach", str(worktree), commit_sha],
            cwd=repo_path,
        )
        if add_worktree.returncode:
            raise RuntimeError(
                add_worktree.stderr.strip()
                or f"Could not inspect build configuration at {commit_sha}"
            )
        try:
            project = discover_build_project(worktree)
            requirements = {}
            if project and project["kind"] not in {"ambiguous"}:
                requirements["java_target_versions"] = detect_java_target(worktree, project)
                requirements.update(detect_android_requirements(worktree, project))
                if project["kind"] == "gradle":
                    is_android = bool(
                        requirements["android_compile_sdks"]
                        or requirements["android_gradle_plugin_versions"]
                    )
                    project["build_target"] = (
                        "assembleDebug" if is_android else "assemble"
                    )
                    project["has_test_sources"] = has_gradle_test_sources(
                        worktree / project["path"]
                    )
            if project and project["kind"] == "maven":
                project["maven_wrapper"] = (
                    worktree / project["path"] / "mvnw"
                ).is_file()
            return project, requirements
        finally:
            remove_worktree = run_command(
                ["git", "worktree", "remove", "--force", str(worktree)],
                cwd=repo_path,
            )
            if remove_worktree.returncode:
                print(
                    f"Warning: could not remove temporary worktree {worktree}: "
                    f"{remove_worktree.stderr.strip()}"
                )


def formatted_repo_name(repo_name):
    return repo_name.replace("/", "__").lower()


def docker_tag(repo_name, pr_number, base_sha):
    return f"{formatted_repo_name(repo_name)}:pr-{pr_number}-base-{base_sha[:7]}"


def docker_worktree_image(repo_path, base_sha, tag, pr_number, base_image):
    with tempfile.TemporaryDirectory(prefix=f"pr-{pr_number}-base-") as temporary_dir:
        worktree = Path(temporary_dir) / "repo"
        add_worktree = run_command(
            ["git", "worktree", "add", "--detach", str(worktree), base_sha],
            cwd=repo_path,
        )
        if add_worktree.returncode:
            return add_worktree

        try:
            base_commit_date = run_command(
                ["git", "show", "-s", "--format=%cI", base_sha],
                cwd=repo_path,
            )
            if base_commit_date.returncode:
                return base_commit_date

            dockerfile = Path(temporary_dir) / ".pipeline.Dockerfile"
            dockerfile.write_text(
                f"""FROM {base_image}
WORKDIR /workspace/repo
COPY . /workspace/repo
ARG PIPELINE_BASE_COMMIT_DATE
RUN rm -rf .git && \\
    git init -q && \\
    git config user.email "bench@j2kmigbench.org" && \\
    git config user.name "J2KBench" && \\
    find /workspace/repo -type f \\( -name gradlew -o -name mvnw \\) -exec chmod +x {{}} + && \\
    git add -A && \\
    GIT_AUTHOR_DATE="$PIPELINE_BASE_COMMIT_DATE" \\
    GIT_COMMITTER_DATE="$PIPELINE_BASE_COMMIT_DATE" \\
    git commit -q -m "base state"
""",
                encoding="utf-8",
            )
            dockerfile.with_name(f"{dockerfile.name}.dockerignore").write_text(
                "", encoding="utf-8"
            )
            return run_command(
                [
                    "docker",
                    "build",
                    "-t",
                    tag,
                    "--pull=false",
                    "--build-arg",
                    f"PIPELINE_BASE_COMMIT_DATE={base_commit_date.stdout.strip()}",
                    "-f",
                    str(dockerfile),
                    str(worktree),
                ]
            )
        finally:
            remove_worktree = run_command(
                ["git", "worktree", "remove", "--force", str(worktree)],
                cwd=repo_path,
            )
            if remove_worktree.returncode:
                print(
                    f"Warning: could not remove temporary worktree {worktree}: "
                    f"{remove_worktree.stderr.strip()}"
                )


def ensure_commit_available(repo_path, sha):
    check = run_command(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
        cwd=repo_path,
    )
    if check.returncode == 0:
        return
    fetch = run_command(["git", "fetch", "--no-tags", "origin", sha], cwd=repo_path)
    if fetch.returncode:
        raise RuntimeError(
            fetch.stderr.strip() or f"Could not fetch required commit {sha}"
        )

def print_result(label, result):
    print(f"{label} failed with exit code {result.returncode}.", file=sys.stderr)
    if result.stdout:
        print(result.stdout, file=sys.stderr)
    if result.stderr:
        print(result.stderr, file=sys.stderr)


def command_error(result):
    return "\n".join(part for part in (result.stderr.strip(), result.stdout.strip()) if part)


INFRASTRUCTURE_FAILURE_PATTERNS = (
    (
        "network/dependency",
        (
            "unknownhostexception",
            "unknown host",
            "could not resolve host",
            "could not resolve all files",
            "failed to resolve",
            "could not get resource",
            "could not download",
            "failed to download",
            "401 unauthorized",
            "authentication failed",
            "403 forbidden",
            "read timed out",
            "timed out",
            "connect timed out",
            "connection reset",
            "connection refused",
            "no route to host",
            "temporary failure in name resolution",
            "repository not found",
            "host not found",
        ),
    ),
    (
        "java toolchain",
        (
            "cannot find a java installation",
            "could not determine java version",
            "java_home",
            "unsupported class file major version",
            "languageversion",
            "no matching toolchains found",
            "release version is not supported",
            "jdk",
            "java home",
            "toolchain",
        ),
    ),
    (
        "android sdk/agp",
        (
            "failed to install the following android sdk packages",
            "failed to find target with hash string",
            "sdk location not found",
            "could not determine sdk root",
            "sdkmanager: command not found",
            "android_sdk_root",
            "android gradle plugin requires",
            "minimum supported gradle version is",
        ),
    ),
    (
        "repo/docker state",
        (
            "fatal: not a git repository",
            "not a git repository",
            "no such container",
            "no such image",
            "input/output error",
            "docker version",
            "could not connect to docker daemon",
            "error creating container",
            "nothing to commit",
            "failed to unmarshal the received message: string field contains invalid utf-8",
            "worktree",
            "repository disappeared",
            "could not inspect build configuration",
        ),
    ),
)


def classify_failure_bucket(message):
    text = str(message or "").lower()
    if not text:
        return "project build failure"
    for bucket, patterns in INFRASTRUCTURE_FAILURE_PATTERNS:
        if any(pattern in text for pattern in patterns):
            return bucket
    return "project build failure"


def failure_bucket_for_item(item):
    if not isinstance(item, dict):
        return "project build failure"
    for key in (
        "image_build_error",
        "build_error",
        "test_error",
        "dependency_cache_clean_error",
        "offline_build_error",
        "offline_test_error",
        "execution_error",
        "sha_resolution_error",
        "network_verification_error",
        "git_verification_error",
    ):
        bucket = classify_failure_bucket(item.get(key))
        if bucket != "project build failure":
            return bucket
    return "project build failure"


def is_retryable_infrastructure_failure(item):
    return failure_bucket_for_item(item) in {
        "network/dependency",
        "java toolchain",
        "android sdk/agp",
        "repo/docker state",
    }


def gradle_test_tasks(project):
    if not project.get("has_test_sources", True):
        return []
    if project.get("build_target") == "assembleDebug":
        return ["testDebugUnitTest"]
    return ["test"]


def java_runtime_shell_prefix(java_home):
    if not java_home:
        return []
    return [
        "sh",
        "-c",
        'export PATH="$JAVA_HOME/bin:$PATH"; exec "$@"',
        "selected-java-runtime",
    ]


def execute_in_container(
    container_name,
    workdir,
    command,
    label,
    java_home=None,
    environment_names=(),
    environment_variables=(),
):
    environment = ["-e", f"JAVA_HOME={java_home}"] if java_home else []
    environment.extend(
        part for name in environment_names for part in ("-e", name)
    )
    environment.extend(
        part for variable in environment_variables for part in ("-e", variable)
    )
    runtime_command = [*java_runtime_shell_prefix(java_home), *command]
    result = run_command(
        [
            "docker",
            "exec",
            *environment,
            "-w",
            workdir,
            container_name,
            *runtime_command,
        ]
    )
    if result.returncode:
        print_result(label, result)
        return False, command_error(result)
    return True, None


def copy_skyhanni_archive_inputs(container_name, archive_path, neu_archive_path):
    init_script = Path(__file__).with_name("skyhanni_archive.init.gradle")
    mkdir = run_command(
        ["docker", "exec", container_name, "mkdir", "-p", "/opt/pipeline-assets"]
    )
    if mkdir.returncode:
        return command_error(mkdir)

    for source, destination in (
        (archive_path, SKYHANNI_REPO_ARCHIVE_CONTAINER_PATH),
        (neu_archive_path, SKYHANNI_NEU_REPO_ARCHIVE_CONTAINER_PATH),
        (init_script, SKYHANNI_REPO_ARCHIVE_INIT_CONTAINER_PATH),
    ):
        result = run_command(
            ["docker", "cp", str(source), f"{container_name}:{destination}"]
        )
        if result.returncode:
            return command_error(result)
    return None


def execute_gradle_test_tasks(command_prefix, gradle_args, tasks, label):
    command = [*command_prefix, *gradle_args, *tasks]
    result = run_command(command)
    if result.returncode:
        error = command_error(result)
        candidates_match = re.search(
            r"Candidates are:\s*((?:'[^']+'\s*,?\s*)+)", error
        )
        candidates = (
            list(dict.fromkeys(re.findall(r"'([^']+)'", candidates_match.group(1))))
            if candidates_match
            else []
        )
        if len(candidates) > 1:
            tasks = candidates
            result = run_command([*command_prefix, *gradle_args, *tasks])

    if result.returncode:
        print_result(label, result)
        return False, command_error(result), tasks
    return True, None, tasks


def build_and_test(
    container_name,
    project,
    java_home=None,
    maven_settings_path=None,
    maven_auth_environment=(),
    skyhanni_init_script_path=None,
    disable_configuration_cache=False,
    gradle_stacktrace=False,
    skip_docker=False,
    environment_variables=(),
):
    workdir = container_workdir(project["path"])
    if project["kind"] == "gradle":
        gradle_command = (
            "./gradlew" if project.get("gradle_wrapper", True) else "gradle"
        )
        clean = [
            gradle_command,
            "--no-daemon",
            "--no-build-cache",
            "--rerun-tasks",
            *(["--stacktrace"] if gradle_stacktrace else []),
            *(["--no-configuration-cache"] if disable_configuration_cache else []),
            "clean",
        ]
        clean_ok, clean_error = execute_in_container(
            container_name,
            workdir,
            clean,
            "gradle clean",
            java_home=java_home,
            environment_variables=environment_variables,
        )
        if not clean_ok:
            return "failed", "not_run", [], clean_error, None
        build = [
            gradle_command,
            "--no-daemon",
            "--no-build-cache",
            "--rerun-tasks",
            *(["--stacktrace"] if gradle_stacktrace else []),
            *(
                ["--init-script", skyhanni_init_script_path]
                if skyhanni_init_script_path
                else []
            ),
            *(["--no-configuration-cache"] if disable_configuration_cache else []),
            project.get("build_target", "assemble"),
        ]
    else:
        wrapper = "./mvnw" if project["maven_wrapper"] else "mvn"
        build = [
            wrapper,
            "--batch-mode",
            "--no-transfer-progress",
            "dependency:go-offline",
            "clean",
            "verify" if skip_docker else "package",
            "-DskipTests",
            *(["-DskipDocker"] if skip_docker else []),
        ]
        if maven_settings_path:
            build[1:1] = ["--settings", maven_settings_path]

    build_ok, build_error = execute_in_container(
        container_name,
        workdir,
        build,
        f"{project['kind']} {build[-1]}",
        java_home=java_home,
        environment_names=(
            maven_auth_environment if project["kind"] == "maven" else ()
        ),
        environment_variables=environment_variables,
    )
    if not build_ok:
        return "failed", "not_run", [], build_error, None

    if project["kind"] == "gradle":
        tasks = gradle_test_tasks(project)
        if not tasks:
            return (
                "passed",
                "unsupported",
                [],
                None,
                "No Gradle unit-test source files were found.",
            )
        test_args = [
            gradle_command,
            "--no-daemon",
            "--no-build-cache",
            "--rerun-tasks",
            *(["--stacktrace"] if gradle_stacktrace else []),
            *(
                ["--init-script", skyhanni_init_script_path]
                if skyhanni_init_script_path
                else []
            ),
            *(["--no-configuration-cache"] if disable_configuration_cache else []),
        ]

        test_ok, test_error, tasks = execute_gradle_test_tasks(
            [
                "docker",
                "exec",
                *(["-e", f"JAVA_HOME={java_home}"] if java_home else []),
                *(
                    part
                    for variable in environment_variables
                    for part in ("-e", variable)
                ),
                "-w",
                workdir,
                container_name,
                *java_runtime_shell_prefix(java_home),
            ],
            test_args,
            tasks,
            "gradle tests",
        )
    else:
        tasks = ["test"]
        wrapper = "./mvnw" if project["maven_wrapper"] else "mvn"
        test_command = [wrapper, "--batch-mode", "--no-transfer-progress"]
        if skip_docker:
            test_command.append("-DskipDocker")
        test_command.append("test")
        if maven_settings_path:
            test_command[1:1] = ["--settings", maven_settings_path]
        test_ok, test_error = execute_in_container(
            container_name,
            workdir,
            test_command,
            f"{project['kind']} tests",
            java_home=java_home,
            environment_names=maven_auth_environment,
            environment_variables=environment_variables,
        )
    return "passed", "passed" if test_ok else "failed", tasks, None, test_error


def offline_verify(
    tag,
    project,
    test_tasks,
    java_home=None,
    skyhanni_init_script_path=None,
    disable_configuration_cache=False,
    gradle_stacktrace=False,
    skip_docker=False,
    environment_variables=(),
):
    environment = ["-e", f"JAVA_HOME={java_home}"] if java_home else []
    environment.extend(
        part for variable in environment_variables for part in ("-e", variable)
    )
    runtime_prefix = java_runtime_shell_prefix(java_home)
    if project["kind"] == "gradle":
        gradle_command = (
            "./gradlew" if project.get("gradle_wrapper", True) else "gradle"
        )
        relative_path = project["path"]
        offline_clean = [
            gradle_command,
            "--no-daemon",
            "--no-build-cache",
            "--rerun-tasks",
            "--offline",
            *(["--stacktrace"] if gradle_stacktrace else []),
            *(["--no-configuration-cache"] if disable_configuration_cache else []),
            "clean",
        ]
        clean = run_command(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--hostname",
                "localhost",
                *environment,
                "-w",
                container_workdir(relative_path),
                tag,
                *runtime_prefix,
                *offline_clean,
            ]
        )
        if clean.returncode:
            print_result("Offline clean", clean)
            return False, False, command_error(clean), None
        offline_build = [
            gradle_command,
            "--no-daemon",
            "--no-build-cache",
            "--rerun-tasks",
            "--offline",
            *(["--stacktrace"] if gradle_stacktrace else []),
            *(
                ["--init-script", skyhanni_init_script_path]
                if skyhanni_init_script_path
                else []
            ),
            *(["--no-configuration-cache"] if disable_configuration_cache else []),
            project.get("build_target", "assemble"),
        ]
    else:
        offline_build, relative_path = build_command(
            project, offline=True, skip_docker=skip_docker
        )
    workdir = container_workdir(relative_path)

    build = run_command(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--hostname",
            "localhost",
            *environment,
            "-w",
            workdir,
            tag,
            *runtime_prefix,
            *offline_build,
        ]
    )
    if build.returncode:
        print_result("Offline build", build)
        return False, False, command_error(build), None

    if project["kind"] == "gradle":
        if not test_tasks:
            return True, False, None, "No Gradle unit-test task was found."
        test_args = [
            gradle_command,
            "--no-daemon",
            "--no-build-cache",
            "--rerun-tasks",
            "--offline",
            *(["--stacktrace"] if gradle_stacktrace else []),
            *(
                ["--init-script", skyhanni_init_script_path]
                if skyhanni_init_script_path
                else []
            ),
            *(["--no-configuration-cache"] if disable_configuration_cache else []),
        ]
        tests_passed, tests_error, test_tasks = execute_gradle_test_tasks(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--hostname",
                "localhost",
                *environment,
                "-w",
                workdir,
                tag,
                *runtime_prefix,
            ],
            test_args,
            test_tasks,
            "Offline tests",
        )
    else:
        wrapper = "./mvnw" if project["maven_wrapper"] else "mvn"
        test_command = [
            wrapper,
            "--batch-mode",
            "--no-transfer-progress",
            "--offline",
        ]
        if skip_docker:
            test_command.append("-DskipDocker")
        test_command.append("test")

        tests = run_command(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--hostname",
                "localhost",
                *environment,
                "-w",
                workdir,
                tag,
                *runtime_prefix,
                *test_command,
            ]
        )
        tests_passed = tests.returncode == 0
        tests_error = command_error(tests) if tests.returncode else None
    if not tests_passed:
        return True, False, None, tests_error
    return (
        True,
        True,
        None,
        None,
    )


def verify_git_history(tag):
    result = run_command(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            tag,
            "git",
            "log",
            "--all",
            "--oneline",
        ]
    )
    if result.returncode:
        print_result("Git history verification", result)
        return False, command_error(result)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        return False, f"Expected one commit in image, found {len(lines)}."
    return True, None


def verify_network_blocked(tag):
    probe = (
        "import socket, sys\n"
        "try:\n"
        "    socket.create_connection(('api.github.com', 443), timeout=3)\n"
        "except OSError:\n"
        "    print('network blocked')\n"
        "else:\n"
        "    print('network reachable')\n"
        "    sys.exit(1)\n"
    )
    result = run_command(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            tag,
            "python3",
            "-c",
            probe,
        ]
    )
    if result.returncode == 0 and result.stdout.strip() == "network blocked":
        return True, None
    error = command_error(result) or "Network probe did not confirm blocked connectivity."
    print(f"Network-isolation verification failed: {error}", file=sys.stderr)
    return False, error


def process_pr(
    repo_name,
    repo_path,
    item,
    base_image,
    maven_settings=None,
    skyhanni_repo_archive=None,
    skyhanni_neu_repo_archive=None,
    gradle_stacktrace=False,
):
    pr_number = item["pr_number"]
    item["tier"] = "unverified"
    item["ghcr_result"] = False
    item.pop("ghcr_tag", None)
    item.pop("local_image_removed", None)
    item["docker_tag"] = None
    item["image_build_result"] = "not_run"
    item["build_result"] = "not_run"
    item["test_result"] = "not_run"
    try:
        base_sha, head_sha = resolve_pr_shas(repo_name, pr_number)
    except (OSError, RuntimeError) as error:
        item["base_sha_full"] = None
        item["head_sha_full"] = None
        item["sha_resolution_result"] = "failed"
        item["sha_resolution_error"] = str(error)
        item["docker_tag"] = None
        item["image_build_result"] = "not_run"
        item["build_result"] = "not_run"
        item["test_result"] = "not_run"
        item["tier"] = "unverified"
        print(f"Could not resolve SHAs for {repo_name} PR #{pr_number}: {error}", file=sys.stderr)
        return

    item["base_sha_full"] = base_sha
    item["head_sha_full"] = head_sha
    item["sha_resolution_result"] = "passed"
    item.pop("sha_resolution_error", None)

    try:
        ensure_commit_available(repo_path, base_sha)
    except (OSError, RuntimeError) as error:
        item["image_build_result"] = "not_run"
        item["build_result"] = "not_run"
        item["test_result"] = "not_run"
        item["docker_tag"] = None
        item["tier"] = "unverified"
        item["image_build_error"] = str(error)
        print(f"Base SHA unavailable for {repo_name} PR #{pr_number}: {error}", file=sys.stderr)
        return

    try:
        project, requirements = inspect_commit_build(repo_path, base_sha)
    except (OSError, RuntimeError) as error:
        item["build_system"] = "unknown"
        item["image_build_result"] = "not_run"
        item["build_result"] = "not_run"
        item["test_result"] = "not_run"
        item["docker_tag"] = None
        item["tier"] = "unverified"
        item["warning"] = f"Could not inspect base-SHA build configuration: {error}"
        return

    if project is None or project["kind"] in {"ambiguous"}:
        item["build_system"] = "unsupported"
        item["build_project_dir"] = None
        item["image_build_result"] = "not_run"
        item["build_result"] = "not_run"
        item["test_result"] = "not_run"
        item["docker_tag"] = None
        item["tier"] = "unverified"
        item["warning"] = (
            "No Gradle or Maven build root found."
            if project is None
            else f"Multiple build roots found: {project['candidates']}"
        )
        return

    item["build_system"] = project["kind"]
    item["build_project_dir"] = project["path"] or "."
    item["gradle_wrapper"] = project.get("gradle_wrapper", False)
    item["maven_wrapper"] = project.get("maven_wrapper", False)
    if project["kind"] == "gradle":
        item["build_target"] = project.get("build_target", "assemble")
        item["has_test_sources"] = project.get("has_test_sources", False)
    else:
        item.pop("build_target", None)
    item.update(requirements)
    item["java_base_image"] = base_image
    java_version = select_java_runtime_version(
        requirements.get("gradle_wrapper_version")
        if project["kind"] == "gradle"
        else None,
        requirements.get("java_target_versions", ()),
    )
    java_home = java_runtime_home(java_version)
    item["java_runtime_version"] = java_version
    item["java_runtime_home"] = java_home
    item["image_build_result"] = "failed"
    item["build_result"] = "not_run"
    item["test_result"] = "not_run"
    item["tier"] = "unverified"
    item["git_result"] = False
    item["network_result"] = False
    item["docker_tag"] = None
    item["image_size"] = None
    item["test_tasks"] = []
    item["dependency_cache_clean_result"] = "not_run"
    item["offline_build_result"] = False
    item["offline_test_result"] = False
    item["git_verification_error"] = None
    item["network_verification_error"] = None
    for key in (
        "image_build_error",
        "build_error",
        "test_error",
        "dependency_cache_clean_error",
        "offline_build_error",
        "offline_test_error",
        "execution_error",
    ):
        item.pop(key, None)

    final_tag = docker_tag(repo_name, pr_number, base_sha)
    bootstrap_tag = f"{final_tag}-bootstrap"
    try:
        image = docker_worktree_image(
            repo_path, base_sha, bootstrap_tag, pr_number, base_image
        )
    except OSError as error:
        item["image_build_error"] = str(error)
        item["image_build_result"] = "failed"
        print(f"Docker could not build {repo_name} PR #{pr_number}: {error}", file=sys.stderr)
        return
    if image.returncode:
        item["image_build_error"] = command_error(image)
        print_result(f"Base image build for {repo_name} PR #{pr_number}", image)
        return

    container_name = (
        f"pr-base-{formatted_repo_name(repo_name)}-{pr_number}-{base_sha[:7]}"
    )
    run_command(["docker", "rm", "--force", container_name])
    create = run_command(
        [
            "docker",
            "create",
            "--name",
            container_name,
            bootstrap_tag,
            "tail",
            "-f",
            "/dev/null",
        ]
    )
    if create.returncode:
        item["image_build_error"] = command_error(create)
        print_result("Build container creation", create)
        run_command(["docker", "image", "rm", bootstrap_tag])
        return

    start = run_command(["docker", "start", container_name])
    if start.returncode:
        item["image_build_error"] = command_error(start)
        print_result("Build container start", start)
        run_command(["docker", "rm", "--force", container_name])
        run_command(["docker", "image", "rm", bootstrap_tag])
        return

    item["image_build_result"] = "passed"
    skyhanni_init_script_path = None
    if repo_name == SKYHANNI_REPO and skyhanni_repo_archive:
        copy_error = copy_skyhanni_archive_inputs(
            container_name, skyhanni_repo_archive, skyhanni_neu_repo_archive
        )
        if copy_error:
            item["image_build_result"] = "failed"
            item["image_build_error"] = (
                "Could not copy pinned SkyHanni repo archive into build container: "
                f"{copy_error}"
            )
            print(item["image_build_error"], file=sys.stderr)
            run_command(["docker", "rm", "--force", container_name])
            run_command(["docker", "image", "rm", bootstrap_tag])
            return
        skyhanni_init_script_path = SKYHANNI_REPO_ARCHIVE_INIT_CONTAINER_PATH

    maven_settings_path = None
    if project["kind"] == "maven" and maven_settings:
        maven_settings_path = "/tmp/pipeline-maven-settings.xml"
        copy_settings = run_command(
            [
                "docker",
                "cp",
                str(maven_settings),
                f"{container_name}:{maven_settings_path}",
            ]
        )
        if copy_settings.returncode:
            item["image_build_result"] = "failed"
            item["image_build_error"] = (
                "Could not copy Maven settings into build container: "
                f"{command_error(copy_settings)}"
            )
            print_result("Maven settings copy", copy_settings)
            run_command(["docker", "rm", "--force", container_name])
            run_command(["docker", "image", "rm", bootstrap_tag])
            return

    settings_remove = None
    try:
        build_result, test_result, test_tasks, build_error, test_error = build_and_test(
            container_name,
            project,
            java_home=java_home,
            maven_settings_path=maven_settings_path,
            maven_auth_environment=(
                ("GITHUB_USERNAME", "GITHUB_PACKAGES_TOKEN")
                if maven_settings_path
                else ()
            ),
            skyhanni_init_script_path=skyhanni_init_script_path,
            disable_configuration_cache=(repo_name == NEXTCLOUD_ANDROID),
            gradle_stacktrace=gradle_stacktrace,
            skip_docker=(repo_name == ADOBE_S3MOCK),
            environment_variables=(
                ("NODE_ENV=development",)
                if repo_name == INFINL_ZAAKAFHANDEL_COMPONENT
                else ()
            ),
        )
    finally:
        if maven_settings_path:
            settings_remove = run_command(
                ["docker", "exec", container_name, "rm", "-f", maven_settings_path]
            )
    if settings_remove and settings_remove.returncode:
        item["image_build_result"] = "failed"
        item["image_build_error"] = (
            "Could not remove temporary Maven credentials before image commit: "
            f"{command_error(settings_remove)}"
        )
        print_result("Maven settings cleanup", settings_remove)
        run_command(["docker", "rm", "--force", container_name])
        run_command(["docker", "image", "rm", bootstrap_tag])
        return
    item["build_result"] = build_result
    item["test_result"] = test_result
    item["test_tasks"] = test_tasks
    if build_error:
        item["build_error"] = build_error
    if test_error:
        item["test_error"] = test_error

    clean, clean_path = clean_command(project)
    clean_result = run_command(
        [
            "docker",
            "exec",
            "-e",
            f"JAVA_HOME={java_home}",
            "-w",
            container_workdir(clean_path),
            container_name,
            *java_runtime_shell_prefix(java_home),
            *clean,
        ]
    )
    if clean_result.returncode:
        item["dependency_cache_clean_result"] = "failed"
        item["dependency_cache_clean_error"] = command_error(clean_result)
        print_result("Build output cleanup", clean_result)
    else:
        item["dependency_cache_clean_result"] = "passed"

    commit = run_command(["docker", "commit", container_name, final_tag])
    run_command(["docker", "rm", "--force", container_name])
    run_command(["docker", "image", "rm", bootstrap_tag])
    if commit.returncode:
        item["image_build_result"] = "failed"
        item["image_build_error"] = command_error(commit)
        print_result("Dependency-cache image commit", commit)
        return

    item["docker_tag"] = final_tag
    item["image_build_error"] = None

    online_build_passed = build_result == "passed"
    if online_build_passed and item["dependency_cache_clean_result"] == "passed":
        (
            item["offline_build_result"],
            item["offline_test_result"],
            item["offline_build_error"],
            item["offline_test_error"],
        ) = offline_verify(
            final_tag,
            project,
            test_tasks,
            java_home=java_home,
            skyhanni_init_script_path=skyhanni_init_script_path,
            disable_configuration_cache=(repo_name == NEXTCLOUD_ANDROID),
            gradle_stacktrace=gradle_stacktrace,
            skip_docker=(repo_name == ADOBE_S3MOCK),
            environment_variables=(
                ("NODE_ENV=development",)
                if repo_name == INFINL_ZAAKAFHANDEL_COMPONENT
                else ()
            ),
        )
    else:
        item["offline_build_result"] = False
        item["offline_test_result"] = False

    item["git_result"], item["git_verification_error"] = verify_git_history(final_tag)
    (
        item["network_result"],
        item["network_verification_error"],
    ) = verify_network_blocked(final_tag)
    size = run_command(
        ["docker", "image", "inspect", final_tag, "--format={{.Size}}"]
    )
    item["image_size"] = size.stdout.strip() if size.returncode == 0 else None
    if size.returncode:
        print_result("Image size inspection", size)

    verified = (
        item["image_build_result"] == "passed"
        and item["build_result"] == "passed"
        and item["test_result"] == "passed"
        and item["dependency_cache_clean_result"] == "passed"
        and item["offline_build_result"] is True
        and item["offline_test_result"] is True
        and item["git_result"] is True
        and item["network_result"] is True
    )
    item["tier"] = "execution-verified" if verified else "unverified"
    print(
        f"PR #{pr_number}: base={base_sha}, build={item['build_result']}, "
        f"tests={item['test_result']}, java={java_version}, "
        f"image_size={item.get('image_size')}, "
        f"tier={item['tier']}"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Resolve PR SHAs and build/verify a base-state image per PR."
    )
    parser.add_argument("json_file", help="Dataset JSON containing repositories and PRs")
    parser.add_argument(
        "--repo",
        action="append",
        help="Process only this repository (owner/name); may be repeated",
    )
    parser.add_argument(
        "--start-pr",
        type=int,
        help="Start at this PR number, inclusive; requires exactly one --repo",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Process at most this many PRs, useful for a small validation batch",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip completed PRs with built images and retry PRs without images",
    )
    parser.add_argument(
        "--skip-failed-images",
        action="store_true",
        help="With --resume, also skip completed PRs whose image builds failed",
    )
    parser.add_argument(
        "--retry-unverified",
        action="store_true",
        help="With --resume, retry completed image builds that are not execution-verified",
    )
    parser.add_argument(
        "--retry-failed-images-only",
        "--retry-image-build-failures-only",
        dest="retry_failed_images_only",
        action="store_true",
        help=(
            "With --resume, retry failed image builds while skipping every PR "
            "whose image build already passed, including interrupted records."
        ),
    )
    parser.add_argument(
        "--retry-infrastructure-failures-only",
        action="store_true",
        help=(
            "With --resume, retry PRs with recorded network/dependency, Java, "
            "Android SDK/AGP, or repo/Docker infrastructure errors only."
        ),
    )
    parser.add_argument(
        "--retry-missing-images",
        action="store_true",
        help=(
            "With --resume, rebuild execution-verified PR images that were not "
            "successfully published to GHCR."
        ),
    )
    parser.add_argument(
        "--base-image",
        default="j2kbench-base:latest",
        help="Shared build image that contains the required JDK/Android SDK",
    )
    parser.add_argument(
        "--build-base-image",
        action="store_true",
        help="Build --base-image from the repository Dockerfile.base before processing PRs",
    )
    parser.add_argument(
        "--base-dockerfile",
        default=str(Path(__file__).resolve().parent.parent / "Dockerfile.base"),
        help="Dockerfile used with --build-base-image",
    )
    parser.add_argument(
        "--maven-settings",
        help=(
            "Host Maven settings.xml copied temporarily into Maven build containers; "
            "credentials are removed before the PR image is committed."
        ),
    )
    parser.add_argument(
        "--skyhanni-repo-archive",
        default=str(DEFAULT_SKYHANNI_REPO_ARCHIVE),
        help=(
            "Pinned SkyHanni-Repo ZIP for offline builds; its SHA-256 is checked "
            "before it is copied into SkyHanni build containers."
        ),
    )
    parser.add_argument(
        "--skyhanni-neu-repo-archive",
        default=str(DEFAULT_SKYHANNI_NEU_REPO_ARCHIVE),
        help=(
            "Pinned NotEnoughUpdates-Repo tarball for offline SkyHanni builds; "
            "its SHA-256 is checked before use."
        ),
    )
    parser.add_argument(
        "--gradle-stacktrace",
        action="store_true",
        help="Include Gradle stack traces in online and offline build/test failures.",
    )
    args = parser.parse_args()
    if args.skip_failed_images and not args.resume:
        parser.error("--skip-failed-images requires --resume")
    if args.retry_unverified and not args.resume:
        parser.error("--retry-unverified requires --resume")
    if args.retry_failed_images_only and not args.resume:
        parser.error("--retry-failed-images-only requires --resume")
    if args.retry_infrastructure_failures_only and not args.resume:
        parser.error("--retry-infrastructure-failures-only requires --resume")
    if args.retry_missing_images and not args.resume:
        parser.error("--retry-missing-images requires --resume")
    maven_settings = (
        Path(args.maven_settings).expanduser().resolve()
        if args.maven_settings
        else None
    )
    if maven_settings and not maven_settings.is_file():
        parser.error(f"Maven settings file not found: {maven_settings}")
    if maven_settings:
        missing_maven_environment = [
            name
            for name in ("GITHUB_USERNAME", "GITHUB_PACKAGES_TOKEN")
            if not os.environ.get(name)
        ]
        if missing_maven_environment:
            parser.error(
                "--maven-settings requires environment variables: "
                + ", ".join(missing_maven_environment)
            )

    json_path = Path(args.json_file).resolve()
    data = json.loads(json_path.read_text(encoding="utf-8"))
    selected_repos = set(args.repo or (repo["repo"] for repo in data["repos"]))
    skyhanni_repo_archive = None
    skyhanni_neu_repo_archive = None
    if SKYHANNI_REPO in selected_repos:
        try:
            skyhanni_repo_archive = validate_skyhanni_repo_archive(
                args.skyhanni_repo_archive
            )
            skyhanni_neu_repo_archive = validate_skyhanni_neu_repo_archive(
                args.skyhanni_neu_repo_archive
            )
        except (OSError, ValueError) as error:
            parser.error(str(error))
    if args.start_pr is not None:
        if not args.repo or len(set(args.repo)) != 1:
            parser.error("--start-pr requires exactly one --repo")
        start_repo = next(
            (repo for repo in data["repos"] if repo["repo"] == args.repo[0]),
            None,
        )
        if start_repo is None:
            parser.error(f"repository not found in dataset: {args.repo[0]}")
        start_index = next(
            (
                index
                for index, item in enumerate(start_repo["prs"])
                if item.get("pr_number") == args.start_pr
            ),
            None,
        )
        if start_index is None:
            parser.error(
                f"PR #{args.start_pr} was not found in repository {args.repo[0]}"
            )
    else:
        start_index = None
    try:
        check_docker_available()
    except RuntimeError as error:
        print(f"Docker preflight failed; no PRs were processed: {error}", file=sys.stderr)
        raise SystemExit(1) from None

    if args.build_base_image:
        base_build = run_command(
            [
                "docker",
                "build",
                "-t",
                args.base_image,
                "-f",
                str(Path(args.base_dockerfile).resolve()),
                str(Path(args.base_dockerfile).resolve().parent),
            ]
        )
        if base_build.returncode:
            print_result("Shared base image build", base_build)
            raise SystemExit(base_build.returncode)
    allowed_repos = set(args.repo or [])
    processed = 0
    skipped = 0
    skipped_failed_images = 0
    for repo in data["repos"]:
        repo_name = repo["repo"]
        if allowed_repos and repo_name not in allowed_repos:
            continue
        repo_prs = repo["prs"]
        if start_index is not None:
            repo_prs = repo_prs[start_index:]
        if args.resume:
            if args.retry_missing_images:
                repo_prs = [
                    item
                    for item in repo_prs
                    if item.get("tier") == "execution-verified"
                    and item.get("ghcr_result") is not True
                ]
            elif args.retry_infrastructure_failures_only:
                repo_prs = [
                    item
                    for item in repo_prs
                    if item.get("tier") != "execution-verified"
                    and is_retryable_infrastructure_failure(item)
                ]
                if not repo_prs:
                    continue
            else:
                completed_with_images = [
                    item
                    for item in repo_prs
                    if (
                        args.retry_failed_images_only
                        and item.get("image_build_result") == "passed"
                    )
                    or (
                        item.get("run_status") == "completed"
                        and item.get("image_build_result") == "passed"
                        and item.get("docker_tag")
                        and (
                            not args.retry_unverified
                            or item.get("tier") == "execution-verified"
                        )
                    )
                ]
                skipped += len(completed_with_images)
                completed_ids = {id(item) for item in completed_with_images}
                if args.skip_failed_images:
                    failed_images = [
                        item
                        for item in repo_prs
                        if item.get("run_status") == "completed"
                        and item.get("image_build_result") == "failed"
                    ]
                    skipped_failed_images += len(failed_images)
                    skipped += len(failed_images)
                    completed_ids.update(id(item) for item in failed_images)
                repo_prs = [
                    item for item in repo_prs if id(item) not in completed_ids
                ]
            if not repo_prs:
                continue
        repo_path = Path.home() / "local-repos" / repo_name
        if not repo_path.is_dir():
            repo_path.parent.mkdir(parents=True, exist_ok=True)
            clone = run_command(
                ["git", "clone", f"https://github.com/{repo_name}.git", str(repo_path)]
            )
            if clone.returncode:
                print_result(f"Clone {repo_name}", clone)
                for item in repo_prs:
                    item["image_build_result"] = "not_run"
                    item["build_result"] = "not_run"
                    item["test_result"] = "not_run"
                    item["docker_tag"] = None
                    item["tier"] = "unverified"
                    item["image_build_error"] = clone.stderr.strip()
                save_json(json_path, data)
                continue

        for item in repo_prs:
            if args.limit is not None and processed >= args.limit:
                save_json(json_path, data)
                print(f"Stopped after {processed} PRs as requested.")
                return
            print(f"Starting {repo_name} PR #{item['pr_number']}")
            try:
                process_pr(
                    repo_name,
                    str(repo_path),
                    item,
                    args.base_image,
                    maven_settings=maven_settings,
                    skyhanni_repo_archive=(
                        skyhanni_repo_archive
                        if repo_name == SKYHANNI_REPO
                        else None
                    ),
                    skyhanni_neu_repo_archive=(
                        skyhanni_neu_repo_archive
                        if repo_name == SKYHANNI_REPO
                        else None
                    ),
                    gradle_stacktrace=args.gradle_stacktrace,
                )
            except KeyboardInterrupt:
                item["run_status"] = "interrupted"
                item["execution_error"] = "Stopped by user before this PR finished."
                save_json(json_path, data)
                print(
                    f"Interrupted during {repo_name} PR #{item['pr_number']}; "
                    f"progress saved to {json_path}.",
                    file=sys.stderr,
                )
                raise SystemExit(130) from None
            item["run_status"] = "completed"
            save_json(json_path, data)
            processed += 1

    save_json(json_path, data)
    print(
        f"Image build and verification completed. Skipped {skipped} completed "
        f"PRs ({skipped - skipped_failed_images} with built images, "
        f"{skipped_failed_images} with failed image builds). "
        f"Results saved to {json_path}"
    )


if __name__ == "__main__":
    main()
