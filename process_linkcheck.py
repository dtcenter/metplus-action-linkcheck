#!/usr/bin/env python3
"""
Process the JSON output of the Sphinx linkcheck builder.

Broken links are classified as permanent (e.g. 404, missing anchor, unknown
host) or transient (e.g. connection errors, timeouts, 429, 5xx, including 503
responses that Sphinx reports as ignored). GitHub file links that github.com
throttles with 503 or 429 are checked on raw.githubusercontent.com. Transient
failures are re-checked after an increasing delay. Relative links are checked
against the documents read by the Sphinx build instead of over the network.
DOI links, including ignored ones, are checked with the DOI API.
Results are listed in the log and as Markdown tables in the job summary,
including a collapsed table of ignored links. If any links fail, a single
GitHub Actions annotation reports how many.

When a link check history is available, a link that is still unreachable
fails the job only once it has been failing for longer than the grace period
and has failed at least --min-failures times. Links failing for less time are
reported as warnings, as are links seen for the first time. Permanent
failures (404, missing anchors, unknown hosts, and the like) always fail,
whatever the history says.

Exit status is 1 if any link should fail the job, otherwise 0.
"""

import argparse
import json
import os
import pickle
import posixpath
import re
import sys
import time
from urllib.parse import quote, unquote, urljoin, urlsplit

import requests

from linkcheck_history import (evaluate, load_history, merge as merge_history,
                               now_utc, save_results)

PERMANENT = 'permanent'
TRANSIENT = 'transient'

HTTP_ERROR_RE = re.compile(r'^(\d{3}) (?:Client|Server) Error', re.IGNORECASE)

DNS_ERRORS = (
    'nameresolutionerror',
    'failed to resolve',
    'name or service not known',
    'nodename nor servname',
    'no address associated with hostname',
)

# A TLS failure is the server's configuration, not the link: an expired
# certificate or a handshake the server refuses is fixed by its owner, and the
# page is still where the documentation says it is. These are classified as
# transient so that the link check history can hold them inside the grace
# period while the owner fixes them, rather than failing every build at once.
SSL_ERRORS = (
    'sslerror',
    'certificate verify failed',
)

URL_ERRORS = (
    'invalid url',
    'no host supplied',
    'invalid schema',
    'no connection adapters',
)

# github.com file pages, which GitHub throttles with 503 or 429 responses
GITHUB_BLOB_RE = re.compile(r'^https://github\.com/([^/]+)/([^/]+)/blob/([^?#]+)')
GITHUB_THROTTLED = (429, 503)

DOI_HOSTS = ('doi.org', 'dx.doi.org', 'www.doi.org')
DOI_API = 'https://doi.org/api/handles/'

# pages written by the HTML builder that have no source document
BUILDER_PAGES = ('search', 'genindex', 'py-modindex')

# source suffixes to look for if the Sphinx environment cannot be loaded
SOURCE_SUFFIXES = ('.rst', '.md', '.ipynb', '.txt')


def get_user_agent():
    """Use the same User-Agent as the installed version of Sphinx."""
    try:
        # Sphinx < 7.3
        from sphinx.util.requests import useragent_header
        return useragent_header[0][1]
    except ImportError:
        pass
    try:
        # Sphinx >= 7.3
        from sphinx.util.requests import _USER_AGENT
        return _USER_AGENT
    except ImportError:
        pass
    import sphinx
    return f'Sphinx/{sphinx.__version__} requests/{requests.__version__}'


def get_http_code(code, info):
    """Return the HTTP status code, parsing it from info if code is not set."""
    if code and code >= 100:
        return code
    match = HTTP_ERROR_RE.match(info or '')
    return int(match.group(1)) if match else None


def classify(code, info):
    """Classify a broken link as permanent or transient."""
    http_code = get_http_code(code, info)
    if http_code is not None:
        if http_code == 429 or http_code >= 500:
            return TRANSIENT
        return PERMANENT

    lower = (info or '').lower()
    if lower.startswith('anchor ') and 'not found' in lower:
        return PERMANENT
    if any(err in lower for err in SSL_ERRORS):
        return TRANSIENT
    if any(err in lower for err in URL_ERRORS):
        return PERMANENT
    if 'temporary failure in name resolution' in lower:
        return TRANSIENT
    if any(err in lower for err in DNS_ERRORS):
        return PERMANENT

    # connection errors, timeouts, and anything unrecognized
    return TRANSIENT


