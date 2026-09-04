#!/usr/bin/env python3
"""Kami Voice Engine: local Applio adapter (single file / folder batch).

Put this file at ~/kami-voice-engine/kami_voice.py.
Requires existing Applio, ffmpeg and ffprobe. Python standard library only.
  python3 kami_voice.py convert input.wav --output output/answer-rvc.wav
  python3 kami_voice.py batch input/week --output output/week --expected-count 7

Default: tested Kokoro female -> Amaterasu settings. Pitch 11 alone does NOT
reproduce the user's own voice preset (formants pending).
No cloud calls, TTS, upload, retention deletion, or scheduling.
Batch output must be a NEW folder: <input-stem>.wav + batch-result.json.
Exit 0 = all succeeded; 1 = conversion/partial failure; 2 = argument error.
After partial failure, validated outputs remain available; inspect the report.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

APPLIO_REFERENCE_COMMIT = "085197e738ce9dd4c0bae1e0a74df5de25b89444"
AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg", ".m4a"}
THREAD_ENV = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
              "VECLIB_MAXIMUM_THREADS", "NUMBA_NUM_THREADS")
FAILURES = (OSError, RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired)


def absolute(value):
    return Path(value).expanduser().resolve()


def executable_path(value):
    # Resolving a venv Python symlink would incorrectly select the base Python.
    return Path(os.path.abspath(os.path.expanduser(value)))


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def checked(command, timeout=120):
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{Path(command[0]).name} failed: {result.stderr[-1500:].strip()}")
    return result.stdout


def validate_wav(file):
    if not file.is_file() or file.stat().st_size <= 44:
        raise RuntimeError(f"Missing or empty WAV: {file}")
    with file.open("rb") as audio:
        header = audio.read(12)
    if header[:4] not in (b"RIFF", b"RF64") or header[8:12] != b"WAVE":
        raise RuntimeError(f"Not a WAV container: {file}")
    metadata = json.loads(checked([
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=sample_rate,channels:format=duration", "-of", "json", str(file)]))
    duration = float(metadata.get("format", {}).get("duration", 0))
    streams = metadata.get("streams", [])
    if not streams or not 0 < duration < float("inf"):
        raise RuntimeError(f"No usable audio duration: {file}")
    checked(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-xerror",
             "-err_detect", "explode", "-i", str(file), "-map", "0:a:0", "-f", "null", "-"])
    return {"duration_seconds": duration, "sample_rate": int(streams[0]["sample_rate"]),
            "channels": int(streams[0]["channels"]), "bytes": file.stat().st_size}


def publish(source, target, overwrite=False):
    if overwrite:
        os.replace(source, target)
    else:
        # Same filesystem; atomic no-clobber publication.
        os.link(source, target)


def run_applio(args, mode, paths):
    env = os.environ.copy()
    for key in THREAD_ENV:
        env[key] = "1"
    if sys.platform == "darwin":
        env["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"
        env["PYTORCH_MPS_HIGH_WATERMARK_RATIO"] = "0.0"
    options = ["--pth-path", str(args.model), "--index-path", str(args.index),
               "--pitch", str(args.pitch), "--index-rate", "0.84",
               "--volume-envelope", "1", "--protect", "0.5", "--f0-method", "rmvpe",
               "--embedder-model", "contentvec", "--sid", "0", "--export-format", "WAV"]
    command = [str(args.python), "-X", "faulthandler", "core.py", mode, *paths, *options]
    return subprocess.run(command, cwd=args.applio_dir, env=env,
                          timeout=args.timeout_seconds).returncode


def convert(args):
    args.output.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix=".kami-", dir=args.output.parent) as folder:
        temporary = Path(folder) / "converted.wav"
        code = run_applio(args, "infer", ["--input-path", str(args.input), "--output-path", str(temporary)])
        if code:
            raise RuntimeError(f"Applio failed (exit {code}); existing output was not replaced")
        validate_wav(temporary)
        publish(temporary, args.output, args.overwrite)
    print(f"Saved: {args.output}")
    print(f"Total elapsed: {time.perf_counter() - started:.2f}s")
    return 0


def batch(args, inputs):
    # Each invocation owns a new directory. Partial results never mix with old ones.
    args.output.mkdir(parents=True, exist_ok=False)
    report_path = args.output / "batch-result.json"
    report = {
        "schema_version": 1, "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "applio_reference_commit": APPLIO_REFERENCE_COMMIT,
        "applio_exit_code": None, "applio_error": None,
        "settings": {"pitch": args.pitch, "index_rate": 0.84, "volume_envelope": 1,
                     "protect": 0.5, "f0_method": "rmvpe", "embedder_model": "contentvec",
                     "sid": 0, "formant_shifting": False},
        "model": str(args.model), "index": str(args.index),
        "files": [{"input": str(file), "output": str(args.output / f"{file.stem}.wav"),
                   "status": "pending"} for file in inputs],
    }

    def save_report():
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=".report-",
                                         suffix=".json", dir=args.output, delete=False) as handle:
            temporary_report = Path(handle.name)
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        try:
            os.replace(temporary_report, report_path)
        finally:
            temporary_report.unlink(missing_ok=True)

    started = time.perf_counter()
    save_report()
    with tempfile.TemporaryDirectory(prefix=".kami-", dir=args.output) as folder:
        staged, converted = Path(folder) / "input", Path(folder) / "output"
        staged.mkdir()
        converted.mkdir()
        ready = []
        for number, (file, item) in enumerate(zip(inputs, report["files"])):
            name = f"item-{number:04d}"
            try:
                # Normalize container only: preserve sample rate and channel count.
                # Unique WAV filenames avoid upstream name/extension collisions.
                checked(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-xerror",
                         "-err_detect", "explode", "-y", "-i", str(file), "-map", "0:a:0",
                         "-vn", "-c:a", "pcm_f32le", str(staged / f"{name}.wav")])
                validate_wav(staged / f"{name}.wav")
                ready.append((name, item))
            except FAILURES as error:
                (staged / f"{name}.wav").unlink(missing_ok=True)
                item.update(status="failed", error=f"Input preparation: {error}")
            save_report()

        try:
            if ready:
                report["applio_exit_code"] = run_applio(args, "batch-infer", [
                    "--input-folder", str(staged), "--output-folder", str(converted)])
        except (OSError, subprocess.TimeoutExpired) as error:
            report["applio_error"] = str(error)

        # Exit 0 alone is not enough: upstream may omit failed files.
        # Even after a crash, preserve any individually validated outputs.
        for name, item in ready:
            try:
                temporary = converted / f"{name}_output.wav"
                details = validate_wav(temporary)
                publish(temporary, Path(item["output"]))
                item.update(status="succeeded", audio=details)
            except FAILURES as error:
                item.update(status="failed", error=f"Output validation: {error}")
            save_report()

    count = sum(item["status"] == "succeeded" for item in report["files"])
    succeeded = count == len(inputs) and report["applio_exit_code"] == 0 and not report["applio_error"]
    report.update(status="succeeded" if succeeded else "failed",
                  succeeded_count=count, failed_count=len(inputs) - count,
                  elapsed_seconds=round(time.perf_counter() - started, 3),
                  finished_at=datetime.now(timezone.utc).isoformat())
    save_report()
    print(f"Batch: {count}/{len(inputs)} valid files; elapsed {report['elapsed_seconds']}s")
    print(f"Report: {report_path}")
    if not succeeded:
        print("Some work failed. Valid outputs are preserved; see the report.", file=sys.stderr)
    return 0 if succeeded else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for mode in ("convert", "batch"):
        command = commands.add_parser(mode)
        command.add_argument("input", type=absolute, help="Input audio file or flat folder")
        command.add_argument("--output", required=True, type=absolute, help="WAV file, or NEW batch folder")
        command.add_argument("--applio-dir", type=absolute, default=absolute("~/Applio"))
        command.add_argument("--python", type=executable_path, help="Applio's Python executable")
        command.add_argument("--model", type=absolute)
        command.add_argument("--index", type=absolute)
        command.add_argument("--pitch", type=int, choices=range(-24, 25), default=2)
        command.add_argument("--timeout-seconds", type=positive_int, default=1800,
                             help="Maximum Applio process runtime (default 1800)")
        if mode == "convert":
            command.add_argument("--overwrite", action="store_true")
        else:
            command.add_argument("--expected-count", type=positive_int)
    args = parser.parse_args()
    args.python = args.python or args.applio_dir / ".venv/bin/python"
    args.model = args.model or args.applio_dir / "logs/amaterasu/amaterasu.pth"
    args.index = args.index or args.applio_dir / "logs/amaterasu/amaterasu.index"
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            parser.error(f"{tool} is required on PATH")
    for label, file in (("Applio Python", args.python), ("Applio CLI", args.applio_dir / "core.py"),
                        ("Model", args.model), ("Index", args.index)):
        if not file.is_file():
            parser.error(f"{label} not found: {file}")
    if args.command == "convert":
        if not args.input.is_file():
            parser.error(f"Input not found: {args.input}")
        if args.output.suffix.lower() != ".wav":
            parser.error("--output must have a .wav extension")
        if args.output in (args.input, args.model, args.index, args.python.resolve(), args.applio_dir / "core.py"):
            parser.error("Output must differ from the input and engine files")
        if args.output.exists() and not args.overwrite:
            parser.error("Output exists; choose another name or use --overwrite")
        return convert(args)
    if not args.input.is_dir():
        parser.error("Batch input must be a folder")
    if args.output.exists():
        parser.error("Batch output must be a NEW folder; choose another name")
    if args.output.is_relative_to(args.input) or args.input.is_relative_to(args.output):
        parser.error("Batch input and output folders must not overlap")
    inputs = sorted((file for file in args.input.iterdir()
                     if file.is_file() and file.suffix.lower() in AUDIO_EXTENSIONS), key=lambda file: file.name)
    if not inputs:
        parser.error("No supported audio files (wav/mp3/flac/ogg/m4a) directly in input folder")
    if args.expected_count is not None and len(inputs) != args.expected_count:
        parser.error(f"Expected {args.expected_count} audio files, found {len(inputs)}")
    names = [file.stem.casefold() for file in inputs]
    if len(names) != len(set(names)):
        parser.error("Input stems must be unique (e.g. quiz.wav and quiz.mp3 conflict)")
    return batch(args, inputs)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Cancelled. An interrupted batch may retain a 'running' report; it is not complete.", file=sys.stderr)
        sys.exit(130)
    except FAILURES as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)