import argparse
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd

if __package__:
    from .pr_image_script import ensure_commit_available, run_command, save_json
else:
    from pr_image_script import ensure_commit_available, run_command, save_json


SOURCE_PATTERN = re.compile(r"(?:^|/)src/main/(?:java|kotlin)/(.+\.(?:java|kt))$")
SOURCE_EXTENSIONS = {".java", ".kt"}


def source_key(path):
    match = SOURCE_PATTERN.search(path.replace("\\", "/"))
    return match.group(1) if match else None


def changed_sources(repo_path, base_sha, head_sha):
    results = {}

    def collect_statuses(output):
        for line in output.splitlines():
            parts = line.split("\t")
            if not parts:
                continue
            status = parts[0]
            paths = parts[1:]
            if status.startswith("R") and len(paths) == 2:
                paths = paths
            elif len(paths) != 1:
                continue
            for path in paths:
                if Path(path).suffix.lower() in SOURCE_EXTENSIONS:
                    results.setdefault(path, set()).add(status)

    diff = run_command(
        ["git", "diff", "--name-status", "--find-renames", base_sha, head_sha],
        cwd=repo_path,
    )
    if diff.returncode:
        raise RuntimeError(diff.stderr.strip() or "Could not diff PR base and head")
    collect_statuses(diff.stdout)

    commit_history = run_command(
        [
            "git",
            "log",
            "--format=",
            "--name-status",
            "--find-renames",
            "--first-parent",
            f"{base_sha}..{head_sha}",
        ],
        cwd=repo_path,
    )
    if commit_history.returncode:
        raise RuntimeError(
            commit_history.stderr.strip()
            or "Could not enumerate changes across PR commits"
        )
    collect_statuses(commit_history.stdout)

    base_tree = run_command(
        ["git", "ls-tree", "-r", "--name-only", base_sha],
        cwd=repo_path,
    )
    if base_tree.returncode:
        raise RuntimeError(base_tree.stderr.strip() or "Could not list base-SHA files")
    base_paths = set(base_tree.stdout.splitlines())

    return [
        {
            "file_path": path,
            "base_coverage_path": path if path in base_paths else None,
            "change_type": ",".join(sorted(statuses)),
        }
        for path, statuses in sorted(results.items())
    ]


def parse_jacoco_report(xml_path):
    tree = ET.parse(xml_path)
    root = tree.getroot()
    if root.tag.rsplit("}", 1)[-1] != "report":
        raise ValueError(f"{xml_path} is not a JaCoCo report")

    source_metrics = {}
    for package in root:
        if package.tag.rsplit("}", 1)[-1] != "package":
            continue
        package_name = package.get("name", "").strip("/")
        for source_file in package:
            if source_file.tag.rsplit("}", 1)[-1] != "sourcefile":
                continue
            source_name = source_file.get("name")
            if not source_name:
                continue
            key = f"{package_name}/{source_name}" if package_name else source_name
            metrics = {"lines_covered": 0, "lines_missed": 0, "branches_covered": 0, "branches_missed": 0}
            for counter in source_file:
                if counter.tag.rsplit("}", 1)[-1] != "counter":
                    continue
                covered = int(counter.get("covered", "0"))
                missed = int(counter.get("missed", "0"))
                if counter.get("type") == "LINE":
                    metrics["lines_covered"] += covered
                    metrics["lines_missed"] += missed
                elif counter.get("type") == "BRANCH":
                    metrics["branches_covered"] += covered
                    metrics["branches_missed"] += missed
            source_metrics[key] = metrics
    return source_metrics