def get_malformed_reason(uri):
    """Return why an http(s) URL is malformed, or None if it is not."""
    try:
        parts = urlsplit(uri)
        host = parts.hostname
    except ValueError as err:
        return f'malformed URL: {err}'
    if parts.scheme not in ('http', 'https'):
        return None
    if not host:
        return 'malformed URL: no host name'
    if host in ('http', 'https'):
        return (f"malformed URL: host name is '{host}' "
                f"(is the scheme repeated?)")
    return None


def get_doi(uri):
    """Return the DOI from a doi.org link, or None if it is not one."""
    try:
        parts = urlsplit(uri)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme not in ('http', 'https') or host not in DOI_HOSTS:
        return None
    doi = unquote(parts.path).lstrip('/')
    return doi if doi.startswith('10.') else None


def check_doi(doi, timeout, user_agent):
    """Look up a DOI with the DOI API. Return (ok, code, info)."""
    url = DOI_API + quote(doi, safe='/')
    try:
        response = requests.get(url, timeout=timeout,
                                headers={'User-Agent': user_agent})
        response_code = response.json().get('responseCode')
    except (requests.RequestException, ValueError) as err:
        return False, 0, f'DOI lookup failed: {err}'
    if response_code == 1:
        return True, response.status_code, ''
    if response_code == 100:
        return False, 404, f'DOI {doi} not found'
    code = response.status_code if response.status_code >= 500 else 0
    return (False, code, f'DOI lookup returned HTTP {response.status_code} '
                         f'with responseCode {response_code}')


def is_service_unavailable(status, info):
    """Return True for a link Sphinx ignored because it returned HTTP 503.

    Sphinx 8 reports these with info "service unavailable" and Sphinx 5 with
    "503 Server Error: ...". Other ignored links (matching linkcheck_ignore,
    ignored redirects, excluded documents) are not affected.
    """
    if status != 'ignored':
        return False
    lower = (info or '').lower()
    return lower == 'service unavailable' or get_http_code(0, info) == 503


def get_github_raw(uri):
    """Return the raw.githubusercontent.com URL for a github.com file link."""
    match = GITHUB_BLOB_RE.match(uri)
    if not match:
        return None
    owner, repo, ref_and_path = match.groups()
    return f'https://raw.githubusercontent.com/{owner}/{repo}/{ref_and_path}'


def check_github_raw(raw_url, timeout, user_agent):
    """Check a GitHub file on raw.githubusercontent.com. Return (ok, code, info)."""
    ok, code, info = check_url(raw_url, timeout, user_agent)
    if ok:
        return ok, code, info
    if code == 404:
        return False, 404, f'file not found on raw.githubusercontent.com: {raw_url}'
    return False, code, f'raw.githubusercontent.com: {info}'


def classify_doi(code):
    """Only a DOI the API reports as not found is a permanent failure."""
    return PERMANENT if code == 404 else TRANSIENT


def is_relative(uri):
    """Return True for a link to a local path rather than a URL or anchor."""
    return (not uri.startswith('#') and not urlsplit(uri).scheme
            and not uri.startswith('//'))


def is_history_url(uri):
    """Return True for an http(s) URL, the only kind the history records."""
    try:
        return urlsplit(uri).scheme in ('http', 'https')
    except ValueError:
        return False


def record_result(results, uri, checked, ok, status='', code=0, info=''):
    """Record one link's result for the history, keyed by URL.

    Only http(s) URLs are recorded. Relative links are checked against the
    documents in the build rather than over the network, so they never fail
    transiently, and the same relative path means different things in
    different files.

    A URL appearing in several files is recorded once. Recording it as seen
    but not checked keeps it from being dropped by the retention cutoff while
    it is still in the documentation.
    """
    if not is_history_url(uri):
        return
    entry = results.setdefault(uri, {'checked': False, 'ok': False})
    if not checked or (entry['checked'] and not entry['ok']):
        return
    entry['checked'] = True
    entry['ok'] = ok
    if ok:
        entry.pop('status', None)
        entry.pop('code', None)
        entry.pop('info', None)
    else:
        entry['status'] = status or 'broken'
        entry['code'] = code or 0
        entry['info'] = info or ''


