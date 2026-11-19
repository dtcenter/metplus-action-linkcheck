# metplus-action-linkcheck

METplus Sphinx Linkcheck Action

A GitHub Composite Action used to run Sphinx's linkcheck builder
across METplus component repositories to catch broken links in
documentation. This action automates Python setup, dependency
installation, and broken link identification with configurable
failure thresholds.

## Version History

### v1.7.0
* **2026-11-16**
* Track the result of every link across runs in a history file on an orphan branch of the repository being checked (#8)
* Fail only on links that have been failing for longer than a grace period, reporting the rest as warnings
* Report when a failing link last passed, how long it has been failing, and when it starts failing the job
* Classify TLS/SSL failures as transient when the history is in use, so that the grace period covers an expired certificate or a refused handshake
* Decide from each run alone, as in v1.6.0, until the history branch holds a usable history

### v1.6.0
* **2026-10-02**
* Re-check transient link failures before failing (#1)
* Report broken links in the job summary, with a single annotation giving the number of failing links
* Detect broken links from linkcheck-output.json
* Cache pip downloads; document Python 3.12 limit
* Check relative links against the documents in the Sphinx build (#3)
* Show the Read the Docs URL of broken relative links for the current branch
* Re-check and report links that time out
* Report malformed URLs as errors without re-checking them
* List failing, warning, and ignored links in collapsed tables in the job summary
* Check DOI links with the DOI API, including links ignored by `linkcheck_ignore`
* Re-check links that return `503`, which Sphinx reports as ignored (#6)
* Check GitHub file links that `github.com` throttles on `raw.githubusercontent.com`

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
* Keeps a history of every link it checks, and fails only on transient failures that have outlasted a grace period (see [Link Check History](#link-check-history)).
* Checks relative links against the documents in the Sphinx build (see [Relative Links](#relative-links)).
* Checks DOI links with the DOI API instead of the publisher's site (see [DOI Links](#doi-links)).
* Checks GitHub file links that `github.com` throttles on `raw.githubusercontent.com` (see [GitHub File Links](#github-file-links)).
* Writes collapsed tables of failing, warning, and ignored links to the job summary, and adds a single annotation giving the number of failing links.
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
| **check-dois** | If `"true"`, check `doi.org` links with the DOI API instead of the publisher's site, including links ignored by `linkcheck_ignore` (see [DOI Links](#doi-links)). | No | `true` |
| **history-branch** | Orphan branch of the repository being checked that holds the link check history (see [Link Check History](#link-check-history)). Leave empty to disable the history, so that every run decides from its own results. | No | `linkcheck-history` |
| **history-file** | Path to the link check history file on the history branch. Several workflows can share one branch by giving each its own file. | No | `linkcheck-history.json` |
| **grace-period** | Days a transiently failing link may keep failing before it fails the job. Permanent failures always fail at once. | No | `14` |
| **min-failures** | Consecutive failed checks required before a link can fail the job. | No | `2` |
| **history-retention** | Days to keep a history record for a URL that is no longer found in the documentation. | No | `90` |
| **update-history** | If `"true"`, write the history back to the history branch. If `"auto"`, write it for every event except `pull_request` and `pull_request_target`. If `"false"`, never write it. | No | `auto` |
| **history-token** | Token used to read and write the history branch. The default `GITHUB_TOKEN` needs `permissions: contents: write` in the calling workflow for the runs that write the history. | No | `${{ github.token }}` |
| **upload-artifact** | If `"true"`, upload the linkcheck output as a workflow artifact. | No | `true` |
| **artifact-name** | Name for the uploaded linkcheck artifact. | No | `linkcheck-output` |

## Outputs

| Output | Description |
| :--- | :--- |
| **broken-links-found** | `"true"` if any links are still broken after re-checks (including transient failures reported as warnings when `fail-on-transient` is `"false"`), otherwise `"false"`. |

---

## Broken Link Handling

Sphinx `linkcheck` reports most failures as `broken` (Sphinx 8.x reports timeouts as `timeout`, and both Sphinx 5.x and 8.x report `503 Service Unavailable` as `ignored`), and its `linkcheck_retries` setting retries immediately with no delay. To avoid failing jobs on short network outages, this action reads `linkcheck-output.json` and classifies each broken link:

| Category | Errors | Result |
| :--- | :--- | :--- |
| **Permanent** | HTTP 4xx (except 429), `Anchor '...' not found`, host name not found, malformed URLs (e.g. `https://` with no host, or a repeated scheme such as `https://https://...`) | Reported as an error |
| **Transient** | Connection errors, timeouts (including the `timeout` status), HTTP 429, HTTP 5xx (including `503` responses that Sphinx reports as `ignored`), TLS/SSL errors when the [link check history](#link-check-history) is in use (otherwise they are permanent), temporary DNS failures, and any other error | Re-checked |

Transient failures are re-checked up to `recheck-attempts` times, waiting `recheck-delay` seconds before the first re-check and doubling the wait each time (15s, then 30s by default). Re-checks request the URL without its anchor, using the same User-Agent as Sphinx.

* Links that **recover** are reported as warnings.
* Links that are **still unreachable** after all re-checks are reported as errors, or as warnings if `fail-on-transient` is `"false"`. When a [link check history](#link-check-history) is available, they are reported as warnings until they have been failing for longer than the grace period.
* A link that returns a permanent error during a re-check (e.g. 404) is reported as an error.

The job fails only when errors are reported and `fail-on-broken-links` is `"true"`. The job also fails if `sphinx-build` itself fails for reasons other than broken links.

All errors and warnings are listed in the step log and in collapsed **Failing Links** and **Warnings** tables in the job summary, with the file and line containing each link. If any links fail, a single annotation reports how many (e.g. `55 of 464 links failed. See the job summary for details.`). It is an error if `fail-on-broken-links` is `"true"`, otherwise a warning.

Links that Sphinx does not check (for example, those matching `linkcheck_ignore`) are listed with clickable URLs in a collapsed **Ignored Links** table in the job summary, so they can be checked by hand. They do not affect the job result.

---

## Link Check History

Re-checks ride out an outage that lasts seconds. They do nothing for a site with an expired certificate, a host under maintenance, or a server throttling the runner for an afternoon, which last hours or days. Without a memory of earlier runs, a link that has worked every day for a year and failed once this morning is treated exactly like a link that has been dead since March, and every pull request opened in the meantime fails for a reason that has nothing to do with the pull request.

This action keeps a history of every `http` and `https` link it checks in a single JSON file on an orphan branch of the repository being checked, the way `gh-pages` holds built documentation. The branch holds that one file and nothing else, and never appears in the history of the branches being built. The history lives in the repository being checked, not in this action's repository, since each component checks its own set of links.

One record per URL:

```json
{
  "https://ladsweb.modaps.eosdis.nasa.gov": {
    "last_seen": "2026-10-27T14:03:11Z",
    "last_checked": "2026-10-27T14:03:11Z",
    "last_success": "2026-10-26T14:01:52Z",
    "first_failure": "2026-10-27T14:03:11Z",
    "failure_count": 1,
    "last_status": "unreachable",
    "last_code": 0,
    "last_info": "[Errno 101] Network is unreachable"
  }
}
```

`first_failure`, `last_code`, and `last_info` are removed and `failure_count` is reset to `0` whenever the link passes, so `failure_count` counts the checks in the current run of failures. `last_seen` is refreshed for every URL found in the documentation, checked or not, and records for URLs not seen for `history-retention` days are dropped, so the file does not grow without bound as links are removed from the docs.

### What fails the job

| Failure | Result |
| :--- | :--- |
| **Permanent** (404, 410, `Anchor '...' not found`, unknown host, malformed URLs, a DOI that is not registered, a broken relative link) | Fails the job at once, as it did before. The grace period never applies. |
| **Transient** (connection errors, timeouts, 429, 5xx, TLS failures), failing since before the grace period, seen failing at least `min-failures` times | Fails the job |
| **Transient**, failing for less than `grace-period` days | Reported as a warning, with the date it last passed and the date it starts failing the job |
| **Transient**, with no record in an existing history | Reported as a warning, labeled as seen for the first time, and the grace period takes over from the next run |
| **Transient**, with no usable history at all (the branch or file does not exist yet, or the file is damaged) | Fails the job, as in v1.6.0 |

A new link that fails transiently is awkward: it could be a typo in a hostname or a healthy site having a bad hour, and there is no history to tell them apart. It is reported as a warning rather than failing a pull request for the links a reviewer is already looking at. A mistyped host name usually fails as `Name or service not known`, which is a permanent failure and fails the job however new the link is.

Requiring `min-failures` observed failures keeps a link from failing the job on the strength of a single bad run, which matters when runs are infrequent. The decision is made from `first_failure` rather than from `last_success` alone, because the history is only as fresh as the runs that write it: if the documentation workflow does not run for three weeks, every link has a stale `last_success` and a rule based on it would fail all of them at once.

Setting `grace-period` to `0` and `min-failures` to `1` restores the behavior of v1.6.0, where every link still unreachable after its re-checks fails the job, except that TLS failures are still re-checked before they fail. Setting `history-branch` to an empty string restores it exactly, TLS failures included, and stops the branch from being read or written at all. `fail-on-transient: 'false'` still reports every transient failure as a warning, whatever the history says.

### Which runs write the history

`GITHUB_TOKEN` is read-only for pull requests from forks, and giving pull request runs write access to a branch is not wanted anyway. Every run reads the history; by default only runs that are not `pull_request` or `pull_request_target` write it. A pull request run decides pass or fail from the history that the branch runs have built up, and leaves the branch alone.

The writing runs need `permissions: contents: write` in the calling workflow. Without it the push is rejected, which is reported as a warning and does not fail the job.

Until the branch holds the history file, there is nothing to measure a grace period against, so each run decides from its own results as v1.6.0 did: a TLS failure is permanent, and a link still unreachable after its re-checks fails the job. The same applies to a workflow that never manages to write the history, because it runs only on pull requests or lacks `contents: write`, so turning the history on can never stop a persistent failure from failing the job. Such a workflow simply gets no grace period. After adding this action to a workflow, check the log of the first push or scheduled run for the warning above, and confirm that the history branch was created.

A history file that cannot be parsed is reported with a warning annotation naming the commit that holds it, and that run decides from its own results. The next run that writes the history replaces the file with one built from its own results; the damaged copy stays in the branch's log, where it can be recovered with `git show <commit>:<history-file>`.

Three cautions when choosing `history-branch`:

* **The branch name must not match the `push` branch patterns of the calling workflow.** MET's `documentation.yml` triggers on `feature_*`, `main_*`, and `bugfix_*`, so a history branch named for any of those patterns would start another run every time the history is written.
* **Give each workflow its own `history-file` if several share the branch.** Each push replaces only its own file and leaves the others alone.
* **Add a `schedule` trigger.** A workflow that only runs on pushes that touch `docs/**` refreshes the history only when the documentation changes, leaving `first_failure` dates weeks apart. A scheduled run also reports a link that has gone dead on its own, instead of waiting for whoever opens the next pull request to find it.

The history is written after the links are checked, so a failing job still records the run that made it fail. The commit is built with git plumbing commands and pushed straight from the object database, so writing it never touches the working tree, the index, or the branch being built. Concurrent runs collide on the branch: a rejected push is retried by fetching the branch again, merging this run's results into what the other run wrote, and pushing again, up to three times. A link the other run checked more recently keeps that run's result, so an older pass never clears a newer failure.

### What the job summary shows

The summary reports how many records the history holds, and how many links are failing beyond the grace period against how many are within it. The **Failing Links** table carries a `Failing since` column giving the date the current run of failures started and how long it has been going (e.g. `2026-10-10 (18 days)`). The **Warnings** table carries `Last OK` and `Fails job after` columns, so nobody has to guess whether a failing link is being tracked or when it will start failing the job.

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

## DOI Links

Many publishers (e.g. AMS, DOI prefix `10.1175`, and Wiley, prefix `10.1002`) return `403` or `405` to automated requests, so their DOI links are often added to `linkcheck_ignore`. Ignored links are never checked, so a mistyped DOI would not be caught.

When `check-dois` is `"true"`, this action checks every `doi.org` or `dx.doi.org` link that Sphinx reports as `ignored`, `broken`, or `timeout` with the DOI API (`https://doi.org/api/handles/<doi>`), which says whether a DOI is registered without loading the publisher's page:

* **Found:** the link passes, and is not listed in the Ignored Links table.
* **Not found:** the link is reported as an error (`DOI ... not found`).
* **Lookup failed** (connection error, timeout, or unexpected response): the link is re-checked like other transient failures.

This confirms that the DOI is registered, not that the publisher's page loads. Anchors on DOI links are not checked. Components can keep publisher DOI patterns such as `r'https://doi\.org/10\.1175/.*'` in `linkcheck_ignore`, which stops Sphinx from requesting the publisher's page while the DOI is still checked.

---

## GitHub File Links

GitHub throttles unauthenticated requests for file pages (`https://github.com/<owner>/<repo>/blob/<ref>/<path>`), returning `503` or `429`, and the throttling usually lasts longer than the re-check delays. When a GitHub file link fails with `503` or `429`, this action checks the same file on `https://raw.githubusercontent.com/<owner>/<repo>/<ref>/<path>` instead:

* **Found:** the link passes.
* **Not found (`404`):** the link is reported as an error, so a renamed or deleted file still fails the job.
* **Any other error:** the link is re-checked on `raw.githubusercontent.com` like other transient failures.

This confirms that the file exists, not that the `github.com` page loads. Line anchors such as `#L55` are not checked. Other GitHub links, and GitHub file links that fail for other reasons (e.g. `404`), are handled as usual.

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

### Link Check History

The [link check history](#link-check-history) is enabled by default. The runs that write it need `contents: write`, and a `schedule` trigger keeps the history fresh even when the documentation does not change:

```yaml
name: Documentation

on:
  push:
    branches: ['develop', 'main_v*']
    paths: ['docs/**']
  pull_request:
    paths: ['docs/**']
  schedule:
    - cron: '17 6 * * 1'
  workflow_dispatch:

permissions:
  contents: write

jobs:
  linkcheck:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5

      - name: Check Documentation Links
        uses: dtcenter/metplus-action-linkcheck@v1
        with:
          grace-period: '14'
          min-failures: '2'
```

This example turns the history off, so that each run decides from its own results, as in v1.6.0:

```yaml
- name: Check Documentation Links
  uses: dtcenter/metplus-action-linkcheck@v1
  with:
    history-branch: ''
```
