# Releasing

Releases are automated with [release-please](https://github.com/googleapis/release-please).

1. Merge pull requests into `main` using conventional commit messages (`feat: ...`,
   `fix: ...`, `docs: ...`, and so on). When squash-merging, the PR title is the message.
2. release-please keeps a "release PR" open on `main`. It contains the next version, the
   changelog, and the version bumps in `pyproject.toml`, `src/flipsheet/__init__.py` and
   `packaging/flipsheet.spec`.
3. Merge the release PR. That creates the `vX.Y.Z` tag and the GitHub release, then the
   workflow builds the RPM on Fedora 43 and 44 and attaches the packages to the release.

## One-time repository setup

In the repository settings, under Actions → General → Workflow permissions, turn on
"Allow GitHub Actions to create and approve pull requests". release-please needs it to open
the release PR.

## Version numbers

The version lives in three files, and release-please updates all of them. The marker comments
(`x-release-please-*`) tell it where. Don't remove them. `packaging/build-rpm.sh` refuses to
build if `pyproject.toml` and the spec disagree.

## Building the RPM locally

```
sudo dnf install rpm-build pyproject-rpm-macros python3-devel desktop-file-utils \
                 python3-PyMuPDF python3-pyside6
packaging/build-rpm.sh
```

The package lands in `~/rpmbuild/RPMS/noarch/`.
