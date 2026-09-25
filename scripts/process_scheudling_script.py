import subprocess
import sys

json_file = sys.argv[1]

print("Starting migrated files script")
#subprocess.run(["python", "migrated_files_script.py", json_file], check=True)

print("Starting image building script")
subprocess.run(["python", "pr_image_script.py", json_file], check=True)

print("Starting iamge tagging script")
subprocess.run(["python", "pr_tagging_script.py", json_file], check=True)

print("Starting jacoco debugging script")
subprocess.run(["python", "jacoco_debug_script.py", json_file], check=True)

print("Starting jacoco parsing script")
subprocess.run(["python", "jacoco_parser_script.py", json_file], check=True)

print(f"All scripts complete. Results in {json_file}")