def load_docnames(doctree_dir, docs_path):
    """Return the names of all documents read by the Sphinx build."""
    try:
        with open(os.path.join(doctree_dir, 'environment.pickle'),
                  'rb') as file_handle:
            return set(pickle.load(file_handle).found_docs)
    except Exception as err:
        print(f'Could not load the Sphinx environment ({err}), '
              f'searching {docs_path} for source files instead')

    docnames = set()
    for root, dirs, files in os.walk(docs_path):
        dirs[:] = [d for d in dirs if not d.startswith(('_build', '.'))]
        for name in files:
            stem, ext = os.path.splitext(name)
            if ext in SOURCE_SUFFIXES:
                rel = os.path.relpath(os.path.join(root, stem), docs_path)
                docnames.add(rel.replace(os.sep, '/'))
    return docnames


def check_relative(filename, uri, docnames):
    """Check a relative link against the documents in the build.

    Return (ok, info) where info describes why the link is broken.
    """
    path = unquote(urlsplit(uri).path)
    if not path:
        # only a query string, which refers to the current page
        return True, ''
    if path.startswith('/'):
        target = posixpath.normpath(path.lstrip('/'))
    else:
        target = posixpath.normpath(
            posixpath.join(posixpath.dirname(filename), path))
    if target == '..' or target.startswith('../'):
        return False, f'path {target} is outside of the docs directory'

    if not path.endswith('.html') and not path.endswith('/'):
        # Sphinx already checked other files against the source tree
        return False, f'file {target} not found in the docs directory'

    docname = target[:-len('.html')] if path.endswith('.html') else (
        posixpath.join(target, 'index') if target != '.' else 'index')
    if docname in docnames or docname in BUILDER_PAGES:
        return True, ''
    return False, f'no document {docname} in the docs build'


def get_rtd_base(rtd_url, branch):
    """Return the Read the Docs URL for the branch, or None if not set."""
    if not rtd_url:
        return None
    version = re.sub(r'[^a-z0-9._-]', '-', (branch or '').lower())
    base = rtd_url.replace('{version}', version)
    return base if base.endswith('/') else base + '/'


def get_rtd_link(rtd_base, filename, uri):
    """Resolve a relative link against the Read the Docs page containing it."""
    page = posixpath.splitext(filename)[0] + '.html'
    return urljoin(urljoin(rtd_base, page), uri)


def check_url(uri, timeout, user_agent):
    """Request a URL (ignoring any anchor). Return (ok, code, info)."""
    url = uri.split('#', 1)[0]
    headers = {
        'User-Agent': user_agent,
        'Accept': 'text/html,application/xhtml+xml;q=0.9,*/*;q=0.8',
    }
    try:
        response = requests.head(url, headers=headers, timeout=timeout,
                                 allow_redirects=True)
        if response.status_code >= 400:
            # some servers do not support HEAD requests, so retry with GET
            response = requests.get(url, headers=headers, timeout=timeout,
                                    allow_redirects=True, stream=True)
            response.close()
        if response.status_code < 400:
            return True, response.status_code, ''
        return (False, response.status_code,
                f'{response.status_code} {response.reason} for url: {url}')
    except requests.RequestException as err:
        return False, 0, str(err)


def recheck(links, attempts, delay, timeout):
    """Re-check transient failures, waiting longer before each attempt."""
    user_agent = get_user_agent()
    pending = list(links)
    for attempt in range(1, attempts + 1):
        if not pending:
            break
        wait = delay * 2 ** (attempt - 1)
        print(f'Waiting {wait}s before re-checking {len(pending)} link(s) '
              f'(attempt {attempt} of {attempts})')
        sys.stdout.flush()
        time.sleep(wait)

        still_failing = []
        for link in pending:
            if link.get('doi'):
                ok, code, info = check_doi(link['doi'], timeout, user_agent)
            elif link.get('github_raw'):
                ok, code, info = check_github_raw(link['github_raw'], timeout,
                                                  user_agent)
            else:
                ok, code, info = check_url(link['uri'], timeout, user_agent)
            link['attempts'] = attempt
            if ok:
                link['result'] = 'recovered'
                print(f'  recovered: {link["uri"]}')
                continue
            link['info'] = info
            link['code'] = code or 0
            print(f'  still failing: {link["uri"]}: {info}')
            category = (classify_doi(code) if link.get('doi')
                        else classify(code, info))
            if category == PERMANENT:
                link['category'] = PERMANENT
                link['result'] = 'broken'
            else:
                still_failing.append(link)
        pending = still_failing

    for link in pending:
        link['result'] = 'unreachable'


