import sys
import re

# version = 1.0.3

def bump_version_in_file(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # Group 1: Comment prefix (# version = , // version = , or <!-- version = )
    # Groups 2, 3, 4: Major, Minor, Patch numbers
    # Group 5: Optional closing tag ( --> )
    pattern = r'((?:#|//|<!--)\s*version\s*=\s*)(\d+)\.(\d+)\.(\d+)(\s*-->)?'
    match = re.search(pattern, content)

    if match:
        prefix, major, minor, patch, suffix = match.groups()

        # Fallback to empty string if suffix is None (for .py and .js files)
        if suffix is None:
            suffix = ""

        # Increment the patch number locally
        new_patch = int(patch) + 1

        # Reconstruct the string keeping the suffix intact
        new_version_str = f"{prefix}{major}.{minor}.{new_patch}{suffix}"

        # Replace only the first occurrence
        updated_content = re.sub(pattern, new_version_str, content, count=1)

        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(updated_content)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        bump_version_in_file(sys.argv[1])
