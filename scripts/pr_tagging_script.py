import argparse
from pathlib import Path

if __package__:
    from .pr_image_script import (
        DEFAULT_SKYHANNI_NEU_REPO_ARCHIVE,
        DEFAULT_SKYHANNI_REPO_ARCHIVE,
        SKYHANNI_REPO,
        formatted_repo_name,
        run_command,
        save_json,
        validate_skyhanni_neu_repo_archive,
        validate_skyhanni_repo_archive,
        is_retryable_infrastructure_failure,
    )
else:
    from pr_image_script import (
        DEFAULT_SKYHANNI_NEU_REPO_ARCHIVE,
        DEFAULT_SKYHANNI_REPO_ARCHIVE,
        SKYHANNI_REPO,
        formatted_repo_name,
        run_command,
        save_json,
        validate_skyhanni_neu_repo_archive,
        validate_skyhanni_repo_archive,
        is_retryable_infrastructure_failure,
    )

import json
import subprocess
import sys


def run_script(name, label, json_file):
    print(f"Starting {label}")
    try:
        subprocess.run(
            [sys.executable, str(Path(__file__).with_name(name)), json_file],
            check=True,
        )
    except KeyboardInterrupt:
        print("Pipeline interrupted; completed PR progress has been saved.")
        raise SystemExit(130) from None
    except subprocess.CalledProcessError as error:
        if error.returncode == 130:
            print("Pipeline interrupted; completed PR progress has been saved.")
            raise SystemExit(130) from None
        print(
            f"{label} failed with exit status {error.returncode}; stopping pipeline.",
            file=sys.stderr,
        )
        raise SystemExit(error.returncode) from None


