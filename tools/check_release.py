"""Check a staged public source tree for private fixtures and unwanted runtimes."""
from pathlib import Path
import re
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
tracked = subprocess.check_output(["git", "ls-files", "--cached", "-z"], cwd=root).decode().split("\0")
errors = []
patterns = [re.compile(rb"-----BEGIN (?:OPENSSH|RSA|EC|DSA) PRIVATE KEY-----"),
            re.compile(rb"(?:ghp_|github_pat_)[A-Za-z0-9_]{30,}"),
            re.compile(rb"sk-(?:ant-)?[A-Za-z0-9_-]{30,}")]
for name in filter(None, tracked):
    path = root / name
    if any(part in name.split("/") for part in (".venv", "__pycache__", ".claude", "contracts")):
        errors.append(f"Private/build path staged: {name}")
    if path.is_symlink():
        errors.append(f"Source tree symlink must be reviewed: {name}")
        continue
    data = path.read_bytes()
    if any(pattern.search(data) for pattern in patterns):
        errors.append(f"Sensitive data pattern in {name}")
if errors:
    print("\n".join(errors), file=sys.stderr)
    raise SystemExit(1)
print(f"Public source scan passed ({len(list(filter(None, tracked)))} files).")
