# Releasing

This project uses a `VERSION` file for the current source release and a
`v<version>` Git tag to publish a matching container image.

## 0.1.0 release procedure

Run the checks from a clean or intentionally reviewed worktree:

```bash
test "$(tr -d '\n' < VERSION)" = "0.1.0"
python3 -m py_compile commands.py hconnect_server.py abilities/__init__.py
python3 tests/verify_goldens.py
python3 tests/tests_conditions.py
python3 tests/tests_leaves.py
python3 tests/tests_store.py
python3 tests/tests_card_coverage.py
git diff --check
```

The current focused release checks pass. The broader
`tests/tests_cards_fixes.py` script still reports two known failures
(Incubation Slave egg summoning and Bun'jitsu charge-power stat transfer); it
is not currently part of the GHCR publish gate and should be repaired before
claiming full regression coverage.

Review the complete diff for credentials, local accounts, runtime databases,
client binaries, extracted client assets, and generated artifacts. In
particular, `hconnect.db` is runtime state and must not be made public with
local users, purchases, IP addresses, or campaign progress. Removing a file
from a new commit does not remove it from existing Git history; use a history
rewrite before a public first push if sensitive state has ever been committed.

Create and publish the release tag after the release commit has been reviewed:

```bash
git tag -a v0.1.0 -m "Release 0.1.0"
git push origin master
git push origin v0.1.0
```

The GitHub Actions workflow runs the protocol golden checks and publishes the
Docker image to:

```text
ghcr.io/ianutley/hex-server:0.1.0
```

The tag is deliberately `v0.1.0` for Git release conventions; the workflow's
semver metadata emits the container tag without the `v` prefix. It also emits
`v0.1.0`, a commit-SHA tag, and `latest` for default-branch builds.

## Container deployment

The image contains server code and tests, but not the original client,
`Records/`, or a database snapshot. Supply client-derived data at first
startup with `HEX_GAMEDATA`, or mount a complete `Records/` directory. Put
`HEX_DB_PATH` under a persistent host or task volume so the database and its
SQLite sidecars survive container replacement. At every container start,
`docker/docker_bootstrap.py` applies the current `static.py` schema and
idempotent seeds to that database in place before services start. See the
Docker section in the [README](../README.md) and the operational details in
[HOWTO.md](../HOWTO.md).
