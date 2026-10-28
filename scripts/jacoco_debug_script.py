import argparse
import base64
import json
import re
import shutil
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

if __package__:
    from .pr_image_script import container_workdir, run_command, save_json
else:
    from pr_image_script import container_workdir, run_command, save_json


REPORT_COLLECTOR = (
    "from pathlib import Path\n"
    "import shutil\n"
    "import xml.etree.ElementTree as ET\n"
    "root = Path('/workspace/repo')\n"
    "destination = Path('/coverage-output')\n"
    "candidates = []\n"
    "for report in root.rglob('*'):\n"
    "    if not report.is_file() or report.suffix.lower() != '.xml':\n"
    "        continue\n"
    "    parts = report.parts\n"
    "    if not (('build' in parts or 'target' in parts) and 'reports' in parts):\n"
    "        continue\n"
    "    try:\n"
    "        if ET.parse(report).getroot().tag.rsplit('}', 1)[-1] == 'report':\n"
    "            candidates.append(report)\n"
    "    except (OSError, ET.ParseError):\n"
    "        pass\n"
    "if candidates:\n"
    "    candidates.sort(key=lambda path: ("
    "        'jacoco' not in path.name.lower(),"
    "        'jacoco' not in str(path).lower(),"
    "        len(path.parts),"
    "        str(path),"
    "    ))\n"
    "    shutil.copy2(candidates[0], destination / 'jacoco.xml')\n"
)

GRADLE_OFFLINE_INIT = """
gradle.projectsEvaluated {
    allprojects { project ->
        project.tasks.withType(org.gradle.testing.jacoco.tasks.JacocoReport).configureEach {
            reports.xml.required = true
            reports.html.required = false
        }
        project.tasks.configureEach { task ->
            def retained = task.dependsOn.findAll { dependency ->
                def dependencyName = dependency.hasProperty("name")
                    ? dependency.name
                    : dependency.toString()
                !dependencyName.toLowerCase().contains("androidtest")
            }
            task.setDependsOn(retained)
        }
    }
}
""".strip()

MEDTIMER_REPO = "Futsch1/medTimer"
MEDTIMER_GRADLE_TASKS = (
    "app:JacocoDebugCodeCoverage",
    "app:jacocoFullDebugCodeCoverage",
    "app:createDebugUnitTestCoverageReport",
    "app:createFullDebugUnitTestCoverageReport",
    "app:testDebugUnitTest",
)


def command_error(result):
    return "\n".join(part for part in (result.stderr.strip(), result.stdout.strip()) if part)


def ensure_image_available(item):
    local_tag = item.get("docker_tag")
    remote_tag = item.get("ghcr_tag")
    if not local_tag:
        return None, None

    present = run_command(["docker", "image", "inspect", local_tag])
    if present.returncode == 0:
        return local_tag, None
    if not remote_tag:
        return None, "Local image is unavailable and no GHCR tag is recorded."

    pulled = run_command(["docker", "pull", remote_tag])
    if pulled.returncode:
        return None, command_error(pulled)
    retagged = run_command(["docker", "tag", remote_tag, local_tag])
    if retagged.returncode:
        return None, command_error(retagged)
    return local_tag, None


def remove_coverage_images(item):
    tags = [item.get("docker_tag"), item.get("ghcr_tag")]
    for tag in dict.fromkeys(tag for tag in tags if tag):
        result = run_command(["docker", "image", "rm", tag])
        if result.returncode and "No such image" not in command_error(result):
            raise RuntimeError(command_error(result))


def gradle_report_tasks(container_name, workdir, gradle_command):
    result = run_command(
        [
            "docker",
            "exec",
            "-w",
            workdir,
            container_name,
            gradle_command,
            "--no-daemon",
            "--offline",
            "tasks",
            "--all",
        ]
    )
    if result.returncode:
        return [], result

    tasks = set()
    for line in result.stdout.splitlines():
        match = re.match(r"^\s*([A-Za-z][A-Za-z0-9_:-]*)\s+-", line)
        if match and re.search(
            r"(?:jacoco|kover|cobertura|coverage|test.*report|report.*test)",
            match.group(1),
            re.IGNORECASE,
        ):
            tasks.add(match.group(1))
    preferred = sorted(
        task
        for task in tasks
        if "jacoco" in task.lower() and "create" not in task.lower()
    )
    fallback = sorted(
        task
        for task in tasks
        if re.search(r"(?:test.*unit|unit.*test|testDebug)", task, re.IGNORECASE)
    )
    return preferred or fallback or sorted(tasks), result


