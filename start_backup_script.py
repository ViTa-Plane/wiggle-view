# version = 1.0.2

from datetime import datetime
import os
import sys
import zipfile

# Try importing pathspec for robust .gitignore handling.
# If not installed, instruct the user how to install it.
try:
  import pathspec
except ImportError:
  print("Error: The 'pathspec' library is required for .gitignore parsing.")
  print("Please install it by running: pip install pathspec")
  input("\nPress Enter to exit...")
  sys.exit(1)


def load_gitignore(root_dir):
  """Loads patterns from .gitignore in the root directory if it exists."""
  gitignore_path = os.path.join(root_dir, ".gitignore")
  patterns = [
      ".git",
      "backup",
  ]  # Always ignore the .git directory and the backup folder itself

  if os.path.exists(gitignore_path):
    print(f"-> Reading ignore rules from: {gitignore_path}")
    with open(gitignore_path, "r", encoding="utf-8", errors="ignore") as f:
      for line in f:
        stripped = line.strip()
        # Skip empty lines and comments
        if stripped and not stripped.startswith("#"):
          patterns.append(stripped)
  else:
    print(
        "-> No .gitignore found. Defaulting to ignore '.git' and 'backup'"
        " folders."
    )

  return pathspec.PathSpec.from_lines("gitwildmatch", patterns)


def main():
  # 1. Setup paths and names
  current_dir = os.path.abspath(os.getcwd())
  parent_folder_name = os.path.basename(current_dir)

  backup_dir = os.path.join(current_dir, "backup")
  os.makedirs(backup_dir, exist_ok=True)

  # Format: parentfoldername_YYYY-MM-DD_HH-MM-SS.zip
  timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
  zip_filename = f"{parent_folder_name}_{timestamp}.zip"
  zip_filepath = os.path.join(backup_dir, zip_filename)

  print(f"=== Starting Backup Process ===")
  print(f"Source Directory : {current_dir}")
  print(f"Destination Zip  : {zip_filepath}\n")

  # 2. Load .gitignore rules
  spec = load_gitignore(current_dir)

  print("\n--- Archiving Files ---")
  archived_count = 0

  # 3. Walk through directory and add files to zip
  with zipfile.ZipFile(zip_filepath, "w", zipfile.ZIP_DEFLATED) as zipf:
    for root, dirs, files in os.walk(current_dir):
      # Calculate relative path from the root directory for matching
      rel_root = os.path.relpath(root, current_dir)
      if rel_root == ".":
        rel_root = ""

      # Filter out ignored directories in-place so os.walk doesn't traverse them
      dirs[:] = [
          d
          for d in dirs
          # Check both directory name and its relative path
          if not spec.match_file(
              os.path.normpath(os.path.join(rel_root, d)) + "/"
          )
          and not spec.match_file(d)
      ]

      for file in files:
        rel_file_path = os.path.normpath(os.path.join(rel_root, file))

        # Check if file matches gitignore rules
        if spec.match_file(rel_file_path):
          print(f"[Ignored] {rel_file_path}")
          continue

        # Skip the zip file itself if it's somehow created inside the working tree
        if os.path.abspath(os.path.join(root, file)) == os.path.abspath(
            zip_filepath
        ):
          continue

        abs_file_path = os.path.join(root, file)
        arcname = rel_file_path  # Path inside the zip archive

        try:
          zipf.write(abs_file_path, arcname)
          print(f"[Added]   {arcname}")
          archived_count += 1
        except Exception as e:
          print(f"[Error]   Could not add {arcname}: {e}")

  print(f"\n=== Backup Completed Successfully ===")
  print(f"Total files archived: {archived_count}")
  print(f"Archive saved at: {zip_filepath}")

  # 4. Keep the window open
  input("\nPress Enter to exit...")


if __name__ == "__main__":
  main()