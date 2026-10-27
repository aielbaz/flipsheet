#!/bin/bash
# Build the RPM from the working tree: packaging/build-rpm.sh
set -euo pipefail
# a PYTHONPATH from the caller would stop %pyproject_check_import finding the built package
unset PYTHONPATH
cd "$(dirname "$0")/.."
version=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)
spec_version=$(sed -n 's/^Version:[[:space:]]*//p' packaging/flipsheet.spec | head -1)
if [ "$version" != "$spec_version" ]; then
    echo "error: pyproject.toml says $version but packaging/flipsheet.spec says $spec_version" >&2
    exit 1
fi
top=${RPMBUILD_TOPDIR:-$HOME/rpmbuild}
mkdir -p "$top"/{SOURCES,SPECS}
tar --transform "s,^,flipsheet-$version/," -czf "$top/SOURCES/flipsheet-$version.tar.gz" \
    --exclude=__pycache__ --exclude=.venv --exclude=.git --exclude='*.egg-info' \
    LICENSE README.md pyproject.toml src data
cp packaging/flipsheet.spec "$top/SPECS/"
rpmbuild --define "_topdir $top" "$@" -ba "$top/SPECS/flipsheet.spec"
echo "Built: $(ls "$top"/RPMS/noarch/flipsheet-"$version"-*.rpm)"