def classify_coverage_failure(kind, message):
    text = (message or "").lower()
    if kind == "image":
        return "image_unavailable"
    if "could not resolve" in text or "could not download" in text or "401 unauthorized" in text:
        return "dependency_unavailable"
    if kind == "discovery":
        return "task_discovery_failed"
    if kind == "execution":
        return "task_execution_failed"
    if kind == "report":
        return "report_not_found"
    return "coverage_failed"


def run_medtimer_coverage(container_name, output_dir):
    last_result = None
    for task in MEDTIMER_GRADLE_TASKS:
        result = run_command(
            [
                "docker",
                "exec",
                "-w",
                "/workspace/repo",
                container_name,
                "./gradlew",
                task,
                "--offline",
                "--gradle-user-home",
                "/root/.gradle",
                "-x",
                "connectedDebugAndroidTest",
                "--no-build-cache",
            ]
        )
        last_result = result
        if result.returncode:
            continue

        copied = run_command(
            [
                "docker",
                "cp",
                f"{container_name}:/workspace/repo/app/build/reports/.",
                str(output_dir),
            ]
        )
        if copied.returncode:
            return task, copied
        return task, result
    return None, last_result


def collect_coverage(repo, item, output_root, delete_image_after=False):
    for key in (
        "coverage_error",
        "coverage_warning",
        "coverage_failure_type",
        "coverage_xml_files",
    ):
        item.pop(key, None)
    item["coverage_status"] = "running"
    tag, image_error = ensure_image_available(item)
    build_system = item.get("build_system")
    if image_error:
        item["coverage_status"] = "failed"
        item["coverage_error"] = image_error
        item["coverage_failure_type"] = classify_coverage_failure("image", image_error)
        return
    if not tag or build_system not in {"gradle", "maven"}:
        item["coverage_status"] = "not_run"
        item["coverage_warning"] = (
            image_error
            or "No built image or supported build-system metadata."
        )
        return

    repo_slug = repo.replace("/", "__").lower()
    output_dir = output_root / repo_slug / f"pr_{item['pr_number']}"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    item["coverage_report_path"] = output_dir.as_posix()
    container_name = (
        f"jacoco-{repo_slug}-{item['pr_number']}-{uuid.uuid4().hex[:8]}"
    )
    workdir = container_workdir(item.get("build_project_dir", "."))

    create = run_command(
        [
            "docker",
            "create",
            "--network",
            "none",
            "--mount",
            f"type=bind,source={output_dir.resolve()},target=/coverage-output",
            "--name",
            container_name,
            tag,
            "tail",
            "-f",
            "/dev/null",
        ]
    )
    if create.returncode:
        item["coverage_status"] = "failed"
        item["coverage_error"] = command_error(create)
        print(f"Could not create JaCoCo container for {repo} PR #{item['pr_number']}: {create.stderr}", file=sys.stderr)
        return

    try:
        start = run_command(["docker", "start", container_name])
        if start.returncode:
            item["coverage_status"] = "failed"
            item["coverage_error"] = command_error(start)
            print(f"Could not start JaCoCo container for {repo} PR #{item['pr_number']}: {start.stderr}", file=sys.stderr)
            return

        if repo == MEDTIMER_REPO:
            task, result = run_medtimer_coverage(container_name, output_dir)
            item["coverage_tasks"] = [task] if task else list(MEDTIMER_GRADLE_TASKS)
            if task is None:
                item["coverage_status"] = "failed"
                item["coverage_error"] = command_error(result)
                item["coverage_failure_type"] = classify_coverage_failure(
                    "execution", item["coverage_error"]
                )
                return
        elif build_system == "gradle":
            gradle_command = (
                "./gradlew" if item.get("gradle_wrapper", True) else "gradle"
            )
            tasks, task_result = gradle_report_tasks(
                container_name, workdir, gradle_command
            )
            if task_result.returncode:
                item["coverage_status"] = "failed"
                item["coverage_error"] = command_error(task_result)
                item["coverage_failure_type"] = classify_coverage_failure(
                    "discovery", item["coverage_error"]
                )
                return
            if not tasks:
                item["coverage_status"] = "unsupported"
                item["coverage_warning"] = "No JaCoCo or coverage report task is configured."
                item["coverage_failure_type"] = "no_coverage_task"
                return
            command = [
                gradle_command,
                "--offline",
                "--no-daemon",
                "--no-build-cache",
                "--rerun-tasks",
                "--init-script",
                "/tmp/jacoco-offline.init.gradle",
                *tasks,
            ]
            init_script = run_command(
                [
                    "docker",
                    "exec",
                    container_name,
                    "python3",
                    "-c",
                    (
                        "import base64; from pathlib import Path; "
                        "Path('/tmp/jacoco-offline.init.gradle').write_bytes("
                        "base64.b64decode("
                        f"{base64.b64encode(GRADLE_OFFLINE_INIT.encode()).decode()!r}))"
                    ),
                ],
            )
            if init_script.returncode:
                item["coverage_status"] = "failed"
                item["coverage_error"] = command_error(init_script)
                item["coverage_failure_type"] = classify_coverage_failure(
                    "execution", item["coverage_error"]
                )
                return
        else:
            wrapper = "./mvnw" if item.get("maven_wrapper") else "mvn"
            command = [
                wrapper,
                "--batch-mode",
                "--no-transfer-progress",
                "--offline",
                "test",
                "jacoco:report",
            ]
            tasks = ["test", "jacoco:report"]

        if repo != MEDTIMER_REPO:
            result = run_command(
                ["docker", "exec", "-w", workdir, container_name, *command]
            )
            item["coverage_tasks"] = tasks
            if result.returncode:
                item["coverage_status"] = "failed"
                item["coverage_error"] = (result.stderr or result.stdout).strip()
                item["coverage_failure_type"] = classify_coverage_failure(
                    "execution", item["coverage_error"]
                )
                print(f"JaCoCo execution failed for {repo} PR #{item['pr_number']}.", file=sys.stderr)
                return

            copied = run_command(
                [
                    "docker",
                    "exec",
                    container_name,
                    "python3",
                    "-c",
                    REPORT_COLLECTOR,
                ]
            )
            if copied.returncode:
                item["coverage_status"] = "failed"
                item["coverage_error"] = command_error(copied)
                item["coverage_failure_type"] = classify_coverage_failure(
                    "report", item["coverage_error"]
                )
                return

        report_files = list(output_dir.rglob("*.xml"))
        jacoco_reports = []
        for report in report_files:
            try:
                root = ET.parse(report).getroot()
                if root.tag.rsplit("}", 1)[-1] == "report":
                    jacoco_reports.append(report)
            except (OSError, ET.ParseError):
                continue
        if not jacoco_reports:
            item["coverage_status"] = "not_found"
            item["coverage_warning"] = "Coverage tasks completed but produced no JaCoCo XML report."
            item["coverage_failure_type"] = "report_not_found"
            return

        item["coverage_status"] = "passed"
        item.pop("coverage_failure_type", None)
        selected_report = output_dir / "jacoco.xml"
        for report in jacoco_reports:
            if report != selected_report:
                report.unlink(missing_ok=True)
        if selected_report not in jacoco_reports:
            shutil.copy2(jacoco_reports[0], selected_report)
            for report in jacoco_reports:
                if report != selected_report:
                    report.unlink(missing_ok=True)
        item["coverage_xml_files"] = [
            selected_report.relative_to(output_root).as_posix()
        ]
    finally:
        run_command(["docker", "rm", "--force", container_name])
        if delete_image_after and tag:
            try:
                remove_coverage_images(item)
            except RuntimeError as error:
                item["coverage_cleanup_error"] = str(error)
                print(
                    f"Could not remove JaCoCo image for {repo} PR "
                    f"#{item['pr_number']}: {error}",
                    file=sys.stderr,
                )