def escape_data(value):
    return value.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def report(level, link, message):
    """Print one link result to the log."""
    print(f'{level}: {link["path"]}:{link["lineno"]}: {message}')


def date_only(timestamp):
    """Return the date part of a history timestamp."""
    return timestamp[:10] if timestamp else ''


def days_text(days):
    return f'{days} day' if days == 1 else f'{days} days'


def history_note(entry):
    """Describe how long a link has been failing."""
    if entry.get('new'):
        return 'seen for the first time'
    if not entry.get('first_failure'):
        return 'no record of an earlier failure'
    return (f'failing since {date_only(entry["first_failure"])}, '
            f'{days_text(entry["days"])}, {entry["failure_count"]} check(s)')


def grace_message(link, entry):
    """Describe a link that is failing but still inside the grace period."""
    message = (f'Link {link["uri"]} is unreachable ({history_note(entry)}): '
               f'{link["info"]}')
    if entry.get('last_success'):
        message += f'. It last passed on {date_only(entry["last_success"])}'
    if entry.get('expired'):
        message += (f'. It will fail the job once it has failed '
                    f'{entry["min_failures"]} checks in a row')
    elif entry.get('fails_on'):
        message += (f'. It will fail the job if it is still failing after '
                    f'{date_only(entry["fails_on"])}')
    return message


def annotate(level, message):
    print(f'::{level} title=Linkcheck::{escape_data(message)}')


def escape_cell(value):
    return value.replace('|', '\\|').replace('\n', ' ')


def markdown_link(url):
    """Format a URL as a clickable Markdown link for a table cell."""
    text = url.replace('[', '\\[').replace(']', '\\]')
    return escape_cell(f'[{text}](<{url}>)')


def uri_cell(link):
    """Format the URI cell, adding the Read the Docs URL if there is one."""
    if link.get('rtd_link'):
        return (f'{escape_cell(link["uri"])}<br>'
                f'{markdown_link(link["rtd_link"])}')
    return escape_cell(link['uri'])


def failing_since_cell(entry):
    """Format the date the current run of failures started."""
    if entry and entry.get('new'):
        return 'first seen'
    if entry and entry.get('first_failure'):
        return (f'{date_only(entry["first_failure"])} '
                f'({days_text(entry["days"])})')
    return '-'


def last_ok_cell(entry):
    """Format the date a link last passed."""
    if not entry:
        return '-'
    if entry.get('last_success'):
        return date_only(entry['last_success'])
    return 'first seen' if entry.get('new') else 'never'


def fails_after_cell(entry):
    """Format when a link starts failing the job."""
    if not entry or not entry.get('fails_on'):
        return '-'
    if entry.get('expired'):
        remaining = entry['min_failures'] - entry['failure_count']
        return f'{remaining} more check(s)'
    return date_only(entry['fails_on'])


def collapsed_table(title, header, rows):
    """Return the lines of a Markdown table in a collapsed section."""
    return (['', '<details>', f'<summary>{title} ({len(rows)})</summary>', '',
             '| ' + ' | '.join(header) + ' |',
             '| ' + ' | '.join(':---' for _ in header) + ' |']
            + rows + ['', '</details>'])


