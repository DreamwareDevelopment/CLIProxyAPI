# Controlled-fork native CGO release

This route applies to `DreamwareDevelopment/CLIProxyAPI`. GitHub-hosted macOS
is denied by default. Windows, Linux and FreeBSD builds remain in the original
release workflow. Darwin assets retain native Apple SDK/CGO builds, architecture
names, archive contents and the fixed Go 1.26.4 toolchain.

## Producer and native custody

A release tag creates a unique `native-handoff-RUN_ID-ATTEMPT` **draft**, not a
published release. The producer resolves the model catalog to one immutable
commit and uses it for every build. Existing non-Darwin jobs upload the same
named assets to that draft and to Actions artifacts. Provisional checksum
publication is replaced by one complete native-gated checksum set.

On the exact clean committed source, still current `main`, an authenticated
founder performs each architecture's native handoff under the shared queue:

```bash
dreamware-test run --slots 1 -- dreamware-python scripts/native_release.py native --run RUN_ID --attempt ATTEMPT --draft DRAFT_ID --arch arm64 --go /absolute/path/to/go1.26.4/bin/go --output /tmp/cliproxy-native-arm64-RUN_ID-ATTEMPT
```

Repeat for `--arch amd64` on a Mac route that can actually execute AMD64. The
absolute Go binary may be an isolated official Go 1.26.4 distribution whose
archive SHA-256 and size were verified against official `go.dev` metadata. The
helper never upgrades the global Go installation or automatically installs
Rosetta. An unavailable toolchain/SDK/target executor leaves that native slice
blocked. An ARM Mac requires an existing Rosetta runtime before the AMD64
execution attempt; an Intel Mac cannot qualify an ARM64 execution.

The helper makes an ephemeral source clone, refreshes catalogs at the pinned
commit, and records both generated catalog hashes. It uses native clang/the
Apple SDK with `CGO_ENABLED=1`, checks Mach-O architecture and Go CGO metadata,
creates the original archive layout, unpacks it and executes that packaged
binary with `-h`. Runtime/tool binary identities, committed source and generated
catalog inputs are captured before/after. It uploads the exact archive, log and
bounded success receipt through standard draft-release asset APIs without
clobber. It uses no private Actions upload endpoint or Mac Actions runner.

GitHub's server-observed native asset uploader must be the founder, immutable
user ID `25471746` (`DreamwareDevelopment`). This is authenticated operator
evidence produced by the fixed actual-Darwin helper, not cryptographic OS
attestation. Caller-supplied OS/approval JSON or another maintainer's upload
cannot replace it. Native logs and receipts become release assets when that
qualified draft is later published; the workload uses no real credentials.

## Linux finalization

After **both** native architecture handoffs succeed, run `Finalize native
release` from `main` with the original producer run, attempt and draft IDs.
This is an explicit publication operation. The Linux continuation independently
reads current source/workflow/producer state, exact original Actions artifact
IDs and bytes, native asset IDs/digests and authenticated receipts. It preserves
all ten expected archive names, checks native/generated-input custody and
computes the complete `checksums.txt`. It repeats those checks immediately
before publishing the same draft under the original existing release tag.

Missing/failed/expired/ambiguous producer evidence, changed source/tag/attempt,
wrong native uploader, altered bytes, missing architectures or unavailable
native execution refuses publication. Linux/Windows/FreeBSD builds can still
complete while native custody is pending. Preserve old attempts; recovery never
silently re-enables a hosted Mac or clobbers original proof. Direct administrator
or API bypass is outside this supported-path control.

## Local qualification without publishing

`build-local` uses the same native builder and exact toolchain without creating
drafts, uploading evidence, dispatching jobs or publishing a release:

```bash
dreamware-test run --slots 1 -- dreamware-python scripts/native_release.py build-local --arch arm64 --go /absolute/path/to/go1.26.4/bin/go --models-commit PINNED_COMMIT --output /tmp/cliproxy-native-qualification
dreamware-test run --slots 1 -- dreamware-python -B -m unittest discover -s scripts -p 'test_native_release.py' -v
```

Use the actual immutable catalog commit returned by the official models source.
The refusal controls have named assertion mutations on disposable copies.
Kody, CI, source ownership and exact-head ruler release remain separate gates.
