# metplus-action-linkcheck

METplus Sphinx Linkcheck Action

A GitHub Composite Action used to run Sphinx's linkcheck builder
across METplus component repositories to catch broken links in
documentation. This action automates Python setup, dependency
installation, and broken link identification with configurable
failure thresholds.

## Version History

### v1
* **2026-07-08**
* Initial version

---

## Description

This composite action:
* Sets up a Python environment with the specified version.
* Installs Sphinx and documentation dependencies via a pip requirements file (or falls back to basic Sphinx installation).
* Optionally installs the current repository package in editable mode if required by Sphinx's `conf.py`.
* Runs the Sphinx `linkcheck` builder against the specified documentation source tree.
* Evaluates the output to detect broken links and exposes the finding as an action output.
* Optionally fails the build when broken links are found.
* Optionally uploads the detailed linkcheck output text files as a workflow artifact.

---

## Inputs

| Input | Description | Required | Default |
| :--- | :--- | :---: | :--- |
| **docs-path** | Path to the Sphinx docs directory (containing `conf.py`). | No | `docs` |
| **requirements-file** | Path to a pip requirements file for building the docs. | No | `docs/requirements.txt` |
| **python-version** | Python version to use. | No | `3.12` |
| **install-package** | If `"true"`, pip install the repository itself (editable) before building docs. Needed for repos whose `conf.py` imports the package directly (e.g. to read `__version__`). | No | `false` |
| **fail-on-broken-links** | If `"true"`, the action fails when linkcheck reports broken links. | No | `true` |
| **upload-artifact** | If `"true"`, upload the linkcheck output as a workflow artifact. | No | `true` |
| **artifact-name** | Name for the uploaded linkcheck artifact. | No | `linkcheck-output` |

## Outputs

| Output | Description |
| :--- | :--- |
| **broken-links-found** | `"true"` if linkcheck reported any broken links, otherwise `"false"`. |

---

## Examples

### Minimum Configuration

This example shows the minimum configuration, assuming your documentation is in the default `docs/` directory with a standard `requirements.txt` file:

```yaml
- name: Check Documentation Links
  uses: dtcenter/metplus-action-linkcheck@v1