def write_summary(path, failing, warnings, ignored, total, doi_count,
                  github_count, history_info=None):
    lines = ['## Linkcheck Results', '']
    if not failing and not warnings:
        summary = f'All {total} checked links passed'
    else:
        summary = (f'Checked {total} links: **{len(failing)} failing**, '
                   f'{len(warnings)} warning(s)')
    if ignored:
        summary += f', {len(ignored)} ignored'
    lines.append(summary + '.')
    if history_info:
        note = (f'Link check history: {history_info["records"]} record(s) on '
                f'the `{history_info["branch"]}` branch.')
        if history_info['beyond'] or history_info['within']:
            note += (f' {history_info["beyond"]} link(s) failing beyond the '
                     f'{history_info["grace"]}-day grace period, '
                     f'{history_info["within"]} within it.')
        lines += ['', note]
    if doi_count:
        lines += ['', f'{doi_count} DOI link(s) were checked with the DOI API '
                      f'(`{DOI_API}`) instead of the publisher\'s site.']
    if github_count:
        lines += ['', f'{github_count} GitHub file link(s) were checked on '
                      f'`raw.githubusercontent.com` because `github.com` '
                      f'returned 503 or 429.']
    for title, links in (('Failing Links', failing), ('Warnings', warnings)):
        if not links:
            continue
        header = ['Location', 'URI', 'Result']
        history_cells = []
        if history_info:
            if title == 'Failing Links':
                header += ['Failing since']
                history_cells = [failing_since_cell]
            else:
                header += ['Last OK', 'Fails job after']
                history_cells = [last_ok_cell, fails_after_cell]
        header += ['Details']
        rows = []
        for link in links:
            cells = [f'{escape_cell(link["path"])}:{link["lineno"]}',
                     uri_cell(link), link['result']]
            cells += [cell(link.get('history')) for cell in history_cells]
            cells += [escape_cell(link['info'])]
            rows.append('| ' + ' | '.join(cells) + ' |')
        lines += collapsed_table(title, header, rows)
    if ignored:
        rows = [f'| {escape_cell(link["path"])}:{link["lineno"]} '
                f'| {markdown_link(link["uri"])} '
                f'| {escape_cell(link["info"])} |' for link in ignored]
        lines += collapsed_table('Ignored Links',
                                 ('Location', 'URI', 'Reason'), rows)
    with open(path, 'a') as file_handle:
        file_handle.write('\n'.join(lines) + '\n')


