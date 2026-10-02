#!/usr/bin/env python3
"""
Process the JSON output of the Sphinx linkcheck builder.

Broken links are classified as permanent (e.g. 404, missing anchor, unknown
host) or transient (e.g. connection errors, timeouts, 429, 5xx). Transient
failures are re-checked after an increasing delay. Results are reported as
GitHub Actions annotations and as a Markdown table in the job summary.

Exit status is 1 if any link should fail the job, otherwise 0.
"""

import argparse
import json
import os
import re
import sys
import time

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
    if 'temporary failure in name resolution' in lower:
        return TRANSIENT
    if any(err in lower for err in DNS_ERRORS):
        return PERMANENT

    # connection errors, timeouts, and anything unrecognized
    return TRANSIENT


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
            ok, code, info = check_url(link['uri'], timeout, user_agent)
            link['attempts'] = attempt
            if ok:
                link['result'] = 'recovered'
                print(f'  recovered: {link["uri"]}')
                continue
            link['info'] = info
            print(f'  still failing: {link["uri"]}: {info}')
            if classify(code, info) == PERMANENT:
                link['category'] = PERMANENT
                link['result'] = 'broken'
            else:
                still_failing.append(link)
        pending = still_failing

    for link in pending:
        link['result'] = 'unreachable'


def escape_data(value):
    return value.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def escape_property(value):
    return escape_data(value).replace(':', '%3A').replace(',', '%2C')


def annotate(level, link, message):
    print(f'::{level} file={escape_property(link["path"])},'
          f'line={link["lineno"]},title=Linkcheck::{escape_data(message)}')


def escape_cell(value):
    return value.replace('|', '\\|').replace('\n', ' ')


def write_summary(path, failing, warnings, total):
    lines = ['## Linkcheck Results', '']
    if not failing and not warnings:
        lines.append(f'All {total} checked links passed.')
    else:
        lines.append(f'Checked {total} links: **{len(failing)} failing**, '
                     f'{len(warnings)} warning(s).')
    for title, links in (('Failing Links', failing), ('Warnings', warnings)):
        if not links:
            continue
        lines += ['', f'### {title}', '',
                  '| Location | URI | Result | Details |',
                  '| :--- | :--- | :--- | :--- |']
        for link in links:
            lines.append(f'| {escape_cell(link["path"])}:{link["lineno"]} '
                         f'| {escape_cell(link["uri"])} '
                         f'| {link["result"]} '
                         f'| {escape_cell(link["info"])} |')
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
                        help='docs directory, used to build annotation paths')
    parser.add_argument('--recheck-attempts', type=int, default=2)
    parser.add_argument('--recheck-delay', type=int, default=15)
    parser.add_argument('--timeout', type=int, default=30)
    parser.add_argument('--fail-on-transient', default='true')
    args = parser.parse_args()

    fail_on_transient = args.fail_on_transient.lower() == 'true'

    with open(args.output_json) as file_handle:
        entries = [json.loads(line) for line in file_handle if line.strip()]

    links = []
    for entry in entries:
        if entry.get('status') != 'broken':
            continue
        links.append({
            'path': os.path.normpath(os.path.join(args.docs_path,
                                                  entry['filename'])),
            'lineno': entry.get('lineno') or 1,
            'uri': entry['uri'],
            'info': entry.get('info', ''),
            'category': classify(entry.get('code'), entry.get('info')),
            'result': 'broken',
            'attempts': 0,
        })

    total = sum(1 for entry in entries
                if entry.get('status') not in ('unchecked', 'ignored', 'local'))
    transient = [link for link in links if link['category'] == TRANSIENT]
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
            annotate('warning', link,
                     f'Link {link["uri"]} failed linkcheck but recovered '
                     f'after {link["attempts"]} re-check(s)')
            warnings.append(link)
        elif link['result'] == 'unreachable' and not fail_on_transient:
            annotate('warning', link,
                     f'Link {link["uri"]} is still unreachable after '
                     f'{link["attempts"]} re-check(s): {link["info"]}')
            warnings.append(link)
        else:
            annotate('error', link,
                     f'Broken link {link["uri"]}: {link["info"]}')
            failing.append(link)

    summary_file = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary_file:
        write_summary(summary_file, failing, warnings, total)

    still_broken = [link for link in links if link['result'] != 'recovered']
    set_output('broken_links_found', 'true' if still_broken else 'false')

    print(f'Result: {len(failing)} failing, {len(warnings)} warning(s)')
    return 1 if failing else 0


if __name__ == '__main__':
    sys.exit(main())
