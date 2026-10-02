# metplus-action-linkcheck

METplus Sphinx Linkcheck Action

A GitHub Composite Action used to run Sphinx's linkcheck builder
across METplus component repositories to catch broken links in
documentation. This action automates Python setup, dependency
installation, and broken link identification with configurable
failure thresholds.

## Version History

### v1.6.0
* **2026-10-02**
* Re-check transient link failures before failing (#1)
* Report broken links in the job summary, with a single annotation giving the number of failing links
* Detect broken links from linkcheck-output.json
* Cache pip downloads; document Python 3.12 limit
* Check relative links against the documents in the Sphinx build (#3)
* Show the Read the Docs URL of broken relative links for the current branch
* Re-check and report links that time out
* List ignored links in a collapsed table in the job summary

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
* Checks relative links against the documents in the Sphinx build (see [Relative Links](#relative-links)).
* Writes a results table to the job summary, along with a collapsed table of ignored links, and adds a single annotation giving the number of failing links.
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
| **rtd-url** | Read the Docs URL with a `{version}` placeholder, e.g. `https://metplus.readthedocs.io/en/{version}/`. If set, broken relative links are reported with their Read the Docs URL for the current branch (see [Relative Links](#relative-links)). | No | `''` |
| **upload-artifact** | If `"true"`, upload the linkcheck output as a workflow artifact. | No | `true` |
| **artifact-name** | Name for the uploaded linkcheck artifact. | No | `linkcheck-output` |

## Outputs

| Output | Description |
| :--- | :--- |
| **broken-links-found** | `"true"` if any links are still broken after re-checks (including transient failures reported as warnings when `fail-on-transient` is `"false"`), otherwise `"false"`. |

---

## Broken Link Handling

Sphinx `linkcheck` reports most failures as `broken` (Sphinx 8.x reports timeouts as `timeout`), and its `linkcheck_retries` setting retries immediately with no delay. To avoid failing jobs on short network outages, this action reads `linkcheck-output.json` and classifies each broken link:

| Category | Errors | Result |
| :--- | :--- | :--- |
| **Permanent** | HTTP 4xx (except 429), `Anchor '...' not found`, host name not found, TLS/SSL certificate errors, malformed URLs (e.g. `https://` with no host, or a repeated scheme such as `https://https://...`) | Reported as an error |
| **Transient** | Connection errors, timeouts (including the `timeout` status), HTTP 429, HTTP 5xx, temporary DNS failures, and any other error | Re-checked |

Transient failures are re-checked up to `recheck-attempts` times, waiting `recheck-delay` seconds before the first re-check and doubling the wait each time (15s, then 30s by default). Re-checks request the URL without its anchor, using the same User-Agent as Sphinx.

* Links that **recover** are reported as warnings.
* Links that are **still unreachable** after all re-checks are reported as errors, or as warnings if `fail-on-transient` is `"false"`.
* A link that returns a permanent error during a re-check (e.g. 404) is reported as an error.

The job fails only when errors are reported and `fail-on-broken-links` is `"true"`. The job also fails if `sphinx-build` itself fails for reasons other than broken links.

All errors and warnings are listed in the job summary, with the file and line containing each link, and in the step log. If any links fail, a single annotation reports how many (e.g. `55 of 464 links failed. See the job summary for details.`). It is an error if `fail-on-broken-links` is `"true"`, otherwise a warning.

Links that Sphinx does not check (those matching `linkcheck_ignore`, and in Sphinx 8.x, those returning `503 Service Unavailable`) are listed with clickable URLs in a collapsed **Ignored Links** table in the job summary, so they can be checked by hand. They do not affect the job result.

---

## Relative Links

Relative links such as `` `Use Case <../generated/met_tool_wrapper/StatAnalysis/StatAnalysis.html>`_ `` keep readers on the version of the docs they are viewing on Read the Docs. Sphinx `linkcheck` checks them against the **source** tree, where `.html` files never exist, so it reports them as broken.

Instead, this action checks them against the documents read by the Sphinx build, including pages generated by extensions such as sphinx-gallery. The list of documents is read from `_build/linkcheck/.doctrees/environment.pickle`, or found by searching `docs-path` for source files if that cannot be loaded.

* The link is resolved against the directory of the document containing it, ignoring any query string or anchor.
* A link to `X.html` (or `X/`, meaning `X/index.html`) passes if `X` is a document in the build. Links to `search.html`, `genindex.html`, and `py-modindex.html` always pass, since the HTML builder creates them.
* Any other broken relative link (e.g. a missing `../_static/example.pdf`) is reported as an error. Relative links are never re-checked over the network.
* Anchors on relative links are not checked.

If `rtd-url` is set, each broken relative link is also shown with its Read the Docs URL for the branch being built. `{version}` is replaced with the branch name (`GITHUB_HEAD_REF` for pull requests, otherwise `GITHUB_REF_NAME`), converted the way Read the Docs names versions: lowercase, with characters other than `a-z`, `0-9`, `.`, `_`, and `-` replaced by `-`. This URL is shown for convenience and is not requested, so it may not exist yet if Read the Docs has not finished building the branch.

---

## Examples

### Minimum Configuration

This example shows the minimum configuration, assuming your documentation is in the default `docs/` directory with a standard `requirements.txt` file:

```yaml
- name: Check Documentation Links
  uses: dtcenter/metplus-action-linkcheck@v1
```

### Read the Docs URLs for Relative Links

This example shows broken relative links with their Read the Docs URL for the current branch:

```yaml
- name: Check Documentation Links
  uses: dtcenter/metplus-action-linkcheck@v1
  with:
    rtd-url: 'https://metplus.readthedocs.io/en/{version}/'
```
