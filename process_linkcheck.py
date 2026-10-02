#!/usr/bin/env python3
"""
Process the JSON output of the Sphinx linkcheck builder.

Broken links are classified as permanent (e.g. 404, missing anchor, unknown
host) or transient (e.g. connection errors, timeouts, 429, 5xx). Transient
failures are re-checked after an increasing delay. Relative links are checked
against the documents read by the Sphinx build instead of over the network.
DOI links, including ignored ones, are checked with the DOI API.
Results are listed in the log and as Markdown tables in the job summary,
including a collapsed table of ignored links. If any links fail, a single
GitHub Actions annotation reports how many.

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
        return PERMANENT
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


def classify_doi(code):
    """Only a DOI the API reports as not found is a permanent failure."""
    return PERMANENT if code == 404 else TRANSIENT


def is_relative(uri):
    """Return True for a link to a local path rather than a URL or anchor."""
    return (not uri.startswith('#') and not urlsplit(uri).scheme
            and not uri.startswith('//'))


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
            else:
                ok, code, info = check_url(link['uri'], timeout, user_agent)
            link['attempts'] = attempt
            if ok:
                link['result'] = 'recovered'
                print(f'  recovered: {link["uri"]}')
                continue
            link['info'] = info
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


def collapsed_table(title, header, rows):
    """Return the lines of a Markdown table in a collapsed section."""
    return (['', '<details>', f'<summary>{title} ({len(rows)})</summary>', '',
             '| ' + ' | '.join(header) + ' |',
             '| ' + ' | '.join(':---' for _ in header) + ' |']
            + rows + ['', '</details>'])


def write_summary(path, failing, warnings, ignored, total, doi_count):
    lines = ['## Linkcheck Results', '']
    if not failing and not warnings:
        summary = f'All {total} checked links passed'
    else:
        summary = (f'Checked {total} links: **{len(failing)} failing**, '
                   f'{len(warnings)} warning(s)')
    if ignored:
        summary += f', {len(ignored)} ignored'
    lines.append(summary + '.')
    if doi_count:
        lines += ['', f'{doi_count} DOI link(s) were checked with the DOI API '
                      f'(`{DOI_API}`) instead of the publisher\'s site.']
    for title, links in (('Failing Links', failing), ('Warnings', warnings)):
        if not links:
            continue
        rows = [f'| {escape_cell(link["path"])}:{link["lineno"]} '
                f'| {uri_cell(link)} '
                f'| {link["result"]} '
                f'| {escape_cell(link["info"])} |' for link in links]
        lines += collapsed_table(title, ('Location', 'URI', 'Result',
                                         'Details'), rows)
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
    args = parser.parse_args()

    fail_on_transient = args.fail_on_transient.lower() == 'true'
    fail_on_broken = args.fail_on_broken_links.lower() == 'true'
    doctree_dir = args.doctree_dir or os.path.join(
        os.path.dirname(args.output_json), '.doctrees')
    rtd_base = get_rtd_base(args.rtd_url, args.branch)
    check_dois = args.check_dois.lower() == 'true'
    user_agent = get_user_agent()

    with open(args.output_json) as file_handle:
        entries = [json.loads(line) for line in file_handle if line.strip()]

    links = []
    ignored = []
    docnames = None
    relative_ok = 0
    doi_results = {}
    doi_count = 0
    doi_ignored = 0
    for entry in entries:
        status = entry.get('status')
        link = {
            'path': os.path.normpath(os.path.join(args.docs_path,
                                                  entry['filename'])),
            'lineno': entry.get('lineno') or 1,
            'uri': entry['uri'],
            'info': entry.get('info') or '',
            'result': 'broken',
            'attempts': 0,
        }
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
                continue
            link['doi'] = doi
            link['info'] = info
            link['category'] = classify_doi(code)
            links.append(link)
            continue

        if status == 'ignored':
            link['info'] = link['info'] or 'matches linkcheck_ignore'
            ignored.append(link)
            continue
        if status not in ('broken', 'timeout'):
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

    total = doi_ignored + sum(
        1 for entry in entries
        if entry.get('status') not in ('unchecked', 'ignored', 'local'))
    transient = [link for link in links if link['category'] == TRANSIENT]
    if relative_ok:
        print(f'Found {relative_ok} relative link(s) in the docs build')
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

    failing = []
    warnings = []
    for link in links:
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
        else:
            message = f'Broken link {link["uri"]}: {link["info"]}'
            if link.get('rtd_link'):
                message += f' (Read the Docs: {link["rtd_link"]})'
            report('error', link, message)
            failing.append(link)

    summary_file = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary_file:
        write_summary(summary_file, failing, warnings, ignored, total,
                      doi_count)

    still_broken = [link for link in links if link['result'] != 'recovered']
    set_output('broken_links_found', 'true' if still_broken else 'false')

    print(f'Result: {len(failing)} failing, {len(warnings)} warning(s)')
    if failing:
        annotate('error' if fail_on_broken else 'warning',
                 f'{len(failing)} of {total} links failed. '
                 f'See the job summary for details.')
    return 1 if failing else 0


if __name__ == '__main__':
    sys.exit(main())
