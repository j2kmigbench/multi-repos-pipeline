"""Scripts for the multi-repository migration pipeline."""
"""Five-script pipeline package with compatibility aliases for older callers."""

from . import jacoco_debug_script, jacoco_parser_script, pr_image_script, pr_tagging_script

pipeline_utils = pr_image_script
process_repo_images_script = pr_tagging_script
process_scheudling_script = pr_tagging_script