def set_output(name, value):
    output_file = os.environ.get('GITHUB_OUTPUT')
    if output_file:
        with open(output_file, 'a') as file_handle:
            file_handle.write(f'{name}={value}\n')
    else:
        print(f'{name}={value}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output_json',
                        help='path to the linkcheck output.json file')
    parser.add_argument('--docs-path', default='docs',
                        help='docs directory, used to build file paths')
    parser.add_argument('--recheck-attempts', type=int, default=2)
    parser.add_argument('--recheck-delay', type=int, default=15)
    parser.add_argument('--timeout', type=int, default=30)
    parser.add_argument('--fail-on-transient', default='true')
    parser.add_argument('--fail-on-broken-links', default='true',
                        help='if "true", report failing links as an error '
                             'annotation, otherwise as a warning')
    parser.add_argument('--doctree-dir',
                        help='Sphinx doctree directory containing '
                             'environment.pickle (default: .doctrees next '
                             'to output_json)')
    parser.add_argument('--rtd-url', default='',
                        help='Read the Docs URL with a {version} placeholder, '
                             'used to show where broken relative links point')
    parser.add_argument('--branch', default='',
                        help='branch being built, used for {version}')
    parser.add_argument('--check-dois', default='true',
                        help='if "true", check doi.org links, including '
                             'ignored ones, with the DOI API')
    parser.add_argument('--use-history', default='false',
                        help='if "true", decide transient failures from the '
                             'link check history instead of failing at once')
    parser.add_argument('--history-file', default='',
                        help='link check history read from the history '
                             'branch; a missing file starts a new history')
    parser.add_argument('--history-results', default='',
                        help='where to write this run\'s results, which the '
                             'action merges into the history and pushes')
    parser.add_argument('--history-branch', default='',
                        help='name of the history branch, reported in the '
                             'job summary')
    parser.add_argument('--grace-period', type=int, default=14,
                        help='days a transiently failing link may keep '
                             'failing before it fails the job')
    parser.add_argument('--min-failures', type=int, default=2,
                        help='consecutive failed checks required before a '
                             'link can fail the job')
    parser.add_argument('--history-retention', type=int, default=90,
                        help='days to keep a record for a URL that is no '
                             'longer in the documentation')
    args = parser.parse_args()

    fail_on_transient = args.fail_on_transient.lower() == 'true'
    fail_on_broken = args.fail_on_broken_links.lower() == 'true'
    use_history = args.use_history.lower() == 'true'
    checked_at = now_utc()
    doctree_dir = args.doctree_dir or os.path.join(
        os.path.dirname(args.output_json), '.doctrees')
    rtd_base = get_rtd_base(args.rtd_url, args.branch)
    check_dois = args.check_dois.lower() == 'true'
    user_agent = get_user_agent()

    with open(args.output_json) as file_handle:
        entries = [json.loads(line) for line in file_handle if line.strip()]

    links = []
    ignored = []
    results = {}
    docnames = None
    relative_ok = 0
    doi_results = {}
    doi_count = 0
    doi_ignored = 0
    unavailable = 0
    github_results = {}
    github_count = 0
    github_ignored = 0
    for entry in entries:
        status = entry.get('status')
        link = {
            'path': os.path.normpath(os.path.join(args.docs_path,
                                                  entry['filename'])),
            'lineno': entry.get('lineno') or 1,
            'uri': entry['uri'],
            'info': entry.get('info') or '',
            'code': get_http_code(entry.get('code'), entry.get('info')) or 0,
            'result': 'broken',
            'attempts': 0,
        }
        # record every URL in the documentation, checked or not, so that its
        # history is not dropped by the retention cutoff while it is in use
        record_result(results, entry['uri'], False, False)
        doi = None
        if check_dois and status in ('ignored', 'broken', 'timeout'):
            doi = get_doi(entry['uri'])
        if doi:
            if doi not in doi_results:
                doi_results[doi] = check_doi(doi, args.timeout, user_agent)
            ok, code, info = doi_results[doi]
            doi_count += 1
            if status == 'ignored':
                doi_ignored += 1
            if ok:
                record_result(results, entry['uri'], True, True)
                continue
            link['doi'] = doi
            link['info'] = info
            link['code'] = code
            link['category'] = classify_doi(code)
            links.append(link)
            continue

        raw_url = get_github_raw(entry['uri'])
        throttled = is_service_unavailable(status, link['info']) or (
            status in ('broken', 'timeout') and
            get_http_code(entry.get('code'), link['info']) in GITHUB_THROTTLED)
        if raw_url and throttled:
            # github.com throttles file pages, so check the raw file instead
            if raw_url not in github_results:
                github_results[raw_url] = check_github_raw(raw_url, args.timeout,
                                                           user_agent)
            ok, code, info = github_results[raw_url]
            github_count += 1
            if status == 'ignored':
                github_ignored += 1
            if ok:
                record_result(results, entry['uri'], True, True)
                continue
            link['github_raw'] = raw_url
            link['info'] = info
            link['code'] = code
            link['category'] = PERMANENT if code == 404 else TRANSIENT
            links.append(link)
            continue

        if is_service_unavailable(status, link['info']):
            # Sphinx ignores 503 responses, but they are transient failures
            unavailable += 1
            if link['info'].lower() == 'service unavailable':
                link['info'] = '503 Service Unavailable'
            link['category'] = TRANSIENT
            links.append(link)
            continue
        if status == 'ignored':
            link['info'] = link['info'] or 'matches linkcheck_ignore'
            ignored.append(link)
            continue
        if status not in ('broken', 'timeout'):
            if status in ('working', 'redirected'):
                record_result(results, entry['uri'], True, True)
            continue

        if is_relative(entry['uri']):
            if docnames is None:
                docnames = load_docnames(doctree_dir, args.docs_path)
            ok, info = check_relative(entry['filename'], entry['uri'],
                                      docnames)
            if ok:
                relative_ok += 1
                continue
            link['info'] = info
            link['category'] = PERMANENT
            if rtd_base:
                link['rtd_link'] = get_rtd_link(rtd_base, entry['filename'],
                                                entry['uri'])
        elif get_malformed_reason(entry['uri']):
            link['info'] = get_malformed_reason(entry['uri'])
            link['category'] = PERMANENT
        elif status == 'timeout':
            link['category'] = TRANSIENT
        else:
            link['category'] = classify(entry.get('code'), entry.get('info'))
        links.append(link)

    total = doi_ignored + unavailable + github_ignored + sum(
        1 for entry in entries
        if entry.get('status') not in ('unchecked', 'ignored', 'local'))
    transient = [link for link in links if link['category'] == TRANSIENT]
    if relative_ok:
        print(f'Found {relative_ok} relative link(s) in the docs build')
    if unavailable:
        print(f'Sphinx ignored {unavailable} link(s) that returned '
              f'503 Service Unavailable; re-checking them as transient')
    if github_count:
        found = sum(1 for result in github_results.values() if result[0])
        print(f'Checked {github_count} GitHub file link(s) on '
              f'raw.githubusercontent.com because github.com returned 503 '
              f'or 429: {found} of {len(github_results)} unique file(s) found')
    if doi_count:
        found = sum(1 for result in doi_results.values() if result[0])
        print(f'Checked {doi_count} DOI link(s) with the DOI API: '
              f'{found} of {len(doi_results)} unique DOI(s) found')
    print(f'Linkcheck reported {len(links)} broken link(s): '
          f'{len(links) - len(transient)} permanent, '
          f'{len(transient)} transient')

    if transient and args.recheck_attempts > 0:
        recheck(transient, args.recheck_attempts, args.recheck_delay,
                args.timeout)
    else:
        for link in transient:
            link['result'] = 'unreachable'

    # record how this run ended for every link linkcheck reported as broken
    for link in links:
        if link['result'] == 'recovered':
            record_result(results, link['uri'], True, True)
        else:
            record_result(results, link['uri'], True, False,
                          status=link['result'], code=link.get('code'),
                          info=link['info'])

    history_records = {}
    merged = {}
    if use_history:
        history_records = load_history(args.history_file)
        merged = merge_history(history_records, results, checked_at,
                               args.history_retention)
        print(f'Read {len(history_records)} link check history record(s); '
              f'{len(merged)} record(s) after this run')
        for link in links:
            if is_history_url(link['uri']):
                link['history'] = evaluate(merged.get(link['uri'], {}),
                                           link['uri'] not in history_records,
                                           checked_at, args.grace_period,
                                           args.min_failures)
    if args.history_results:
        save_results(args.history_results, checked_at, results)

    failing = []
    warnings = []
    beyond_grace = 0
    within_grace = 0
    for link in links:
        entry = link.get('history')
        if link['result'] == 'recovered':
            report('warning', link,
                   f'Link {link["uri"]} failed linkcheck but recovered '
                   f'after {link["attempts"]} re-check(s)')
            warnings.append(link)
        elif link['result'] == 'unreachable' and not fail_on_transient:
            report('warning', link,
                   f'Link {link["uri"]} is still unreachable after '
                   f'{link["attempts"]} re-check(s): {link["info"]}')
            warnings.append(link)
        elif link['result'] == 'unreachable' and entry and not entry['fail']:
            within_grace += 1
            report('warning', link, grace_message(link, entry))
            warnings.append(link)
        else:
            if link['result'] == 'unreachable' and entry:
                beyond_grace += 1
            message = f'Broken link {link["uri"]}: {link["info"]}'
            if entry and not entry.get('new') and entry.get('first_failure'):
                message += f' ({history_note(entry)})'
            if link.get('rtd_link'):
                message += f' (Read the Docs: {link["rtd_link"]})'
            report('error', link, message)
            failing.append(link)

    history_info = None
    if use_history:
        history_info = {
            'branch': args.history_branch or 'link check history',
            'records': len(merged),
            'beyond': beyond_grace,
            'within': within_grace,
            'grace': args.grace_period,
        }

    summary_file = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary_file:
        write_summary(summary_file, failing, warnings, ignored, total,
                      doi_count, github_count, history_info)

    still_broken = [link for link in links if link['result'] != 'recovered']
    set_output('broken_links_found', 'true' if still_broken else 'false')

    print(f'Result: {len(failing)} failing, {len(warnings)} warning(s)')
    if use_history:
        print(f'{beyond_grace} link(s) failing beyond the '
              f'{args.grace_period}-day grace period, {within_grace} within '
              f'it')
    if failing:
        annotate('error' if fail_on_broken else 'warning',
                 f'{len(failing)} of {total} links failed. '
                 f'See the job summary for details.')
    return 1 if failing else 0


if __name__ == '__main__':
    sys.exit(main())
