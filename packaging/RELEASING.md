# Releasing

1. Set the new version in `pyproject.toml`, `packaging/flipsheet.spec` (`Version:`) and
   `src/flipsheet/__init__.py`.
2. Commit, then tag and push: `git tag v0.2.0 && git push origin v0.2.0`.

The GitHub workflow (`.github/workflows/release.yml`) builds the RPM on Fedora 43 and 44 and
attaches the packages to a new GitHub release. It also builds on every merged pull request,
without releasing anything; the packages are kept as workflow artifacts.

## Building the RPM locally

```
sudo dnf install rpm-build pyproject-rpm-macros python3-devel desktop-file-utils \
                 python3-PyMuPDF python3-pyside6
packaging/build-rpm.sh
```

The package lands in `~/rpmbuild/RPMS/noarch/`.