def main():
    parser = argparse.ArgumentParser(
        description="Run configured JaCoCo coverage tasks in each base-state image."
    )
    parser.add_argument("json_file")
    parser.add_argument("--repo", action="append", help="Limit to owner/name; repeatable")
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument(
        "--delete-image-after",
        action="store_true",
        help="Remove local and GHCR-tagged image copies after each PR.",
    )
    args = parser.parse_args()

    json_path = Path(args.json_file).resolve()
    data = json.loads(json_path.read_text(encoding="utf-8"))
    output_root = Path(args.reports_dir).resolve()
    allowed_repos = set(args.repo or [])
    for repo in data["repos"]:
        if allowed_repos and repo["repo"] not in allowed_repos:
            continue
        for item in repo["prs"]:
            if item.get("tier") != "execution-verified":
                item["coverage_status"] = "not_run"
                item["coverage_warning"] = "Base image is not execution-verified."
                item.pop("coverage_error", None)
                item.pop("coverage_xml_files", None)
                continue
            print(f"Collecting JaCoCo for {repo['repo']} PR #{item['pr_number']}")
            collect_coverage(
                repo["repo"],
                item,
                output_root,
                delete_image_after=args.delete_image_after,
            )
            save_json(json_path, data)

    save_json(json_path, data)
    print(f"JaCoCo collection finished. Updated {json_path}")


if __name__ == "__main__":
    main()