def build_test_lookup(repo_path, base_sha):
    result = run_command(
        ["git", "ls-tree", "-r", "--name-only", base_sha],
        cwd=repo_path,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Could not list base-SHA files")
    lookup = {}
    for path in result.stdout.splitlines():
        normalized_path = path.replace("\\", "/")
        if "/src/test/" not in normalized_path:
            continue
        if Path(normalized_path).suffix.lower() not in SOURCE_EXTENSIONS:
            continue
        lookup.setdefault(Path(normalized_path).name, normalized_path)
    return lookup


def coverage_for_source(base_path, source_metrics):
    if base_path is None:
        return {
            "coverage_status": "not_in_base",
            "lines_covered": None,
            "lines_missed": None,
            "line_coverage_percentage": None,
            "branches_covered": None,
            "branches_missed": None,
            "branch_coverage_percentage": None,
        }

    key = source_key(base_path)
    matched_key = next(
        (
            candidate
            for candidate in source_metrics
            if key is not None and (candidate == key or candidate.endswith(f"/{key}"))
        ),
        None,
    )
    if matched_key is None:
        return {
            "coverage_status": "not_in_report",
            "lines_covered": None,
            "lines_missed": None,
            "line_coverage_percentage": None,
            "branches_covered": None,
            "branches_missed": None,
            "branch_coverage_percentage": None,
        }

    metrics = source_metrics[matched_key]
    line_total = metrics["lines_covered"] + metrics["lines_missed"]
    branch_total = metrics["branches_covered"] + metrics["branches_missed"]
    return {
        "coverage_status": "measured",
        **metrics,
        "line_coverage_percentage": (
            round(metrics["lines_covered"] * 100 / line_total, 2) if line_total else None
        ),
        "branch_coverage_percentage": (
            round(metrics["branches_covered"] * 100 / branch_total, 2)
            if branch_total
            else None
        ),
    }


def reports_for_item(item, repo_path, reports_root):
    if not item.get("base_sha_full") or not item.get("head_sha_full"):
        raise RuntimeError("Full base/head SHAs are missing")
    try:
        ensure_commit_available(repo_path, item["head_sha_full"])
    except RuntimeError:
        fetch_pr_head = run_command(
            [
                "git",
                "fetch",
                "--no-tags",
                "origin",
                f"refs/pull/{item['pr_number']}/head",
            ],
            cwd=repo_path,
        )
        if fetch_pr_head.returncode:
            raise RuntimeError(
                fetch_pr_head.stderr.strip()
                or f"Could not fetch PR #{item['pr_number']} head commit"
            )
        ensure_commit_available(repo_path, item["head_sha_full"])

    files = changed_sources(repo_path, item["base_sha_full"], item["head_sha_full"])
    test_lookup = build_test_lookup(repo_path, item["base_sha_full"])
    report_paths = [
        reports_root / relative
        for relative in item.get("coverage_xml_files", [])
        if relative.endswith(".xml")
    ]
    if item.get("coverage_status") != "passed" or not report_paths:
        source_metrics = {}
        report_status = item.get("coverage_status", "not_run")
    else:
        source_metrics = {}
        for report_path in report_paths:
            source_metrics.update(parse_jacoco_report(report_path))
        report_status = "passed"

    file_results = []
    for file_info in files:
        metrics = coverage_for_source(
            file_info["base_coverage_path"],
            source_metrics,
        )
        source_name = Path(file_info["file_path"]).stem
        test_file = test_lookup.get(f"{source_name}Test.java") or test_lookup.get(
            f"{source_name}Test.kt"
        )
        file_results.append(
            {
                **file_info,
                **metrics,
                "has_corresponding_test_file": test_file is not None,
                "test_file_path": test_file,
                "build_blocker": item.get("build_blocker", ""),
                "manual_validation": "pending",
                "manual_validation_notes": "",
            }
        )

    item["modified_files"] = file_results
    item["coverage_report_result"] = report_status
    if not files:
        item["coverage_summary"] = "no_modified_java_kotlin_sources"
    elif report_status != "passed":
        item["coverage_summary"] = f"report_{report_status}"
    else:
        measured = [row for row in file_results if row["coverage_status"] == "measured"]
        item["coverage_summary"] = (
            "measured" if measured else "no_changed_sources_matched_report"
        )
    return file_results


def make_sheet_name(repo_name, used_names):
    stem = repo_name.replace("/", "_")[-31:]
    candidate = stem
    suffix = 2
    while candidate in used_names:
        ending = f"_{suffix}"
        candidate = f"{stem[:31-len(ending)]}{ending}"
        suffix += 1
    used_names.add(candidate)
    return candidate


def summarize_pr_coverage(repo_name, item):
    files = item.get("modified_files", [])
    measured = [file for file in files if file.get("coverage_status") == "measured"]

    def total(metric):
        return sum(file.get(metric) or 0 for file in measured)

    lines_covered = total("lines_covered")
    lines_missed = total("lines_missed")
    branches_covered = total("branches_covered")
    branches_missed = total("branches_missed")

    def percentage(covered, missed):
        total_count = covered + missed
        return round(covered * 100 / total_count, 2) if total_count else None

    coverage_summary = item.get("coverage_summary", "")
    report_result = item.get("coverage_report_result")
    if report_result is None:
        report_result = (
            "parse_failed"
            if coverage_summary == "parse_failed"
            else item.get("coverage_status", "not_run")
        )

    return {
        "repo": repo_name,
        "instance_id": item.get(
            "instance_id", f"{repo_name.replace('/', '__')}-pr-{item['pr_number']}"
        ),
        "pr_number": item["pr_number"],
        "base_sha_full": item.get("base_sha_full"),
        "head_sha_full": item.get("head_sha_full"),
        "coverage_report_result": report_result,
        "coverage_summary": coverage_summary,
        "changed_source_files": len(files),
        "measured_source_files": len(measured),
        "source_files_not_in_report": sum(
            file.get("coverage_status") == "not_in_report" for file in files
        ),
        "source_files_not_in_base": sum(
            file.get("coverage_status") == "not_in_base" for file in files
        ),
        "source_files_with_test_file": sum(
            file.get("has_corresponding_test_file") is True for file in files
        ),
        "lines_covered": lines_covered,
        "lines_missed": lines_missed,
        "line_coverage_percentage": percentage(lines_covered, lines_missed),
        "branches_covered": branches_covered,
        "branches_missed": branches_missed,
        "branch_coverage_percentage": percentage(
            branches_covered, branches_missed
        ),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Map JaCoCo base-state reports to source files changed by each PR."
    )
    parser.add_argument("json_file")
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--output-json", default="coverage-reports.json")
    parser.add_argument("--output-xlsx", default="coverage-reports.xlsx")
    parser.add_argument("--repo", action="append", help="Limit to owner/name; repeatable")
    args = parser.parse_args()

    json_path = Path(args.json_file).resolve()
    data = json.loads(json_path.read_text(encoding="utf-8"))
    reports_root = Path(args.reports_dir).resolve()
    allowed_repos = set(args.repo or [])
    rows_by_repo = {}
    summary_rows = []

    for repo in data["repos"]:
        repo_name = repo["repo"]
        if allowed_repos and repo_name not in allowed_repos:
            continue
        repo_path = Path.home() / "local-repos" / repo_name
        rows = []
        for item in repo.get("prs", []):
            try:
                file_results = reports_for_item(
                    item, repo_path, reports_root
                )
            except (OSError, RuntimeError, ET.ParseError, ValueError) as error:
                item["coverage_parse_error"] = str(error)
                item["coverage_summary"] = "parse_failed"
                file_results = []
            summary_rows.append(summarize_pr_coverage(repo_name, item))
            for file_result in file_results:
                rows.append(
                    {
                        "repo": repo_name,
                        "pr_number": item["pr_number"],
                        "base_sha_full": item.get("base_sha_full"),
                        "head_sha_full": item.get("head_sha_full"),
                        **file_result,
                    }
                )
        rows_by_repo[repo_name] = rows

    json_output = Path(args.output_json).resolve()
    save_json(json_output, data)
    used_names = set()
    with pd.ExcelWriter(args.output_xlsx, engine="openpyxl") as writer:
        summary_columns = [
            "repo",
            "instance_id",
            "pr_number",
            "base_sha_full",
            "head_sha_full",
            "coverage_report_result",
            "coverage_summary",
            "changed_source_files",
            "measured_source_files",
            "source_files_not_in_report",
            "source_files_not_in_base",
            "source_files_with_test_file",
            "lines_covered",
            "lines_missed",
            "line_coverage_percentage",
            "branches_covered",
            "branches_missed",
            "branch_coverage_percentage",
        ]
        pd.DataFrame(summary_rows, columns=summary_columns).to_excel(
            writer,
            sheet_name="PR Coverage Summary",
            index=False,
        )
        for repo_name, rows in rows_by_repo.items():
            dataframe = pd.DataFrame(rows)
            columns = [
                "repo",
                "pr_number",
                "base_sha_full",
                "head_sha_full",
                "file_path",
                "base_coverage_path",
                "change_type",
                "coverage_status",
                "lines_covered",
                "lines_missed",
                "line_coverage_percentage",
                "branches_covered",
                "branches_missed",
                "branch_coverage_percentage",
                "has_corresponding_test_file",
                "test_file_path",
                "build_blocker",
                "manual_validation",
                "manual_validation_notes",
            ]
            if dataframe.empty:
                dataframe = pd.DataFrame(columns=columns)
            else:
                dataframe = dataframe.reindex(columns=columns)
            dataframe.to_excel(
                writer,
                sheet_name=make_sheet_name(repo_name, used_names),
                index=False,
            )

    print(f"Coverage JSON written to {json_output}; workbook written to {args.output_xlsx}")


if __name__ == "__main__":
    main()
