import json
import subprocess
import os
import sys

json_file = sys.argv[1]

with open(json_file, 'r') as file:
    data = json.load(file)

for repos in data["repos"]:
    repo_name = repos["repo"]
    print(f"Starting {repo_name} repo:")
    repo_url = f"https://github.com/{repo_name}"

    output_dir = os.path.abspath(os.path.expanduser(f"~/local-repos/{repo_name}"))
    if os.path.exists(output_dir):
        print(f"Repository already exists at {output_dir}. Skipping clone.")
    else:       
        repo_clone = subprocess.run(["git", "clone", repo_url, output_dir], capture_output=True, text=True, check=True)
    subprocess.run(["git", "fetch", "origin", "+refs/pull/*:refs/remotes/origin/pull/*"], cwd=output_dir, check=True)
    REPO_PATH = output_dir

    for item in repos["prs"]:
        convert_files = []

        if len(item["commit_shas_abbrev"]) < 2:
            continue
        pr_number = item["pr_number"]
        print(f"Starting PR# {pr_number}")

        first_commit = item["commit_shas_abbrev"][0]
        last_commit = item["commit_shas_abbrev"][-1]

        try:
            cmd = subprocess.run(
                ["git", "diff", "--name-status", first_commit, last_commit],
                cwd=REPO_PATH, capture_output=True, text=True, check=True
            )
        except subprocess.CalledProcessError as e:
            print(f"Warning: Could not diff commits {first_commit} and {last_commit}. Skipping.")
            continue

        files = [line.split() for line in cmd.stdout.splitlines() if line.strip()]

        for parts in files:
            status = parts[0]

            match parts[0]:
                case 'R':
                    before_path = parts[1]
                    after_path = parts[2]

                    if(before_path.endswith('.java') and after_path.endswith('.kt')):
                        renamed_file = {
                            "before_path" : before_path,
                            "after_path" : after_path,
                            "detected_via" : status
                        }
                        convert_files.append(renamed_file)

                case 'D':
                    deleted_file = parts[1]

                    if deleted_file.endswith('.java'):
                        kot_file = f"{parts[1].removesuffix('.java')}.kt"
                        if ['A' , kot_file] in files:
                            da_file = {
                                "before_path" : deleted_file,
                                "after_path" : kot_file,
                                "detected_via" : 'D, A'
                            }
                            convert_files.append(da_file)
                case _:
                    pass

        item["migrated_files"] = convert_files

    for item in repos["commits"]:
        base_sha = item["base_sha_full"]
        head_sha = item["head_sha_full"]
        convert_files = []

        commit_id = item["instance_id"]
        print(f"Starting commit - {commit_id}")

        try:
            cmd = subprocess.run(
                ["git", "diff", "--name-status", base_sha, head_sha],
                cwd=REPO_PATH, capture_output=True, text=True, check=True
            )
        except subprocess.CalledProcessError:
            print(f"Warning: Could not process commit pair {head_sha} -> {base_sha}. Skipping.")
            continue

        files = [line.split() for line in cmd.stdout.splitlines() if line.strip()]

        for parts in files:
            status = parts[0]

            match parts[0]:
                case 'R':
                    before_path = parts[1]
                    after_path = parts[2]

                    if(before_path.endswith('.java') and after_path.endswith('.kt')):
                        renamed_file = {
                            "before_path" : before_path,
                            "after_path" : after_path,
                            "detected_via" : status
                        }
                        convert_files.append(renamed_file)

                case 'D':
                    deleted_file = parts[1]
                    if deleted_file.endswith('.java'):
                        kot_file = f"{parts[1].removesuffix('.java')}.kt"
                        if ['A' , kot_file] in files:
                            da_file = {
                                "before_path" : deleted_file,
                                "after_path" : kot_file,
                                "detected_via" : 'D, A'
                            }
                            convert_files.append(da_file)
                case _:
                    pass

        item["migrated_files"] = convert_files

with open(json_file, 'w') as file:
    json.dump(data, file, indent=2)

print(f"Modified file found and results in {json_file}") 