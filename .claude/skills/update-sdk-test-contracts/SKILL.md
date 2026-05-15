---
name: update-sdk-test-contracts
description: Update the SDK test service implementations to match a new version of the e2e conformance contracts. Use when the user says "update sdk tests", "update test contracts", or gives a specific e2e release tag.
user-invocable: true
---

# Updating SDK Test Service Implementations to a New Contract Version

## Step 1: Read the release notes

The user will provide a release tag (e.g. `v2.0`). Fetch the full release notes from GitHub:

```bash
gh release view v2.0 --repo restatedev/e2e
```

The release body describes every contract change — new commands, removed handlers, field names, return value conventions. **Read it entirely before writing any code.**

The release notes document is also available in the e2e repo at `sdk-tests/releases/<version>.md`.

## Step 2: Find the test service implementations

This SDK's test services live under `test-services/`. Search for the relevant files:

```bash
# Find VirtualObjectCommandInterpreter implementation
grep -r "VirtualObjectCommandInterpreter" test-services/ -l

# Find TestUtilsService implementation
grep -r "TestUtilsService" test-services/ -l
```

## Step 3: Implement the changes

For each contract change described in the release notes, update the corresponding implementation file. The release notes specify:
- Exact JSON `type` discriminator strings for new commands (match them exactly — they are case-sensitive)
- Exact JSON field names for request bodies
- Return value conventions (pipe-joining, `ok:`/`err:` prefixes, etc.)

## Step 4: Update the version in the script

After implementing, bump the version in `.github/workflows/integration.yaml` to the new tag. The script `.tools/run-sdk-tests.sh` reads from there automatically.

## Step 5: Build and verify

```bash
# Build and run all new test classes mentioned in the release notes
./.tools/run-sdk-tests.sh --test-suite=default --test-name=<TestClass>

# Or run the full default suite
./.tools/run-sdk-tests.sh
```

All tests must pass before the update is complete.
