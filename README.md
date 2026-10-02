# metplus-action-linkcheck

METplus Sphinx Linkcheck Action

A GitHub Composite Action used to run Sphinx's linkcheck builder
across METplus component repositories to catch broken links in
documentation. This action automates Python setup, dependency
installation, and broken link identification with configurable
failure thresholds.

## Version History

### v1.6.0
* **2026-10-01**
* Re-check transient link failures before failing (#1)
* Report broken links as annotations and in the job summary
* Detect broken links from linkcheck-output.json
* Cache pip downloads; document Python 3.12 limit

### v1
* **2026-07-08**
* Initial version

---

## Description

This composite action:
* Sets up a Python environment with the specified version, caching pip downloads when a requirements file is found.
* Installs Sphinx and documentation dependencies via a pip requirements file (or falls back to basic Sphinx installation).
* Optionally installs the current repository package in editable mode if required by Sphinx's `conf.py`.
* Runs the Sphinx `linkcheck` builder against the specified documentation source tree.
* Classifies broken links as permanent or transient, re-checking transient failures after a delay (see [Broken Link Handling](#broken-link-handling)).
* Reports each broken link as an annotation on the file and line where it appears, and writes a results table to the job summary.
* Exposes whether any links are still broken as an action output.
* Optionally fails the build when broken links are found.
* Optionally uploads the detailed linkcheck output text files as a workflow artifact.

---

## Inputs

| Input | Description | Required | Default |
| :--- | :--- | :---: | :--- |
| **docs-path** | Path to the Sphinx docs directory (containing `conf.py`). | No | `docs` |
| **requirements-file** | Path to a pip requirements file for building the docs. | No | `docs/requirements.txt` |
| **python-version** | Python version to use. Must be `3.12` or older for repos that pin Sphinx < 6 (e.g. `sphinx==5.3.0`), since Sphinx 5.x imports the `imghdr` module that was removed in Python 3.13. | No | `3.12` |
| **install-package** | If `"true"`, pip install the repository itself (editable) before building docs. Needed for repos whose `conf.py` imports the package directly (e.g. to read `__version__`). | No | `false` |
| **fail-on-broken-links** | If `"true"`, the action fails when linkcheck reports broken links. | No | `true` |
| **recheck-attempts** | Number of times to re-check links that fail with a transient error. Use `0` to disable re-checks. | No | `2` |
| **recheck-delay** | Seconds to wait before the first re-check. The wait doubles before each additional re-check. | No | `15` |
| **fail-on-transient** | If `"true"`, links that still fail with a transient error after all re-checks are reported as errors. If `"false"`, they are reported as warnings instead. | No | `true` |
| **upload-artifact** | If `"true"`, upload the linkcheck output as a workflow artifact. | No | `true` |
| **artifact-name** | Name for the uploaded linkcheck artifact. | No | `linkcheck-output` |

## Outputs

| Output | Description |
| :--- | :--- |
| **broken-links-found** | `"true"` if any links are still broken after re-checks (including transient failures reported as warnings when `fail-on-transient` is `"false"`), otherwise `"false"`. |

---

## Broken Link Handling

Sphinx `linkcheck` reports every failure as `broken`, and its `linkcheck_retries` setting retries immediately with no delay. To avoid failing jobs on short network outages, this action reads `linkcheck-output.json` and classifies each broken link:

| Category | Errors | Result |
| :--- | :--- | :--- |
| **Permanent** | HTTP 4xx (except 429), `Anchor '...' not found`, host name not found, TLS/SSL certificate errors | Reported as an error |
| **Transient** | Connection errors, timeouts, HTTP 429, HTTP 5xx, temporary DNS failures, and any other error | Re-checked |

Transient failures are re-checked up to `recheck-attempts` times, waiting `recheck-delay` seconds before the first re-check and doubling the wait each time (15s, then 30s by default). Re-checks request the URL without its anchor, using the same User-Agent as Sphinx.

* Links that **recover** are reported as warnings.
* Links that are **still unreachable** after all re-checks are reported as errors, or as warnings if `fail-on-transient` is `"false"`.
* A link that returns a permanent error during a re-check (e.g. 404) is reported as an error.

The job fails only when errors are reported and `fail-on-broken-links` is `"true"`. The job also fails if `sphinx-build` itself fails for reasons other than broken links.

Each error and warning is shown as an annotation on the file and line containing the link, and all results are listed in the job summary.

---

## Examples

### Minimum Configuration

This example shows the minimum configuration, assuming your documentation is in the default `docs/` directory with a standard `requirements.txt` file:

```yaml
- name: Check Documentation Links
  uses: dtcenter/metplus-action-linkcheck@v1

