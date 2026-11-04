"""Check the exact Git snapshot before publishing application source."""

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import PurePosixPath

PUBLIC_ASSETS = {
    "docs/screenshots/gallery.jpg": "c03e802daf0d62a22878115833432a3b4082fb3fe23085e1b6c889c0894736fa",
    "framekeep/static/framekeep.ico": "db0c61ced91d67769908348713b14ba98fbe53fd8c61ad2cf4b8f1c1e60d2558",
    "framekeep/static/fonts/DM-Sans.ttf": "8cd08d97e89c24d0aa92edd2f0f4c8ee6195eee9b7c9f154865a58b02f0c1c0d",
}
ROOT_FILES = {
    ".gitignore", ".gitattributes", "README.md", "DESIGN.md", "PORTFOLIO.md", "LICENSE",
    "THIRD_PARTY_NOTICES.md", "pyproject.toml", "requirements.txt", "requirements-dev.txt",
    "requirements-lock.txt", "requirements-model-build.txt", "run.py", "desktop.py",
    "Setup Framekeep.cmd", "Start Framekeep.cmd", "Stop Framekeep.cmd", "Create Framekeep shortcuts.cmd",
}
SECRETS = [
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(rb"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(rb"sk-(?:proj-|svcacct-)[A-Za-z0-9_-]{20,}"),
    re.compile(rb"AKIA[A-Z0-9]{16}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]
PROFILE_PATH = re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+([^\\/\s'\"<>]+)", re.IGNORECASE)
EXAMPLE_PROFILES = {"you", "example", "test", "testuser", "public", "<username>", "{username}"}


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def allowed(path):
    parts = PurePosixPath(path).parts
    if path in ROOT_FILES or path in PUBLIC_ASSETS or path == ".github/workflows/ci.yml":
        return True
    if parts[0] == "framekeep":
        return path == "framekeep/category_prompts.json" or PurePosixPath(path).suffix in {
            ".py", ".html", ".css", ".js", ".svg",
        }
    if parts[0] == "tests":
        return PurePosixPath(path).suffix == ".py"
    if parts[0] == "scripts":
        return PurePosixPath(path).suffix in {".py", ".ps1"}
    if parts[0] == "third_party":
        return PurePosixPath(path).suffix == ".txt"
    return parts[0] == "docs" and PurePosixPath(path).suffix == ".md"


def snapshot(staged):
    if staged:
        records = []
        for record in git("ls-files", "--stage", "-z").split(b"\0"):
            if record:
                metadata, path = record.split(b"\t", 1)
                mode, sha, stage = metadata.decode().split()
                if stage != "0":
                    raise ValueError("Resolve Git index conflicts before publishing.")
                records.append((mode, sha, path.decode("utf-8")))
        return records
    records = []
    for record in git("ls-tree", "-rz", "--full-tree", "HEAD").split(b"\0"):
        if record:
            metadata, path = record.split(b"\t", 1)
            mode, kind, sha = metadata.decode().split()
            if kind != "blob":
                raise ValueError("Submodules are not part of this source release.")
            records.append((mode, sha, path.decode("utf-8")))
    return records


def check(staged=False):
    records = snapshot(staged)
    if not records:
        raise ValueError("No files are selected for publication.")
    request = "".join(sha + "\n" for _, sha, _ in records).encode()
    result = subprocess.run(["git", "cat-file", "--batch"], input=request, check=True, capture_output=True)
    stream, cursor, total = result.stdout, 0, 0
    violations, assets, manifest = [], [], []
    for mode, sha, path in records:
        end = stream.index(b"\n", cursor)
        header = stream[cursor:end].decode().split()
        if len(header) != 3 or header[0] != sha or header[1] != "blob":
            raise ValueError("Cannot read the selected Git blob.")
        length = int(header[2])
        data = stream[end + 1:end + 1 + length]
        cursor = end + length + 2
        total += length
        digest = hashlib.sha256(data).hexdigest()
        manifest.append({"path": path, "git_blob": sha, "sha256": digest, "bytes": length})
        if mode not in {"100644", "100755"} or not allowed(path):
            violations.append(path + ": unapproved path or file mode")
            continue
        if path in PUBLIC_ASSETS:
            if digest != PUBLIC_ASSETS[path]:
                violations.append(path + ": binary differs from the reviewed demo asset")
            assets.append(path)
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            violations.append(path + ": unexpected binary content")
            continue
        if "\x00" in text or length > 1_000_000:
            violations.append(path + ": unexpected encoded data")
        if any(pattern.search(data) for pattern in SECRETS):
            violations.append(path + ": possible credential or private key")
        if any(match.group(1).lower() not in EXAMPLE_PROFILES for match in PROFILE_PATH.finditer(text)):
            violations.append(path + ": non-example user profile path")
        if PurePosixPath(path).suffix == ".svg" and re.search(r"<image\b|data:image", text, re.IGNORECASE):
            violations.append(path + ": embedded raster in a vector asset")
        if PurePosixPath(path).suffix == ".json":
            value = json.loads(text)
            if set(value) != {"model", "commit", "labels", "prompts", "embeddings"}:
                violations.append(path + ": unexpected model prompt structure")
    missing = set(PUBLIC_ASSETS) - set(assets)
    violations.extend(path + ": reviewed asset missing" for path in sorted(missing))
    if violations:
        raise ValueError("Publication check failed:\n" + "\n".join(violations))
    return {"files": len(records), "bytes": total, "reviewed_binary_assets": assets, "manifest": manifest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="Check the exact staged snapshot instead of HEAD.")
    args = parser.parse_args()
    try:
        result = check(args.staged)
    except (ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, str(error) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "manifest"}))


if __name__ == "__main__":
    main()
