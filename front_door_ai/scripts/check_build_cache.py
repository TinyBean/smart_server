"""Build release/code variants and require reuse of expensive Docker RUN layers."""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


def build(context, image, version, log_path):
    started = time.monotonic()
    command = ["docker", "build", "--progress=plain", "--build-arg", f"BUILD_VERSION={version}",
               "--label", f"io.hass.version={version}", "-t", image, "."]
    print(f"Building {image} ({version})", flush=True)
    with log_path.open("w", encoding="utf-8") as log:
        result = subprocess.run(command, cwd=context, stdout=log, stderr=subprocess.STDOUT)
    output = log_path.read_text(encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"Build failed; see {log_path}\n{output[-6000:]}")
    duration = round(time.monotonic() - started, 2)
    print(f"Built in {duration}s; log: {log_path}", flush=True)
    return output, duration


def require_cached(output, *, code_changed):
    steps = re.findall(r"^#(\d+) \[[^\n]+\] RUN ([^\n]+)", output, re.MULTILINE)
    checked = []
    for step, command in steps:
        # This cheap step follows COPY app and legitimately reruns after source edits.
        if code_changed and command == "chmod +x /run.sh":
            continue
        if not re.search(rf"^#{step} CACHED$", output, re.MULTILINE):
            raise AssertionError(f"Expensive build step was not cached: {command}")
        checked.append(command)
    if len(checked) < 4:
        raise AssertionError(f"Expected system, uv, dependency and model steps; found {checked}")
    return checked


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="front-door-ai:test", help="Keep the baseline under this tag")
    parser.add_argument("--log-dir", type=Path, default=PROJECT / ".cache" / "build-cache")
    args = parser.parse_args()
    args.log_dir = args.log_dir.resolve()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    version = tomllib.loads((PROJECT / "pyproject.toml").read_text("utf-8"))["project"]["version"]
    token = uuid.uuid4().hex[:12]
    probe_image = f"front-door-ai:cache-probe-{token}"
    report = {}
    try:
        with tempfile.TemporaryDirectory(prefix="frontdoor-build-cache-") as directory:
            context = Path(directory)
            # Never copy local options, credentials, captured camera images or virtual environments.
            for name in ("Dockerfile", ".dockerignore", "pyproject.toml", "uv.lock", "requirements-runtime.txt",
                         "run.sh", "THIRD_PARTY.md", "app", "scripts"):
                source = PROJECT / name
                if source.is_dir():
                    shutil.copytree(source, context / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                else:
                    shutil.copy2(source, context / name)
            _, report["baseline_seconds"] = build(context, args.image, version, args.log_dir / "baseline.log")
            # Match a real release: both project metadata files, Dockerfile default and HA build arg change.
            next_version = f"{version}.post1"
            for name in ("pyproject.toml", "uv.lock"):
                path = context / name
                path.write_text(path.read_text("utf-8").replace(
                    f'name = "front-door-ai"\nversion = "{version}"',
                    f'name = "front-door-ai"\nversion = "{next_version}"', 1), encoding="utf-8")
            path = context / "Dockerfile"
            path.write_text(path.read_text("utf-8").replace(
                f"ARG BUILD_VERSION={version}", f"ARG BUILD_VERSION={next_version}"), encoding="utf-8")
            subprocess.run([sys.executable, "scripts/export_runtime_requirements.py", "--check"],
                           cwd=context, check=True)
            output, report["version_change_seconds"] = build(
                context, probe_image, next_version, args.log_dir / "version-change.log")
            report["version_cached_steps"] = require_cached(output, code_changed=False)
            with (context / "app" / "__init__.py").open("a", encoding="utf-8") as source:
                source.write(f"\n# Isolated cache verification: {token}\n")
            output, report["code_change_seconds"] = build(
                context, probe_image, next_version, args.log_dir / "code-change.log")
            report["code_cached_steps"] = require_cached(output, code_changed=True)
        (args.log_dir / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
    finally:
        subprocess.run(["docker", "image", "rm", probe_image], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)


if __name__ == "__main__":
    main()
