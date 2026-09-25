import json
import subprocess
import os
import shutil
import sys

json_file = sys.argv[1]

GRADLE_TASKS = [
    "createDebugUnitTestCoverageReport",
    "jacocoFullDebugCodeCoverage",
    "createFullDebugUnitTestCoverageReport",
    "testDebugUnitTest"
]

def setup_output_dir(repo_name, pr_number):
    output_dir = os.path.abspath(f"./reports/{repo_name}/pr_{pr_number}")
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    return output_dir

def run_gradle_tasks(container_name, tag):
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
    subprocess.run([
        "docker", "create", "--name", container_name, 
        "--network", "none", tag, "tail", "-f", "/dev/null"
    ], check=True, capture_output=True)

    try:
        subprocess.run(["docker", "start", container_name], check=True, capture_output=True)
        for task in GRADLE_TASKS:
            result = subprocess.run([
                "docker", "exec", "-w", "/workspace/repo", container_name,
                "./gradlew", task, "--offline",
                "--gradle-user-home", "/root/.gradle",
                "-x", "connectedDebugAndroidTest", "--no-build-cache"
            ], capture_output=True, text=True)
            
            if result.returncode == 0:
                print(f"  Completed task: {task}")
                return True
            print(f"  Task failed: {task}")
        return False
    finally:
        pass # Defer cleanup to caller for potential artifact extraction

def extract_jacoco_xml(output_dir):
    for root, _, files in os.walk(output_dir):
        if any(ignore in root for ignore in ["tmp", ".cache", "intermediates"]):
            continue
        for f in files:
            if f.endswith('.xml') and not f.endswith('pom.xml'):
                full_path = os.path.join(root, f)
                try:
                    with open(full_path, 'r', encoding='utf-8', errors='ignore') as xml_file:
                        content = xml_file.read(2048)
                        if '<report' in content and ('<package' in content or 'counter' in content):
                            return full_path
                except Exception:
                    continue
    return None

def main():
    with open(json_file, "r") as file:
        data = json.load(file)

    for repo in data["repos"]:
        repo_name = repo["repo"]
        output_dir = os.path.abspath(os.path.expanduser(f"~/local-repos/{repo_name.lower()}"))
        REPO_PATH = output_dir
        for item in repo["prs"]:
            pr_number = item["pr_number"]
            short_sha = item["base_sha_full"][:7]
            print(f"Starting PR #{pr_number}")

            output_dir = setup_output_dir(repo_name, pr_number)
            container_name = f"jacoco_runner_pr_{pr_number}"
            tag = f"ghcr.io/j2kmigbench/{repo_name}:pr-{pr_number}-base-{short_sha}"

            item["debug_result"] = run_gradle_tasks(container_name, tag)
            
            if item["debug_result"]:
                subprocess.run([
                    "docker", "cp", 
                    f"{container_name}:/workspace/repo/app/build/reports/", output_dir
                ], capture_output=True)
                
            subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

            xml_path = extract_jacoco_xml(output_dir)
            item["coverage_xml_extracted"] = bool(xml_path)

            if xml_path:
                clean_xml_target = os.path.join(output_dir, "jacoco.xml")
                if os.path.abspath(xml_path) != os.path.abspath(clean_xml_target):
                    shutil.move(xml_path, clean_xml_target)

            print(f"Result for PR #{pr_number}:\n  Task Execution: {item['debug_result']}\n  XML Extracted:  {item['coverage_xml_extracted']}\n")

    with open(json_file, 'w') as file:
        json.dump(data, file, indent=2)
    print("Jacoco debug completed successfully.")

if __name__ == "__main__":
    main()