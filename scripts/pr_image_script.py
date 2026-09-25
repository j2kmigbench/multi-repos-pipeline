import json
import subprocess
import os
import textwrap
import sys

json_file = sys.argv[1]

with open(json_file, 'r') as file:
    data = json.load(file)

for repo in data["repos"]:
    repo_name = repo["repo"]
    output_dir = os.path.abspath(os.path.expanduser(f"~/local-repos/{repo_name}"))
    REPO_PATH = output_dir

    for item in repo["prs"]:
        #Metadata Extraction
        pr_number = item["pr_number"]
        first_commit = item["commit_shas_abbrev"][0]
        last_commit = item["commit_shas_abbrev"][-1]
        

        base_sha_temp = subprocess.run(
            ["git", "rev-parse", first_commit],
            cwd=REPO_PATH,
            capture_output=True,
            text=True,
            check=True
        )
        base_sha = base_sha_temp.stdout.strip()
        short_sha = base_sha[:7]

        head_sha_temp = subprocess.run(
            ["git", "rev-parse", last_commit],
            cwd=REPO_PATH,
            capture_output=True,
            text=True,
            check=True
        )
        head_sha = head_sha_temp.stdout.strip()

        tag = f"{repo_name.lower()}:pr-{pr_number}-base-{short_sha}"

        dockerfile_content = textwrap.dedent(f"""
            FROM j2kbench-shared:latest

            WORKDIR /workspace/repo
            COPY . /workspace/repo

                        RUN git checkout {base_sha} && \
                rm -rf .git && \
                git init -q && \
                git config user.email "bench@j2kmigbench.org" && \
                git config user.name "J2KBench" && \
                git add -A && \
                git commit -q -m "base state"
                
            RUN chmod +x ./gradlew
            RUN ./gradlew --no-daemon assembleDebug
            RUN ./gradlew testDebugUnitTest --no-daemon || true
            RUN ./gradlew compileDebugUnitTestKotlin --no-daemon || true
            """)
        with open('Dockerfile.tmp', 'w') as file:
            file.write(dockerfile_content)

        try:
            subprocess.run(["docker", "build", "-t", f"{tag}", "-f", "Dockerfile.tmp", REPO_PATH], check=True)
        finally:
            if(os.path.exists('Dockerfile.tmp')):
                os.remove('Dockerfile.tmp')


        #Verification Checks
        gitCheck = ["docker", "run", "--rm", f"{tag}", "git", "log", "--all", "--oneline"]
        gitResult = subprocess.run(gitCheck, capture_output=True, text=True)
        lines = [line for line in gitResult.stdout.strip().splitlines() if line]
        is_single_commit = (len(lines) == 1)

        networkCheck = [
            "docker", "run", "--rm", "--network", "none", f"{tag}",
            "./gradlew", "testDebugUnitTest", "--offline", "--gradle-user-home", "/root/.gradle",
            "-x", "connectedDebugAndroidTest"
        ]
        networkResult = subprocess.run(networkCheck, capture_output=True, text=True)

        item["base_sha_full"] = base_sha
        item["head_sha_full"] = head_sha
        item["image_size"] = subprocess.run(["docker", "image", "inspect", tag, "--format={{.Size}}"], capture_output=True, text=True).stdout.strip()
        item["git_result"] = is_single_commit
        item["network_result"] = networkResult.returncode == 0

with open(json_file, 'w') as file:
    json.dump(data, file, indent=2)

print(f"Image build and verification completed successfully. Results saved to {json_file}")