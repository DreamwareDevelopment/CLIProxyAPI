"""Controlled-fork native CGO release custody refusal controls."""
import copy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from scripts import native_release as native


class NativeReleaseTest(unittest.TestCase):
    def setUp(self):
        self.meta = {"id":100,"run_attempt":2,"repository":{"full_name":native.REPO},"head_repository":{"full_name":native.REPO},"path":native.WORKFLOW,"event":"push","head_sha":"a"*40,"head_branch":"v2.1.0","workflow_id":123,"status":"completed","conclusion":"success"}
        self.jobs = [{"name":name,"status":"completed","conclusion":"success"} for name in native.JOBS]
        self.artifacts = [{"id":i+1,"name":name,"digest":"sha256:"+"b"*64,"expired":False,"workflow_run":{"id":100,"head_sha":"a"*40}} for i,name in enumerate(native.ARTIFACTS)]
        self.binding = self.producer()
        self.binding["workflow_blob"] = "c"*40
        self.record = {"schema_version":1,"seed":{key:self.binding[key] for key in ("repo","run_id","attempt","source_sha","workflow_blob")},"tag":"v2.1.0","models_commit":"d"*40}
        self.asset = {"id":42,"name":"native-darwin-amd64.log","size":100,"state":"uploaded","uploader":{"id":native.OPERATOR}}
        self.archive = dict(self.asset,id=43,name="CLIProxyAPI_2.1.0_darwin_amd64.tar.gz",digest="sha256:"+"e"*64)
        self.draft = {"id":7,"draft":True,"target_commitish":"a"*40,"tag_name":"native-handoff-100-2","body":"release notes"+native.PREFIX+json.dumps(self.record)+native.SUFFIX,"assets":[self.asset]}
        runtime = {"system":"Darwin","machine":"arm64","go":"go version go1.26.4 darwin/arm64","go_path":"/fixture/go","go_sha256":"1"*64,"clang_path":"/fixture/clang","clang_sha256":"2"*64,"sdk":"/fixture/sdk","python_path":"/fixture/python","python_sha256":"3"*64}
        self.receipt = {"schema_version":1,"policy":"cliproxy-native-cgo-v1","binding":self.binding,"draft_id":7,"arch":"amd64","runtime":runtime,"runtime_after":copy.deepcopy(runtime),"status":"success","models_commit":"d"*40,"model_inputs":{path:"f"*64 for path in native.MODELS},"archive":{key:self.archive[key] for key in ("id","name","digest")},"log":{"id":42,"sha256":"4"*64},"commands":["pinned model catalog refresh","CGO_ENABLED=1 go build ./cmd/server/","native packaged cli-proxy-api -h"]}

    def producer(self):
        return native.validate_producer(self.meta,self.jobs,self.artifacts,100,2)

    def check_receipt(self):
        native.validate_receipt(self.receipt,self.binding,7,"amd64",self.record,self.archive,self.asset,"4"*64)

    def test_attempt_race(self):
        self.meta["run_attempt"] = 3
        with self.assertRaisesRegex(native.Refused, "attempt changed"):
            self.producer()

    def test_fork_producer(self):
        self.meta["head_repository"]["full_name"] = "other/fork"
        with self.assertRaisesRegex(native.Refused, "repository"):
            self.producer()

    def test_workflow_identity(self):
        self.meta["path"] = ".github/workflows/other.yml"
        with self.assertRaisesRegex(native.Refused, "workflow"):
            self.producer()

    def test_incomplete_producer(self):
        self.meta["status"] = "in_progress"
        with self.assertRaisesRegex(native.Refused, "did not succeed"):
            self.producer()

    def test_failed_required_job(self):
        self.jobs[0]["conclusion"] = "failure"
        with self.assertRaisesRegex(native.Refused, "unsuccessful job"):
            self.producer()

    def test_duplicate_artifact(self):
        self.artifacts[-1] = copy.deepcopy(self.artifacts[0])
        with self.assertRaisesRegex(native.Refused, "artifact inventory"):
            self.producer()

    def test_artifact_source(self):
        self.artifacts[0]["workflow_run"]["head_sha"] = "f" * 40
        with self.assertRaisesRegex(native.Refused, "source mismatch"):
            self.producer()

    def test_expired_artifact(self):
        self.artifacts[0]["expired"] = True
        with self.assertRaisesRegex(native.Refused, "expired artifact"):
            self.producer()

    def test_missing_artifact_digest(self):
        self.artifacts[0]["digest"] = None
        with self.assertRaisesRegex(native.Refused, "server digest"):
            self.producer()

    def test_default_source_move(self):
        pages = [self.meta, {"total_count": len(self.jobs), "jobs": self.jobs},
                 {"total_count": len(self.artifacts), "artifacts": self.artifacts},
                 {"type": "file", "sha": "c" * 40}, {"sha": "f" * 40}]
        with patch.object(native, "api", side_effect=pages):
            with self.assertRaisesRegex(native.Refused, "current default source"):
                native.producer(100, 2)

    def test_published_draft(self):
        self.draft["draft"] = False
        with self.assertRaisesRegex(native.Refused, "published draft"):
            native.validate_draft(self.draft, self.binding)

    def test_receipt_author(self):
        self.asset["uploader"]["id"] = 41898282
        with self.assertRaisesRegex(native.Refused, "founder custody"):
            native.one_asset(self.draft, self.asset["name"], True)

    def test_duplicate_evidence(self):
        self.draft["assets"].append(copy.deepcopy(self.asset))
        with self.assertRaisesRegex(native.Refused, "ambiguous native asset"):
            native.one_asset(self.draft, self.asset["name"], True)

    def test_download_asset_digest(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "receipt"
            asset = dict(self.asset, size=4, digest="sha256:" + "0" * 64)
            def download(_remote, destination, _limit):
                destination.write_bytes(b"test")
            with patch.object(native, "download", side_effect=download):
                with self.assertRaisesRegex(native.Refused, "digest mismatch"):
                    native.download_asset(asset, path)

    def test_valid_controls(self):
        self.assertEqual(self.producer()["source_sha"], "a" * 40)
        self.assertEqual(native.validate_draft(self.draft, self.binding), self.record)
        self.check_receipt()

    def test_draft_binding(self):
        self.record["seed"]["attempt"] = 1
        self.draft["body"] = "notes" + native.PREFIX + json.dumps(self.record) + native.SUFFIX
        with self.assertRaisesRegex(native.Refused, "producer binding"):
            native.validate_draft(self.draft, self.binding)

    def test_receipt_binding(self):
        self.receipt["draft_id"] = 8
        with self.assertRaisesRegex(native.Refused, "receipt binding"):
            self.check_receipt()

    def test_runtime_toolchain(self):
        self.receipt["runtime"]["go"] = "go version go1.27.1 darwin/arm64"
        self.receipt["runtime_after"]["go"] = self.receipt["runtime"]["go"]
        with self.assertRaisesRegex(native.Refused, "runtime"):
            self.check_receipt()

    def test_archive_custody(self):
        self.receipt["archive"]["id"] += 1
        with self.assertRaisesRegex(native.Refused, "archive custody"):
            self.check_receipt()

    def test_model_input_closure(self):
        self.receipt["model_inputs"].pop(native.MODELS[0])
        with self.assertRaisesRegex(native.Refused, "model input closure"):
            self.check_receipt()

    def test_incomplete_workload(self):
        self.receipt["commands"].pop()
        with self.assertRaisesRegex(native.Refused, "workload incomplete"):
            self.check_receipt()

    def test_log_binding(self):
        self.receipt["log"]["id"] += 1
        with self.assertRaisesRegex(native.Refused, "log custody"):
            self.check_receipt()

    def test_actual_platform_required(self):
        with patch.object(native.platform, "system", return_value="Linux"), patch.object(native.platform, "machine", return_value="arm64"), patch.dict(native.os.environ, {"DREAMWARE_TEST_ACTIVE":"1"}), patch.object(native.shutil, "which", return_value="/fixture/go"), patch.object(native.subprocess, "check_output", return_value="go version go1.26.4 darwin/arm64"), patch.object(native, "output", return_value="/fixture/tool"), patch.object(native, "digest", return_value="a" * 64):
            with self.assertRaisesRegex(native.Refused, "actual native Mac"):
                native.native_runtime("go")

    def test_exact_existing_toolchain(self):
        with patch.object(native.platform, "system", return_value="Darwin"), patch.object(native.platform, "machine", return_value="arm64"), patch.dict(native.os.environ, {"DREAMWARE_TEST_ACTIVE":"1"}), patch.object(native.shutil, "which", return_value="/fixture/go"), patch.object(native.subprocess, "check_output", return_value="go version go1.27.1 darwin/arm64"), patch.object(native, "output", return_value="/fixture/tool"), patch.object(native, "digest", return_value="a" * 64):
            with self.assertRaisesRegex(native.Refused, "exact existing Go 1.26.4"):
                native.native_runtime("go")

    def test_no_rosetta_install(self):
        with patch.object(native.Path, "is_file", return_value=False):
            with self.assertRaisesRegex(native.Refused, "amd64 execution unavailable"):
                native.execution_route("arm64", "amd64")

    def test_native_arm_route(self):
        with self.assertRaisesRegex(native.Refused, "actual ARM Mac"):
            native.execution_route("x86_64", "arm64")

    def test_archive_identities(self):
        self.assertEqual(native.archive_name("2.1.0", "amd64"), "CLIProxyAPI_2.1.0_darwin_amd64.tar.gz")
        self.assertEqual(native.archive_name("2.1.0", "arm64"), "CLIProxyAPI_2.1.0_darwin_aarch64.tar.gz")

    def verification(self, scratch, *, original_wrong=False, native_author_wrong=False, raced=False, tag_moved=False):
        import hashlib
        def hashed(data):
            return "sha256:" + hashlib.sha256(data).hexdigest()
        contents, assets = {}, []
        targets = {"windows-amd64":"windows_amd64.zip", "windows-arm64":"windows_aarch64.zip",
                   "linux-amd64":"linux_amd64.tar.gz", "linux-arm64":"linux_aarch64.tar.gz",
                   "linux-amd64-no-plugin":"linux_amd64_no-plugin.tar.gz", "linux-arm64-no-plugin":"linux_aarch64_no-plugin.tar.gz",
                   "freebsd-amd64":"freebsd_amd64.tar.gz", "freebsd-arm64-no-plugin":"freebsd_aarch64_no-plugin.tar.gz"}
        for index, artifact in enumerate(self.binding["artifacts"]):
            name = "CLIProxyAPI_2.1.0_" + targets[artifact["name"]]
            data = artifact["name"].encode()
            contents[name] = data
            assets.append(dict(self.asset, id=100+index, name=name, size=len(data), digest=hashed(data)))
        if original_wrong:
            assets[0]["digest"] = "sha256:" + "0" * 64
        for arch, offset in (("amd64", 200), ("arm64", 300)):
            archive_name = native.archive_name("2.1.0", arch)
            log_name, receipt_name = native.evidence_name(arch, "log"), native.evidence_name(arch, "json")
            contents[archive_name] = ("native " + arch).encode()
            contents[log_name] = ("native log " + arch).encode()
            archive = dict(self.asset, id=offset, name=archive_name, size=len(contents[archive_name]), digest=hashed(contents[archive_name]))
            log = dict(self.asset, id=offset+1, name=log_name, size=len(contents[log_name]), digest=hashed(contents[log_name]))
            receipt = copy.deepcopy(self.receipt)
            receipt.update(arch=arch, archive={key:archive[key] for key in ("id","name","digest")},
                           log={"id":log["id"], "sha256":hashed(contents[log_name])[7:]})
            contents[receipt_name] = json.dumps(receipt).encode()
            proof = dict(self.asset, id=offset+2, name=receipt_name, size=len(contents[receipt_name]), digest=hashed(contents[receipt_name]))
            if native_author_wrong and arch == "arm64":
                archive["uploader"] = {"id":41898282}
            assets.extend((archive,log,proof))
        self.draft["assets"] = assets
        after = copy.deepcopy(self.draft)
        if raced:
            after["assets"][0]["id"] += 1
        def download_asset(asset, destination):
            destination.write_bytes(contents[asset["name"]])
        def download_artifact(artifact, directory):
            name = "CLIProxyAPI_2.1.0_" + targets[artifact["name"]]
            path = directory / name
            path.write_bytes(contents[name])
            return path
        args = SimpleNamespace(run=100,attempt=2,draft=7,output=Path(scratch)/"qualified",compare=None)
        tag = {"sha": ("f" if tag_moved else "a") * 40}
        with patch.object(native,"producer",return_value=self.binding), patch.object(native,"clean_source",return_value="a"*40), patch.object(native,"api",side_effect=[self.draft,tag,after,tag]), patch.object(native,"download_asset",side_effect=download_asset), patch.object(native,"download_artifact",side_effect=download_artifact), patch.object(native,"github_outputs"):
            native.verify(args)
        return args

    def test_original_archive_bytes(self):
        with tempfile.TemporaryDirectory() as scratch:
            with self.assertRaisesRegex(native.Refused, "original producer artifact"):
                self.verification(scratch,original_wrong=True)

    def test_native_archive_author(self):
        with tempfile.TemporaryDirectory() as scratch:
            with self.assertRaisesRegex(native.Refused, "founder custody"):
                self.verification(scratch,native_author_wrong=True)

    def test_final_custody_race(self):
        with tempfile.TemporaryDirectory() as scratch:
            with self.assertRaisesRegex(native.Refused, "custody or producer raced"):
                self.verification(scratch,raced=True)

    def test_complete_checksum_inventory(self):
        with tempfile.TemporaryDirectory() as scratch:
            args = self.verification(scratch)
            lines = (args.output/"checksums.txt").read_text().splitlines()
            self.assertEqual(len(lines),10)
            self.assertEqual(len({line.split()[1] for line in lines}),10)

    def test_live_release_tag_binding(self):
        with tempfile.TemporaryDirectory() as scratch:
            with self.assertRaisesRegex(native.Refused, "release tag source changed"):
                self.verification(scratch,tag_moved=True)


if __name__ == "__main__":
    unittest.main()