def push_execution_verified_image(repo_name, item):
    warnings = []
    pr_number = item["pr_number"]
    local_tag = item.get("docker_tag")
    eligible = (
        item.get("tier") == "execution-verified"
        and item.get("image_build_result") == "passed"
        and item.get("build_result") == "passed"
        and item.get("test_result") == "passed"
        and item.get("dependency_cache_clean_result") == "passed"
        and item.get("offline_build_result") is True
        and item.get("offline_test_result") is True
        and item.get("git_result") is True
        and item.get("network_result") is True
        and isinstance(local_tag, str)
        and bool(local_tag)
        and isinstance(item.get("base_sha_full"), str)
        and len(item["base_sha_full"]) >= 7
    )

    if not eligible:
        warnings.append("GHCR push skipped: base-SHA execution verification did not pass")
        item["ghcr_result"] = False
        item["warnings"] = warnings
        return

    formatted_repo = repo_name.replace("/", "__").lower()
    remote_tag = f"ghcr.io/j2kmigbench/{formatted_repo}:pr-{pr_number}-base-{item['base_sha_full'][:7]}"
    try:
        subprocess.run(["docker", "tag", local_tag, remote_tag], check=True)
        subprocess.run(["docker", "push", remote_tag], check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        warnings.append(f"Docker tagging or GHCR push failed: {error}")
        item["ghcr_result"] = False
    else:
        item["ghcr_tag"] = remote_tag
        item["ghcr_result"] = True

    item["warnings"] = warnings


def tag_main():
    if len(sys.argv) != 2:
        raise SystemExit(f"Usage: {sys.argv[0]} <json-file>")

    json_file = sys.argv[1]
    with open(json_file, "r", encoding="utf-8") as file:
        data = json.load(file)

    for repo in data["repos"]:
        for item in repo["prs"]:
            push_execution_verified_image(repo["repo"], item)

    with open(json_file, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)

    print(f"Image tagging and GHCR push completed. Results in {json_file}.")


def pipeline_main(json_file):
    stages = (
        ("pr_image_script.py", "image building script"),
        ("pr_tagging_script.py", "image tagging script"),
        ("jacoco_debug_script.py", "JaCoCo collection script"),
        ("jacoco_parser_script.py", "JaCoCo parsing script"),
    )
    for script_name, label in stages:
        run_script(script_name, label, json_file)
    print(f"All scripts complete. Results in {json_file}")


def run_image_builder(
    json_path,
    repo_name,
    base_image,
    build_base_image,
    base_dockerfile,
    retry_failed_images=False,
    retry_image_build_failures_only=False,
    retry_infrastructure_failures_only=False,
    retry_missing_images=False,
    maven_settings=None,
    skyhanni_repo_archive=None,
    skyhanni_neu_repo_archive=None,
    rebuild_all_images=False,
    start_pr=None,
    limit=None,
    gradle_stacktrace=False,
):
    command = [
        sys.executable,
        str(Path(__file__).with_name("pr_image_script.py")),
        str(json_path),
        "--repo",
        repo_name,
        "--base-image",
        base_image,
    ]
    if not rebuild_all_images:
        command.append("--resume")
        if retry_failed_images:
            command.append("--retry-unverified")
        elif retry_infrastructure_failures_only:
            command.append("--retry-infrastructure-failures-only")
        elif retry_missing_images:
            command.append("--retry-missing-images")
        elif retry_image_build_failures_only:
            command.append("--retry-failed-images-only")
        else:
            command.append("--skip-failed-images")
    if start_pr is not None:
        command.extend(["--start-pr", str(start_pr)])
    if limit is not None:
        command.extend(["--limit", str(limit)])
    if gradle_stacktrace:
        command.append("--gradle-stacktrace")
    if maven_settings is not None:
        command.extend(["--maven-settings", str(maven_settings)])
    if repo_name == SKYHANNI_REPO and skyhanni_repo_archive is not None:
        command.extend(["--skyhanni-repo-archive", str(skyhanni_repo_archive)])
    if repo_name == SKYHANNI_REPO and skyhanni_neu_repo_archive is not None:
        command.extend(
            ["--skyhanni-neu-repo-archive", str(skyhanni_neu_repo_archive)]
        )
    if build_base_image:
        command.extend(
            ["--build-base-image", "--base-dockerfile", str(base_dockerfile)]
        )
    result = subprocess.run(command, check=False)
    if result.returncode:
        raise RuntimeError(
            f"Image builder failed for {repo_name} with exit code {result.returncode}"
        )


def _remove_docker_resource(command, missing_message):
    result = run_command(command)
    if result.returncode:
        details = "\n".join(
            part.strip() for part in (result.stderr, result.stdout) if part.strip()
        )
        if missing_message in details:
            return
        raise RuntimeError(
            f"`{' '.join(command)}` failed with exit code {result.returncode}: "
            f"{details or 'no error output'}"
        )


def cleanup_local_images(repo_name, prs, preserve_pr_numbers=()):
    repo_slug = formatted_repo_name(repo_name)
    local_prefix = f"{repo_slug}:pr-"
    remote_prefix = f"ghcr.io/j2kmigbench/{repo_slug}:pr-"
    preserved = set(preserve_pr_numbers)

    for item in prs:
        pr_number = item.get("pr_number")
        local_tag = item.get("docker_tag")
        if pr_number in preserved or not isinstance(local_tag, str):
            continue
        if not local_tag.startswith(local_prefix):
            raise RuntimeError(
                f"Refusing to remove unexpected local image tag for {repo_name} "
                f"PR #{pr_number}: {local_tag}"
            )

        base_sha = item.get("base_sha_full")
        if isinstance(base_sha, str) and len(base_sha) >= 7:
            container_name = (
                f"pr-base-{repo_slug}-{pr_number}-{base_sha[:7]}"
            )
            _remove_docker_resource(
                ["docker", "rm", "--force", container_name], "No such container"
            )

        tags = [local_tag, f"{local_tag}-bootstrap"]
        remote_tag = item.get("ghcr_tag")
        if remote_tag:
            if not isinstance(remote_tag, str) or not remote_tag.startswith(
                remote_prefix
            ):
                raise RuntimeError(
                    f"Refusing to remove unexpected GHCR image tag for "
                    f"{repo_name} PR #{pr_number}: {remote_tag}"
                )
            tags.append(remote_tag)

        for tag in dict.fromkeys(tags):
            _remove_docker_resource(
                ["docker", "image", "rm", tag], "No such image"
            )
        item["local_image_removed"] = True


def _should_image_builder_skip(
    item,
    retry_failed_images,
    rebuild_all_images,
    retry_image_build_failures_only=False,
    retry_infrastructure_failures_only=False,
    retry_missing_images=False,
):
    if rebuild_all_images:
        return False
    if retry_infrastructure_failures_only:
        return (
            item.get("tier") == "execution-verified"
            or not is_retryable_infrastructure_failure(item)
        )
    if retry_missing_images:
        return (
            item.get("tier") != "execution-verified"
            or item.get("ghcr_result") is True
        )
    image_build_passed = item.get("image_build_result") == "passed"
    completed_with_image = (
        item.get("run_status") == "completed"
        and image_build_passed
        and item.get("docker_tag")
    )
    if completed_with_image and (
        retry_image_build_failures_only
        or not retry_failed_images
        or item.get("tier") == "execution-verified"
    ):
        return True
    if retry_image_build_failures_only and image_build_passed:
        return True
    return (
        not retry_failed_images
        and not retry_image_build_failures_only
        and item.get("run_status") == "completed"
        and item.get("image_build_result") == "failed"
    )


def _finish_repository_batch(
    json_path, data, repo_name, selected_prs, prune_build_cache
):
    for item in selected_prs:
        if item.get("docker_tag") and not item.get("local_image_removed"):
            push_execution_verified_image(repo_name, item)
    save_json(json_path, data)

    failed_push_prs = {
        item["pr_number"]
        for item in selected_prs
        if item.get("tier") == "execution-verified"
        and item.get("ghcr_result") is not True
    }
    cleanup_local_images(repo_name, selected_prs, failed_push_prs)
    save_json(json_path, data)

    if prune_build_cache:
        result = run_command(["docker", "builder", "prune", "--all", "--force"])
        if result.returncode:
            details = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(
                "Docker build-cache cleanup failed; stopping before the next "
                f"repository: {details or f'exit code {result.returncode}'}"
            )

    if failed_push_prs:
        failed = ", ".join(f"#{pr}" for pr in sorted(failed_push_prs))
        raise RuntimeError(
            f"GHCR publishing did not succeed for {repo_name} PRs {failed}; "
            "local images were retained where available and processing stopped."
        )


def process_repository(
    json_path,
    repo_name,
    base_image="j2kbench-base:latest",
    build_base_image=False,
    base_dockerfile=None,
    prune_build_cache=False,
    retry_failed_images=False,
    retry_image_build_failures_only=False,
    retry_infrastructure_failures_only=False,
    retry_missing_images=False,
    maven_settings=None,
    skyhanni_repo_archive=None,
    skyhanni_neu_repo_archive=None,
    rebuild_all_images=False,
    start_pr=None,
    batch_size=None,
    gradle_stacktrace=False,
):
    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    repo = next((entry for entry in data["repos"] if entry["repo"] == repo_name), None)
    if repo is None:
        raise RuntimeError(f"Repository disappeared from dataset: {repo_name}")
    selected_prs = repo["prs"]
    selected_start_index = 0
    if start_pr is not None:
        start_index = next(
            (
                index
                for index, item in enumerate(selected_prs)
                if item.get("pr_number") == start_pr
            ),
            None,
        )
        if start_index is None:
            raise RuntimeError(f"PR #{start_pr} not found in repository {repo_name}")
        selected_start_index = start_index
        selected_prs = selected_prs[start_index:]

    if batch_size is None:
        candidate_numbers = {
            item.get("pr_number")
            for item in selected_prs
            if not _should_image_builder_skip(
                item,
                retry_failed_images,
                rebuild_all_images,
                retry_image_build_failures_only,
                retry_infrastructure_failures_only,
                retry_missing_images,
            )
        }
        run_image_builder(
            json_path,
            repo_name,
            base_image,
            build_base_image,
            base_dockerfile,
            retry_failed_images=retry_failed_images,
            retry_image_build_failures_only=retry_image_build_failures_only,
            retry_infrastructure_failures_only=retry_infrastructure_failures_only,
            retry_missing_images=retry_missing_images,
            maven_settings=maven_settings,
            skyhanni_repo_archive=skyhanni_repo_archive,
            skyhanni_neu_repo_archive=skyhanni_neu_repo_archive,
            rebuild_all_images=rebuild_all_images,
            start_pr=start_pr,
            gradle_stacktrace=gradle_stacktrace,
        )
        refreshed_data = json.loads(Path(json_path).read_text(encoding="utf-8"))
        refreshed_repo = next(
            entry for entry in refreshed_data["repos"] if entry["repo"] == repo_name
        )
        refreshed_prs = [
            item
            for item in refreshed_repo["prs"][selected_start_index:]
            if item.get("pr_number") in candidate_numbers
        ]
        _finish_repository_batch(
            json_path, refreshed_data, repo_name, refreshed_prs, prune_build_cache
        )
    else:
        candidates = [
            index
            for index, item in enumerate(selected_prs)
            if not _should_image_builder_skip(
                item,
                retry_failed_images,
                rebuild_all_images,
                retry_image_build_failures_only,
                retry_infrastructure_failures_only,
                retry_missing_images,
            )
        ]
        batches = [
            candidates[index : index + batch_size]
            for index in range(0, len(candidates), batch_size)
        ]
        base_image_pending = build_base_image

        if not batches and build_base_image:
            run_image_builder(
                json_path,
                repo_name,
                base_image,
                True,
                base_dockerfile,
                retry_failed_images=retry_failed_images,
                retry_image_build_failures_only=retry_image_build_failures_only,
                retry_infrastructure_failures_only=retry_infrastructure_failures_only,
                retry_missing_images=retry_missing_images,
                maven_settings=maven_settings,
                skyhanni_repo_archive=skyhanni_repo_archive,
                skyhanni_neu_repo_archive=skyhanni_neu_repo_archive,
                rebuild_all_images=rebuild_all_images,
                start_pr=selected_prs[0]["pr_number"] if selected_prs else None,
                limit=0,
                gradle_stacktrace=gradle_stacktrace,
            )
            base_image_pending = False

        for batch in batches:
            batch_start_pr = selected_prs[batch[0]]["pr_number"]
            run_image_builder(
                json_path,
                repo_name,
                base_image,
                base_image_pending,
                base_dockerfile,
                retry_failed_images=retry_failed_images,
                retry_image_build_failures_only=retry_image_build_failures_only,
                retry_infrastructure_failures_only=retry_infrastructure_failures_only,
                retry_missing_images=retry_missing_images,
                maven_settings=maven_settings,
                skyhanni_repo_archive=skyhanni_repo_archive,
                skyhanni_neu_repo_archive=skyhanni_neu_repo_archive,
                rebuild_all_images=rebuild_all_images,
                start_pr=batch_start_pr,
                limit=len(batch),
                gradle_stacktrace=gradle_stacktrace,
            )
            base_image_pending = False
            refreshed_data = json.loads(Path(json_path).read_text(encoding="utf-8"))
            refreshed_repo = next(
                (
                    entry
                    for entry in refreshed_data["repos"]
                    if entry["repo"] == repo_name
                ),
                None,
            )
            if refreshed_repo is None:
                raise RuntimeError(f"Repository disappeared from dataset: {repo_name}")
            batch_prs = [
                refreshed_repo["prs"][selected_start_index + index]
                for index in batch
            ]
            _finish_repository_batch(
                json_path,
                refreshed_data,
                repo_name,
                batch_prs,
                prune_build_cache,
            )
    print(f"Finished {repo_name}: eligible images pushed and local PR images removed.")


def batch_main():
    parser = argparse.ArgumentParser(
        description=(
            "Build one repository at a time, push execution-verified images, "
            "then remove that repository's local PR images."
        )
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
        "--batch-size",
        type=int,
        help=(
            "Build at most this many PRs at a time per repository, then push "
            "eligible images and clean local PR images before the next batch."
        ),
    )
    parser.add_argument(
        "--base-image",
        default="j2kbench-base:latest",
        help="Shared build image containing the required JDK/Android SDK",
    )
    parser.add_argument(
        "--build-base-image",
        action="store_true",
        help="Build the shared base image before processing the first repository",
    )
    parser.add_argument(
        "--base-dockerfile",
        default=str(Path(__file__).resolve().parent.parent / "Dockerfile.base"),
        help="Dockerfile used with --build-base-image",
    )
    parser.add_argument(
        "--maven-settings",
        help=(
            "Host Maven settings.xml for private package access; settings are "
            "removed from build containers before PR images are committed."
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
        "--prune-build-cache",
        action="store_true",
        help=(
            "After each repository or PR batch, remove all unused Docker builder cache. "
            "This affects other Docker projects and can make later builds slower."
        ),
    )
    retry_options = parser.add_mutually_exclusive_group()
    retry_options.add_argument(
        "--retry-failed-images",
        action="store_true",
        help=(
            "Retry previously failed or unverified PR image builds for the "
            "selected repositories instead of leaving them aside."
        ),
    )
    retry_options.add_argument(
        "--retry-image-build-failures-only",
        action="store_true",
        help=(
            "Retry completed PRs whose image build failed, while reusing all "
            "PRs whose image build already passed, including unverified images."
        ),
    )
    retry_options.add_argument(
        "--retry-infrastructure-failures-only",
        action="store_true",
        help=(
            "Retry only PRs with recorded network/dependency, Java, Android "
            "SDK/AGP, or repo/Docker infrastructure errors."
        ),
    )
    retry_options.add_argument(
        "--retry-missing-images",
        action="store_true",
        help=(
            "Rebuild execution-verified PR images that were not successfully "
            "published to GHCR."
        ),
    )
    retry_options.add_argument(
        "--rebuild-all-images",
        action="store_true",
        help=(
            "Rebuild every PR image for the selected repositories, including "
            "ones already completed successfully."
        ),
    )
    parser.add_argument(
        "--gradle-stacktrace",
        action="store_true",
        help="Include Gradle stack traces in online and offline build/test failures.",
    )
    args = parser.parse_args()
    if args.batch_size is not None and args.batch_size < 1:
        parser.error("--batch-size must be at least 1")
    maven_settings = (
        Path(args.maven_settings).expanduser().resolve()
        if args.maven_settings
        else None
    )
    if maven_settings and not maven_settings.is_file():
        parser.error(f"Maven settings file not found: {maven_settings}")
    json_path = Path(args.json_file).resolve()
    data = json.loads(json_path.read_text(encoding="utf-8"))
    available_repos = {repo["repo"] for repo in data["repos"]}
    selected_repos = args.repo or [repo["repo"] for repo in data["repos"]]
    unknown_repos = sorted(set(selected_repos) - available_repos)
    if unknown_repos:
        parser.error(f"repository not found in dataset: {', '.join(unknown_repos)}")
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
        selected_repo = next(
            repo for repo in data["repos"] if repo["repo"] == args.repo[0]
        )
        if not any(item.get("pr_number") == args.start_pr for item in selected_repo["prs"]):
            parser.error(
                f"PR #{args.start_pr} was not found in repository {args.repo[0]}"
            )

    for index, repo_name in enumerate(selected_repos):
        try:
            process_repository(
                json_path,
                repo_name,
                base_image=args.base_image,
                build_base_image=args.build_base_image and index == 0,
                base_dockerfile=args.base_dockerfile,
                prune_build_cache=args.prune_build_cache,
                retry_failed_images=args.retry_failed_images,
                retry_image_build_failures_only=args.retry_image_build_failures_only,
                retry_infrastructure_failures_only=args.retry_infrastructure_failures_only,
                retry_missing_images=args.retry_missing_images,
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
                rebuild_all_images=args.rebuild_all_images,
                start_pr=args.start_pr,
                batch_size=args.batch_size,
                gradle_stacktrace=args.gradle_stacktrace,
            )
        except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
            print(f"Stopped after {repo_name}: {error}", file=sys.stderr)
            raise SystemExit(1) from None


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--pipeline":
        pipeline_main(sys.argv[2])
    elif len(sys.argv) == 2:
        tag_main()
    else:
        batch_main()


if __name__ == "__main__":
    main()
