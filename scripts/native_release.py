#!/usr/bin/env python3
"""Controlled-fork native CGO release custody through standard draft APIs.

Native proof is authenticated operator evidence, never hosted OS attestation.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

REPO = "DreamwareDevelopment/CLIProxyAPI"
DEFAULT = "main"
WORKFLOW = ".github/workflows/release.yaml"
OPERATOR = 25471746
ROOT = Path(__file__).resolve().parents[1]
PREFIX = "\n<!-- CLI_NATIVE_HANDOFF_V1\n"
SUFFIX = "\n-->\n"
GO_VERSION = "go1.26.4"
ARTIFACTS = ("windows-amd64", "windows-arm64", "linux-amd64", "linux-arm64", "linux-amd64-no-plugin", "linux-arm64-no-plugin", "freebsd-amd64", "freebsd-arm64-no-plugin")
JOBS = ("prepare-release", "build windows-amd64", "build windows-arm64", "build linux-amd64 glibc", "build linux-arm64 glibc", "build linux-amd64 no-plugin", "build linux-arm64 no-plugin", "build freebsd-amd64", "build freebsd-arm64-no-plugin")
MODELS = ("internal/registry/models/models.json", "internal/registry/models/codex_client_models.json")

class Refused(ValueError):
    pass

def require(condition, reason):
    if not condition:
        raise Refused(reason)

def run(argv, *, cwd=ROOT, log=None, env=None):
    subprocess.run(argv, cwd=cwd, check=True, stdout=log, stderr=subprocess.STDOUT if log else None, env=env)

def output(argv):
    return subprocess.check_output(argv, cwd=ROOT, text=True).strip()

def api(path):
    return json.loads(output(["gh", "api", path]))

def digest(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()

def clean_source():
    require(not output(["git", "status", "--porcelain"]), "source checkout must be clean")
    return output(["git", "rev-parse", "HEAD"])

def validate_producer(meta, jobs, artifacts, run_id, attempt, preparing=False):
    require(type(run_id) is int and run_id > 0 and type(attempt) is int and attempt > 0, "invalid producer identity")
    require(meta.get("id") == run_id and meta.get("run_attempt") == attempt, "producer attempt changed")
    require(meta.get("repository", {}).get("full_name") == REPO and
            meta.get("head_repository", {}).get("full_name") == REPO, "wrong producer repository")
    require(meta.get("path") == WORKFLOW, "wrong producer workflow")
    require(type(meta.get("workflow_id")) is int and meta["workflow_id"] > 0, "invalid producer workflow identity")
    require(meta.get("event") == "push", "untrusted producer event")
    require(re.fullmatch(r"[0-9a-f]{40}", meta.get("head_sha", "")), "invalid producer source")
    if preparing:
        require(meta.get("status") == "in_progress", "draft preparation must run inside the current producer")
    else:
        require(meta.get("status") == "completed" and meta.get("conclusion") == "success", "producer did not succeed")
    for name in JOBS:
        matching = [job for job in jobs if job.get("name") == name]
        require(len(matching) == 1 and matching[0].get("status") == "completed" and
                matching[0].get("conclusion") == "success", "missing, ambiguous or unsuccessful job: " + name)
    require(len(artifacts) == len(ARTIFACTS) and {a.get("name") for a in artifacts} == set(ARTIFACTS),
            "unexpected or ambiguous producer artifact inventory")
    for artifact in artifacts:
        require(type(artifact.get("id")) is int and artifact["id"] > 0 and not artifact.get("expired", True),
                "invalid or expired artifact")
        require(isinstance(artifact.get("digest"), str) and re.fullmatch(r"sha256:[0-9a-f]{64}", artifact["digest"]), "artifact has no server digest")
        require(artifact.get("workflow_run", {}).get("id") == run_id and
                artifact.get("workflow_run", {}).get("head_sha") == meta["head_sha"], "artifact source mismatch")
    return {"repo": REPO, "run_id": run_id, "attempt": attempt, "source_sha": meta["head_sha"],
            "workflow_id": meta["workflow_id"], "workflow_path": WORKFLOW,
            "event": meta["event"], "head_branch": meta["head_branch"],
            "artifacts": sorted(({key: a[key] for key in ("id", "name", "digest")} for a in artifacts), key=lambda a: a["name"])}

def producer(run_id, attempt, preparing=False):
    base = f"repos/{REPO}/actions/runs/{run_id}"
    meta = api(base)
    jobs_page = api(base + f"/attempts/{attempt}/jobs?per_page=100")
    artifacts_page = api(base + "/artifacts?per_page=100")
    require(jobs_page["total_count"] <= 100 and artifacts_page["total_count"] <= 100, "unbounded producer inventory")
    result = validate_producer(meta, jobs_page["jobs"], artifacts_page["artifacts"], run_id, attempt, preparing)
    workflow = api(f"repos/{REPO}/contents/{WORKFLOW}?ref={result['source_sha']}")
    require(workflow.get("type") == "file", "producer workflow is not a committed file")
    result["workflow_blob"] = workflow["sha"]
    current = api(f"repos/{REPO}/commits/{DEFAULT}")["sha"]
    require(current == result["source_sha"], "producer source is no longer current default source")
    return result

def one_asset(draft, name, founder=False):
    matches = [asset for asset in draft.get("assets", []) if asset.get("name") == name]
    require(len(matches) == 1, "missing or ambiguous native asset: " + name)
    asset = matches[0]
    require(type(asset.get("id")) is int and asset["id"] > 0 and asset.get("state") == "uploaded", "native asset incomplete")
    require(type(asset.get("size")) is int and 0 < asset["size"] <= (512 * 1024 * 1024 if name.startswith("CLIProxyAPI_") else 16 * 1024 * 1024), "native evidence size out of bounds")
    if founder:
        require(asset.get("uploader", {}).get("id") == OPERATOR, "native evidence lacks authenticated founder custody")
    return asset

def download(path, destination, max_size):
    with open(destination, "wb") as stream:
        subprocess.run(["gh", "api", "-H", "Accept: application/octet-stream", path], cwd=ROOT, stdout=stream, check=True)
    require(0 < destination.stat().st_size <= max_size, "download size out of bounds")

def download_asset(asset, destination):
    download(f"repos/{REPO}/releases/assets/{asset['id']}", destination, asset["size"])
    require(destination.stat().st_size == asset["size"], "native evidence size changed")
    server_digest = asset.get("digest")
    require(server_digest == "sha256:" + digest(destination), "native evidence digest mismatch")

def download_artifact(artifact, directory):
    archive_zip = directory / (str(artifact["id"]) + ".zip")
    download(f"repos/{REPO}/actions/artifacts/{artifact['id']}/zip", archive_zip, 2 * 1024 ** 3)
    require("sha256:" + digest(archive_zip) == artifact["digest"], "Actions artifact bytes changed")
    with zipfile.ZipFile(archive_zip) as archive:
        files = archive.infolist()
        require(len(files) == 1 and files[0].filename == Path(files[0].filename).name and
                files[0].file_size <= 2 * 1024 ** 3 and not files[0].is_dir(), "unsafe Actions artifact layout")
        name = files[0].filename
        require(re.fullmatch(r"[A-Za-z0-9._+-]+\.(tar\.gz|zip)", name), "unexpected artifact archive name")
        destination = directory / name
        require(not destination.exists(), "duplicate downloaded archive")
        with archive.open(files[0]) as source, destination.open("wb") as target:
            shutil.copyfileobj(source, target)
    archive_zip.unlink()
    return destination

def github_outputs(values):
    path = os.environ.get("GITHUB_OUTPUT")
    require(path is not None, "GitHub output file missing")
    with open(path, "a") as stream:
        for key, value in values.items():
            stream.write(f"{key}={str(value).lower() if type(value) is bool else value}\n")


def tag_for(binding):
    tag = binding["head_branch"]
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", tag) and not tag.startswith("native-handoff-"), "unsupported release tag")
    return tag


def handoff_tag(binding):
    return f"native-handoff-{binding['run_id']}-{binding['attempt']}"


def archive_name(version, arch):
    return f"CLIProxyAPI_{version}_darwin_{'aarch64' if arch == 'arm64' else 'amd64'}.tar.gz"


def evidence_name(arch, extension):
    return f"native-darwin-{arch}.{extension}"


def validate_draft(draft, binding):
    require(draft.get("draft") is True and draft.get("target_commitish") == binding["source_sha"], "wrong or published draft")
    body = draft.get("body", "")
    require(body.count(PREFIX) == 1 and body.endswith(SUFFIX) and len(body) <= 100000, "missing bounded draft binding")
    record = json.loads(body.split(PREFIX)[1][:-len(SUFFIX)])
    seed = {key: binding[key] for key in ("repo", "run_id", "attempt", "source_sha", "workflow_blob")}
    require(record.get("schema_version") == 1 and record.get("seed") == seed and record.get("tag") == tag_for(binding), "draft producer binding mismatch")
    require(re.fullmatch(r"[0-9a-f]{40}", record.get("models_commit", "")), "missing pinned model catalog commit")
    require(draft.get("tag_name") == handoff_tag(binding), "draft tag mismatch")
    return record


def native_runtime(go_binary):
    require(platform.system() == "Darwin" and platform.machine() in ("arm64", "x86_64"), "actual native Mac executor required")
    require(os.environ.get("DREAMWARE_TEST_ACTIVE") == "1", "run the entire native command through dreamware-test")
    resolved_go = shutil.which(str(go_binary))
    require(resolved_go is not None, "existing Go 1.26.4 toolchain required")
    go_path = Path(resolved_go).resolve()
    version = subprocess.check_output([str(go_path), "version"], text=True, env=dict(os.environ, GOTOOLCHAIN="local")).strip()
    host_arch = "arm64" if platform.machine() == "arm64" else "amd64"
    require(version == f"go version {GO_VERSION} darwin/{host_arch}", "exact existing Go 1.26.4 native toolchain required; no automatic install")
    clang = Path(output(["xcrun", "--find", "clang"])).resolve()
    sdk = output(["xcrun", "--show-sdk-path"])
    return {"system": platform.system(), "machine": platform.machine(), "go": version,
            "go_path": str(go_path), "go_sha256": digest(go_path), "clang_path": str(clang),
            "clang_sha256": digest(clang), "sdk": sdk,
            "python_path": str(Path(sys.executable).resolve()), "python_sha256": digest(Path(sys.executable).resolve())}


def native_build(source, models_commit, version, arch, runtime, directory, log):
    execution_route(runtime["machine"], arch)

    with tempfile.TemporaryDirectory(prefix="cliproxy-native-build-") as scratch:
        checkout = Path(scratch) / "source"
        run(["git", "clone", "--shared", "--no-checkout", str(ROOT), str(checkout)], log=log)
        run(["git", "checkout", "--detach", source], cwd=checkout, log=log)
        target = "arm64" if arch == "arm64" else "x86_64"
        env = dict(os.environ, GOTOOLCHAIN="local", GOMAXPROCS="1", GOOS="darwin", GOARCH=arch, CGO_ENABLED="1",
                   PATH=str(Path(runtime["go_path"]).parent) + os.pathsep + os.environ["PATH"],
                   CC=runtime["clang_path"] + " -arch " + target, SDKROOT=runtime["sdk"],
                   CGO_CFLAGS="-arch " + target, CGO_LDFLAGS="-arch " + target,
                   MODELS_REPOSITORY_URL="https://github.com/router-for-me/models.git", MODELS_REPOSITORY_REF=models_commit)
        run(["bash", ".github/scripts/refresh-model-catalogs.sh"], cwd=checkout, env=env, log=log)
        require(output_in(checkout, ["git", "rev-parse", "FETCH_HEAD"]) == models_commit, "model catalog source moved")
        inputs = {path: digest(checkout / path) for path in MODELS}
        build_date = output(["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"])
        binary = directory / "cli-proxy-api"
        commit_short = output(["git", "rev-parse", "--short", source])
        flags = f"-s -w -X main.Version={version} -X main.Commit={commit_short} -X main.BuildDate={build_date}"
        run([runtime["go_path"], "build", "-p", "1", "-ldflags=" + flags, "-o", str(binary), "./cmd/server/"], cwd=checkout, env=env, log=log)
        require(output(["xcrun", "lipo", "-archs", str(binary)]) == target, "wrong native Mach-O architecture")
        metadata = output([runtime["go_path"], "version", "-m", str(binary)])
        require(all(value in metadata for value in ("CGO_ENABLED=1", "GOOS=darwin", "GOARCH=" + arch)), "native CGO build metadata mismatch")
        archive = directory / archive_name(version, arch)
        with tarfile.open(archive, "w:gz") as package:
            package.add(binary, arcname="cli-proxy-api")
            for path in ("LICENSE", "README.md", "README_CN.md", "config.example.yaml"):
                package.add(checkout / path, arcname=path)
        unpack = Path(scratch) / "packaged"
        unpack.mkdir()
        with tarfile.open(archive, "r:gz") as package:
            package.extractall(unpack, filter="data")
        require(digest(unpack / "cli-proxy-api") == digest(binary), "packaged native binary differs from verified build")
        # Execute the actual archive payload. An unavailable native target can
        # never become a successful cross-compile-only receipt.
        run([str(unpack / "cli-proxy-api"), "-h"], cwd=unpack, env=env, log=log)
        require(inputs == {path: digest(checkout / path) for path in MODELS}, "generated model inputs changed during native build")
        changed = output_in(checkout, ["git", "diff", "--name-only"]).splitlines()
        require(set(changed) <= set(MODELS) and not output_in(checkout, ["git", "ls-files", "--others", "--exclude-standard"]), "native build changed other source inputs")
    return archive, inputs, build_date


def execution_route(machine, arch):
    require(arch in ("amd64", "arm64"), "unknown native architecture")
    if machine == "arm64" and arch == "amd64":
        require(Path("/Library/Apple/usr/libexec/oah/libRosettaRuntime").is_file(), "amd64 execution unavailable; automatic Rosetta installation is forbidden")
    require(not (machine == "x86_64" and arch == "arm64"), "arm64 execution needs an actual ARM Mac")


def output_in(directory, argv):
    return subprocess.check_output(argv, cwd=directory, text=True).strip()


def prepare(args):
    source = clean_source()
    meta = api(f"repos/{REPO}/actions/runs/{args.run}")
    require(meta.get("id") == args.run and meta.get("run_attempt") == args.attempt and
            meta.get("head_sha") == source and meta.get("status") == "in_progress" and meta.get("event") == "push" and
            meta.get("repository", {}).get("full_name") == REPO and meta.get("head_repository", {}).get("full_name") == REPO and
            meta.get("path") == WORKFLOW, "draft preparation producer mismatch")
    workflow_blob = output(["git", "rev-parse", source + ":" + WORKFLOW])
    seed = {"repo": REPO, "run_id": args.run, "attempt": args.attempt, "source_sha": source, "workflow_blob": workflow_blob}
    tag = tag_for(dict(seed, head_branch=meta["head_branch"]))
    require(api(f"repos/{REPO}/commits/{tag}")["sha"] == source, "release tag source changed")
    models_commit = api("repos/router-for-me/models/commits/main")["sha"]
    require(re.fullmatch(r"[0-9a-f]{40}", models_commit), "invalid model catalog commit")
    record = {"schema_version": 1, "seed": seed, "tag": tag, "models_commit": models_commit}
    notes = args.notes.read_text()
    require(PREFIX not in notes, "ambiguous native binding in release notes")
    payload = {"tag_name": handoff_tag(seed), "target_commitish": source, "name": tag + " (native proof pending)",
               "body": notes + PREFIX + json.dumps(record, sort_keys=True) + SUFFIX, "draft": True}
    draft = json.loads(subprocess.check_output(["gh", "api", "--method", "POST", f"repos/{REPO}/releases", "--input", "-"],
                                               input=json.dumps(payload), text=True, cwd=ROOT))
    github_outputs({"draft_id": draft["id"], "handoff_tag": payload["tag_name"], "models_commit": models_commit})


def validate_receipt(receipt, binding, draft_id, arch, record, archive_asset, log_asset, log_hash):
    require(receipt.get("schema_version") == 1 and receipt.get("policy") == "cliproxy-native-cgo-v1", "wrong native receipt policy")
    require(receipt.get("binding") == binding and receipt.get("draft_id") == draft_id and receipt.get("arch") == arch,
            "stale native receipt binding")
    runtime = receipt.get("runtime", {})
    require(receipt.get("status") == "success" and runtime.get("system") == "Darwin" and runtime.get("machine") in ("arm64", "x86_64") and
            runtime.get("go", "").startswith("go version go1.26.4 darwin/") and runtime == receipt.get("runtime_after"), "native runtime changed or missing")
    require(receipt.get("commands") == ["pinned model catalog refresh", "CGO_ENABLED=1 go build ./cmd/server/", "native packaged cli-proxy-api -h"], "native workload incomplete")
    require(receipt.get("models_commit") == record["models_commit"] and set(receipt.get("model_inputs", {})) == set(MODELS) and
            all(re.fullmatch(r"[0-9a-f]{64}", value) for value in receipt["model_inputs"].values()), "native model input closure mismatch")
    expected = {"id": archive_asset["id"], "name": archive_asset["name"], "digest": archive_asset.get("digest")}
    require(receipt.get("archive") == expected, "native archive custody mismatch")
    require(receipt.get("log") == {"id": log_asset["id"], "sha256": log_hash}, "native log custody mismatch")


def native(args):
    source = clean_source()
    binding = producer(args.run, args.attempt)
    require(source == binding["source_sha"], "native source is not exact producer source")
    draft = api(f"repos/{REPO}/releases/{args.draft}")
    record = validate_draft(draft, binding)
    require(api("user")["id"] == OPERATOR, "founder authenticated operator required")
    names = (archive_name(record["tag"].removeprefix("v"), args.arch), evidence_name(args.arch, "log"), evidence_name(args.arch, "json"))
    require(not any(a.get("name") in names for a in draft["assets"]), "native handoff already exists; do not clobber")
    runtime = native_runtime(args.go)
    args.output.mkdir(parents=True, exist_ok=False)
    log_path = args.output / names[1]
    with log_path.open("wb") as log:
        archive, inputs, build_date = native_build(source, record["models_commit"], record["tag"].removeprefix("v"), args.arch, runtime, args.output, log)
    require(clean_source() == source and producer(args.run, args.attempt) == binding and native_runtime(args.go) == runtime, "source/producer/runtime changed during native work")
    run(["gh", "release", "upload", draft["tag_name"], str(archive), str(log_path), "--repo", REPO])
    current = api(f"repos/{REPO}/releases/{args.draft}")
    require(validate_draft(current, binding) == record, "draft changed during native work")
    archive_asset, log_asset = one_asset(current, names[0], True), one_asset(current, names[1], True)
    require(archive_asset.get("digest") == "sha256:" + digest(archive) and log_asset.get("digest") == "sha256:" + digest(log_path), "uploaded native assets changed")
    receipt = {"schema_version": 1, "policy": "cliproxy-native-cgo-v1", "binding": binding, "draft_id": args.draft, "arch": args.arch,
               "runtime": runtime, "runtime_after": native_runtime(args.go), "status": "success", "models_commit": record["models_commit"],
               "model_inputs": inputs, "build_date": build_date,
               "archive": {"id": archive_asset["id"], "name": archive_asset["name"], "digest": archive_asset["digest"]},
               "log": {"id": log_asset["id"], "sha256": digest(log_path)},
               "commands": ["pinned model catalog refresh", "CGO_ENABLED=1 go build ./cmd/server/", "native packaged cli-proxy-api -h"]}
    path = args.output / names[2]
    path.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    run(["gh", "release", "upload", draft["tag_name"], str(path), "--repo", REPO])
    print("Authenticated native " + args.arch + " handoff uploaded; both architectures still gate publication.")


def verify(args):
    binding = producer(args.run, args.attempt)
    require(clean_source() == binding["source_sha"], "continuation source must equal current producer/default source")
    draft = api(f"repos/{REPO}/releases/{args.draft}")
    record = validate_draft(draft, binding)
    require(api(f"repos/{REPO}/commits/{record['tag']}")["sha"] == binding["source_sha"], "release tag source changed")
    args.output.mkdir(parents=True, exist_ok=False)
    saved_assets = []
    for artifact in binding["artifacts"]:
        archive = download_artifact(artifact, args.output)
        asset = one_asset(draft, archive.name)
        require(asset.get("digest") == "sha256:" + digest(archive), "draft non-Darwin asset differs from original producer artifact")
        saved_assets.append(asset)
    for arch in ("amd64", "arm64"):
        archive_asset = one_asset(draft, archive_name(record["tag"].removeprefix("v"), arch), True)
        log_asset = one_asset(draft, evidence_name(arch, "log"), True)
        receipt_asset = one_asset(draft, evidence_name(arch, "json"), True)
        for asset in (archive_asset, log_asset, receipt_asset):
            download_asset(asset, args.output / asset["name"])
        receipt = json.loads((args.output / receipt_asset["name"]).read_text())
        validate_receipt(receipt, binding, args.draft, arch, record, archive_asset, log_asset, digest(args.output / log_asset["name"]))
        saved_assets.extend((archive_asset, log_asset, receipt_asset))
    archives = sorted(path for path in args.output.iterdir() if path.name.startswith("CLIProxyAPI_") and path.suffix in (".gz", ".zip"))
    version = record["tag"].removeprefix("v")
    expected = {f"CLIProxyAPI_{version}_{target}.{extension}" for target, extension in (
        ("windows_amd64", "zip"), ("windows_aarch64", "zip"), ("linux_amd64", "tar.gz"), ("linux_aarch64", "tar.gz"),
        ("linux_amd64_no-plugin", "tar.gz"), ("linux_aarch64_no-plugin", "tar.gz"), ("freebsd_amd64", "tar.gz"),
        ("freebsd_aarch64_no-plugin", "tar.gz"), ("darwin_amd64", "tar.gz"), ("darwin_aarch64", "tar.gz"))}
    require({path.name for path in archives} == expected, "incomplete release architecture inventory")
    if args.compare:
        require(all((args.compare / path.name).is_file() and digest(args.compare / path.name) == digest(path) for path in archives), "assembled archives differ from qualified bytes")
    final = api(f"repos/{REPO}/releases/{args.draft}")
    require(validate_draft(final, binding) == record and producer(args.run, args.attempt) == binding and
            all(one_asset(final, asset["name"]) == asset for asset in saved_assets), "native custody or producer raced")
    (args.output / "checksums.txt").write_text("".join(f"{digest(path)}  {path.name}\n" for path in archives))
    github_outputs({"tag": record["tag"], "handoff_tag": draft["tag_name"], "draft_id": args.draft})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "native", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--run", type=int, required=True)
        command.add_argument("--attempt", type=int, required=True)
        if name == "prepare":
            command.add_argument("--notes", type=Path, required=True)
        else:
            command.add_argument("--draft", type=int, required=True)
            command.add_argument("--output", type=Path, required=True)
        if name == "native":
            command.add_argument("--arch", choices=("amd64", "arm64"), required=True)
            command.add_argument("--go", default="go", help="absolute existing Go 1.26.4 binary; no automatic download/install")
        if name == "verify":
            command.add_argument("--compare", type=Path)
    local = commands.add_parser("build-local")
    local.add_argument("--arch", choices=("amd64", "arm64"), required=True)
    local.add_argument("--go", required=True)
    local.add_argument("--models-commit", required=True)
    local.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "build-local":
        source = clean_source()
        require(re.fullmatch(r"[0-9a-f]{40}", args.models_commit), "invalid pinned catalog source")
        runtime = native_runtime(args.go)
        args.output.mkdir(parents=True, exist_ok=False)
        with (args.output / "build.log").open("wb") as log:
            archive, inputs, date = native_build(source, args.models_commit, "0.0.0-qualification-" + source[:7], args.arch, runtime, args.output, log)
        require(clean_source() == source and native_runtime(args.go) == runtime, "source/runtime changed")
        print(json.dumps({"source": source, "runtime": runtime, "archive": archive.name, "sha256": digest(archive), "model_inputs": inputs, "build_date": date, "status": "success"}))
    else:
        {"prepare": prepare, "native": native, "verify": verify}[args.command](args)


if __name__ == "__main__":
    try:
        main()
    except (Refused, subprocess.CalledProcessError, OSError, ValueError, KeyError, tarfile.TarError, zipfile.BadZipFile) as error:
        print("Native release refused: " + str(error), file=sys.stderr)
        sys.exit(1)

