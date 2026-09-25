import json
import subprocess
import sys

json_file = sys.argv[1]

with open(json_file, 'r') as file:
    data = json.load(file)

for repo in data["repos"]:
    repo_name = repo["repo"]

    for item in repo["prs"]:
        warnings = []
        pr_number = item["pr_number"]
        base_sha_full = item["base_sha_full"]
        if not base_sha_full:
                    warnings.append(f"PR #{pr_number} not found in SHA mapping file")
        git_result = item["git_result"]
        network_result = item["network_result"]
        size = item["image_size"]
        ghcr_result = False

        if(git_result and network_result and base_sha_full):
            short_sha = base_sha_full[:7]
            local_tag = f"{repo_name.lower()}:pr-{pr_number}-base-{short_sha}"
            remote_tag = f"ghcr.io/j2kmigbench/{local_tag}"

            try:
                subprocess.run(["docker", "tag", local_tag, remote_tag], check=True)
                subprocess.run(["docker", "push", remote_tag], check=True)
                ghcr_result = True
            except subprocess.CalledProcessError:
                warnings.append("Docker tagging or GHCR push failed during execution")
                ghcr_result = False
        else:
            warnings.append("GHCR push skipped due to verification failure")

        if not git_result:
            warnings.append("Git history stripping failed (more than 1 commit found)")
        if not network_result:
            warnings.append("Offline build failed under --network none")

        item["ghcr_result"] = ghcr_result
        item["warnings"] = warnings

with open(json_file, 'w') as file:
    json.dump(data, file, indent=2)

print(f"Image tagging and GHCR push completed. Results in {json_file}.")