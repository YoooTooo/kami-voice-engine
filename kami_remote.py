#!/usr/bin/env python3
"""Submit audio files to the Kami Voice Engine GitHub Actions workflow.

Save as ~/kami-voice-engine/kami_remote.py and run from any directory:
  python3 kami_remote.py input.wav --output output/amaterasu
  python3 kami_remote.py input/week --output output/week

Requires the `aws` and `gh` commands, an authenticated GitHub CLI, and the
local AWS profile used for the private Cloudflare R2 bucket.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid


AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg", ".m4a"}
DEFAULT_REPOSITORY = "YoooTooo/kami-voice-engine"
DEFAULT_WORKFLOW = "cpu-smoke-test.yml"
DEFAULT_BUCKET = "kami-voice-private"
DEFAULT_PROFILE = "kami-voice-upload"
DEFAULT_ENDPOINT = "https://1978af9448f6776910323e6d3dbd7b56.r2.cloudflarestorage.com"
REQUEST_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")


class CommandError(RuntimeError):
    pass


def absolute(value):
    return Path(value).expanduser().resolve()


def run(command, *, capture=False, timeout=None):
    result = subprocess.run(
        command,
        text=True,
        capture_output=capture,
        timeout=timeout,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        if len(detail) > 2000:
            detail = detail[-2000:]
        raise CommandError(f"Command failed ({result.returncode}): {command[0]}\n{detail}")
    return result.stdout.strip() if capture else ""


def require_command(name):
    if not shutil.which(name):
        raise CommandError(f"Required command not found on PATH: {name}")


def collect_inputs(source):
    if source.is_file():
        if source.suffix.lower() not in AUDIO_EXTENSIONS:
            raise CommandError(f"Unsupported audio extension: {source.suffix}")
        return [source]
    if not source.is_dir():
        raise CommandError(f"Input does not exist: {source}")
    files = sorted(
        (item for item in source.iterdir()
         if item.is_file() and item.suffix.lower() in AUDIO_EXTENSIONS),
        key=lambda item: item.name.casefold(),
    )
    if not files:
        raise CommandError("No supported audio files found directly in the input folder.")
    stems = [item.stem.casefold() for item in files]
    if len(stems) != len(set(stems)):
        raise CommandError("Input stems must be unique; e.g. quiz.wav and quiz.mp3 conflict.")
    if len(files) > 100:
        raise CommandError("At most 100 files may be submitted in one request.")
    return files


def default_request_id():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"manual-{stamp}-{uuid.uuid4().hex[:8]}"


def validate_args(args):
    if not REQUEST_PATTERN.fullmatch(args.request_id):
        raise CommandError("request-id must use letters, numbers, dot, underscore, or hyphen (max 80).")
    if not -24 <= args.pitch <= 24:
        raise CommandError("pitch must be from -24 to 24.")
    if args.output.exists():
        raise CommandError(f"Output already exists; choose a new folder: {args.output}")
    args.output.parent.mkdir(parents=True, exist_ok=True)


def list_run_ids(args):
    raw = run([
        "gh", "run", "list", "--repo", args.repository,
        "--workflow", args.workflow, "--event", "workflow_dispatch",
        "--limit", "30", "--json", "databaseId",
    ], capture=True, timeout=30)
    return {int(item["databaseId"]) for item in json.loads(raw or "[]")}


def upload_inputs(args, files):
    prefix = f"s3://{args.bucket}/jobs/{args.request_id}/input"
    existing = run([
        "aws", "s3", "ls", f"{prefix}/",
        "--profile", args.profile, "--endpoint-url", args.endpoint,
    ], capture=True, timeout=30)
    if existing:
        raise CommandError(
            f"Request input already exists; use a new request-id: {args.request_id}"
        )
    for file in files:
        print(f"Uploading {file.name} ...")
        run([
            "aws", "s3", "cp", str(file), f"{prefix}/{file.name}",
            "--profile", args.profile, "--endpoint-url", args.endpoint,
            "--only-show-errors",
        ], timeout=args.timeout_seconds)


def trigger(args, count):
    run([
        "gh", "workflow", "run", args.workflow,
        "--repo", args.repository,
        "-f", f"request_id={args.request_id}",
        "-f", f"expected_count={count}",
        "-f", f"pitch={args.pitch}",
    ], timeout=30)


def discover_run(args, previous):
    deadline = time.monotonic() + min(args.timeout_seconds, 180)
    while time.monotonic() < deadline:
        current = list_run_ids(args)
        created = current - previous
        if created:
            return max(created)
        time.sleep(3)
    raise CommandError(
        "Workflow was dispatched, but its run ID was not found. "
        f"Check GitHub Actions and request ID {args.request_id}."
    )


def wait_for_run(args, run_id):
    print(f"GitHub run: {run_id}")
    run([
        "gh", "run", "watch", str(run_id), "--repo", args.repository,
        "--exit-status", "--interval", "10",
    ], timeout=args.timeout_seconds)


def run_attempt(args, run_id):
    value = run([
        "gh", "api", f"repos/{args.repository}/actions/runs/{run_id}",
        "--jq", ".run_attempt",
    ], capture=True, timeout=30)
    attempt = int(value)
    if attempt < 1:
        raise CommandError(f"Invalid GitHub run attempt: {value}")
    return attempt


def download_outputs(args, run_id, attempt, expected_count):
    remote = (
        f"s3://{args.bucket}/jobs/{args.request_id}/output/"
        f"{run_id}-{attempt}/audio/"
    )
    temporary = args.output.parent / f".{args.output.name}.downloading-{uuid.uuid4().hex[:8]}"
    temporary.mkdir(parents=True, exist_ok=False)
    try:
        run([
            "aws", "s3", "sync", remote, str(temporary),
            "--profile", args.profile, "--endpoint-url", args.endpoint,
            "--only-show-errors",
        ], timeout=args.timeout_seconds)
        audio = sorted(item for item in temporary.iterdir()
                       if item.is_file() and item.suffix.lower() == ".wav")
        if len(audio) != expected_count:
            raise CommandError(
                f"Expected {expected_count} WAV outputs, downloaded {len(audio)} from {remote}"
            )
        os.replace(temporary, args.output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return remote


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=absolute, help="Audio file or flat folder")
    parser.add_argument("--output", required=True, type=absolute, help="New local output folder")
    parser.add_argument("--request-id", default=default_request_id())
    parser.add_argument("--pitch", type=int, default=2)
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--workflow", default=DEFAULT_WORKFLOW)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    args = parser.parse_args()

    require_command("aws")
    require_command("gh")
    if args.timeout_seconds < 60:
        parser.error("--timeout-seconds must be at least 60")
    files = collect_inputs(args.input)
    validate_args(args)

    # Authentication checks do not print credential values.
    run(["gh", "auth", "status"], timeout=30)
    run([
        "aws", "s3api", "head-bucket", "--bucket", args.bucket,
        "--profile", args.profile, "--endpoint-url", args.endpoint,
    ], timeout=30)

    previous = list_run_ids(args)
    print(f"Request ID: {args.request_id}")
    upload_inputs(args, files)
    trigger(args, len(files))
    run_id = discover_run(args, previous)
    wait_for_run(args, run_id)
    attempt = run_attempt(args, run_id)
    remote = download_outputs(args, run_id, attempt, len(files))
    print(f"Saved {len(files)} converted file(s): {args.output}")
    print(f"Private R2 source: {remote}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Cancelled. The GitHub job may still be running.", file=sys.stderr)
        sys.exit(130)
    except (CommandError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
