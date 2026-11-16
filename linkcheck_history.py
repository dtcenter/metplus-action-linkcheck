#!/usr/bin/env python3
"""
Read, merge, and write the link check history.

The history records the result of every absolute link the action checks, keyed
by URL, in a single JSON file held on an orphan branch of the repository being
checked. It lets the action tell a link that has been failing for weeks from
one that failed for the first time today, so that a transient failure only
fails the job once it has been failing longer than a grace period.

Each record holds:

    last_seen      when the URL was last found in the documentation
    last_checked   when the URL was last checked
    last_success   when the URL last passed
    first_failure  when the current run of failures started
    failure_count  consecutive checks that have failed
    last_status    "ok", or how the link last failed
    last_code      HTTP status code of the last failure, 0 if there was none
    last_info      error text of the last failure

first_failure, last_code, and last_info are removed and failure_count is reset
whenever the link passes. Records for URLs that have not been seen in the
documentation for the retention period are dropped.

Run as a script, it merges one run of results into the history file read from
that branch, which is how the action retries a push that collided with a
concurrent run:

    linkcheck_history.py merge --history history.json --results results.json \
        --output merged.json --retention 90
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

TIME_FORMAT = '%Y-%m-%dT%H:%M:%SZ'

# written in this order so that the file diffs cleanly from run to run
FIELD_ORDER = ('last_seen', 'last_checked', 'last_success', 'first_failure',
               'failure_count', 'last_status', 'last_code', 'last_info')

# removed when a link passes
FAILURE_FIELDS = ('first_failure', 'last_code', 'last_info')


def now_utc():
    """Return the current time, to the second, in UTC."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def format_time(value):
    """Format a datetime the way the history file stores it."""
    return value.strftime(TIME_FORMAT)


def parse_time(value):
    """Parse a time from the history file, or return None if it is unusable."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, TIME_FORMAT).replace(
            tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def load_history(path):
    """Return the records in a history file, or {} if there are none.

    A missing file is not an error: it is what the first run of a repository
    sees. A file that cannot be parsed is reported and treated as empty, so
    that a damaged history does not fail every documentation build until
    someone deletes it by hand.
    """
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path) as file_handle:
            data = json.load(file_handle)
    except (OSError, ValueError) as err:
        print(f'Could not read the link check history from {path} ({err}), '
              f'starting a new one')
        return {}
    if isinstance(data, dict) and isinstance(data.get('links'), dict):
        data = data['links']
    if not isinstance(data, dict):
        print(f'The link check history in {path} is not a JSON object, '
              f'starting a new one')
        return {}
    return {url: record for url, record in data.items()
            if isinstance(url, str) and isinstance(record, dict)}


def ordered(record):
    """Return a record with its fields in a fixed order."""
    result = {name: record[name] for name in FIELD_ORDER if name in record}
    result.update({name: value for name, value in sorted(record.items())
                   if name not in result})
    return result


def save_history(path, history):
    """Write the history, sorted by URL, as formatted JSON."""
    data = {url: ordered(record) for url, record in sorted(history.items())}
    with open(path, 'w') as file_handle:
        json.dump(data, file_handle, indent=2)
        file_handle.write('\n')


def load_results(path):
    """Return (checked_at, results) from a results file written by a run."""
    with open(path) as file_handle:
        data = json.load(file_handle)
    checked_at = parse_time(data.get('checked_at')) or now_utc()
    results = data.get('links')
    return checked_at, results if isinstance(results, dict) else {}


def save_results(path, checked_at, results):
    """Write one run of results for the push step to merge and retry."""
    with open(path, 'w') as file_handle:
        json.dump({'checked_at': format_time(checked_at), 'links': results},
                  file_handle, indent=2, sort_keys=True)
        file_handle.write('\n')


def keep(record, cutoff):
    """Return True for a record of a URL seen since the retention cutoff."""
    seen = (parse_time(record.get('last_seen'))
            or parse_time(record.get('last_checked')))
    return seen is None or seen >= cutoff


def merge(history, results, checked_at, retention_days):
    """Merge one run of results into the history.

    results maps each URL to a dict with 'checked' and, for checked URLs,
    'ok' and, for failures, 'status', 'code', and 'info'. A URL that appears
    in the documentation but is not checked (one matching linkcheck_ignore,
    say) only has its last_seen time refreshed, so that it is not dropped by
    the retention cutoff while it is still in the documentation.
    """
    merged = {url: dict(record) for url, record in history.items()}
    timestamp = format_time(checked_at)
    for url, result in sorted(results.items()):
        record = merged.setdefault(url, {})
        record['last_seen'] = timestamp
        if not result.get('checked'):
            continue
        record['last_checked'] = timestamp
        if result.get('ok'):
            record['last_success'] = timestamp
            record['last_status'] = 'ok'
            record['failure_count'] = 0
            for name in FAILURE_FIELDS:
                record.pop(name, None)
        else:
            record['last_status'] = result.get('status') or 'broken'
            record['failure_count'] = (record.get('failure_count') or 0) + 1
            record.setdefault('first_failure', timestamp)
            record['last_code'] = result.get('code') or 0
            record['last_info'] = result.get('info') or ''
    if retention_days > 0:
        cutoff = checked_at - timedelta(days=retention_days)
        merged = {url: record for url, record in merged.items()
                  if keep(record, cutoff)}
    return merged


def evaluate(record, is_new, checked_at, grace_days, min_failures):
    """Decide whether a failing link has been failing long enough to fail.

    record is the link's history after this run's result has been merged in,
    so failure_count includes this run. is_new is True for a URL that had no
    record before this run. Returns a dict describing the failure for the
    report, with 'fail' set only when the current run of failures started more
    than grace_days ago and has been seen at least min_failures times.
    """
    entry = {
        'new': is_new,
        'first_failure': record.get('first_failure'),
        'last_success': record.get('last_success'),
        'failure_count': record.get('failure_count') or 0,
        'min_failures': min_failures,
        'days': 0,
        'fails_on': None,
        'expired': False,
        'fail': False,
    }
    first_failure = parse_time(entry['first_failure'])
    if first_failure is None:
        return entry
    entry['days'] = max((checked_at - first_failure).days, 0)
    deadline = first_failure + timedelta(days=grace_days)
    entry['fails_on'] = format_time(deadline)
    # a link that has outlasted the grace period but has not yet been seen
    # failing often enough waits for the checks rather than for the date
    entry['expired'] = checked_at >= deadline
    entry['fail'] = (entry['expired']
                     and entry['failure_count'] >= min_failures)
    return entry


def main():
    parser = argparse.ArgumentParser(
        description='Merge a run of link check results into the history.')
    subparsers = parser.add_subparsers(dest='command', required=True)
    merge_parser = subparsers.add_parser(
        'merge', help='merge one run of results into the history')
    merge_parser.add_argument('--history', default='',
                              help='history file read from the history '
                                   'branch; a missing file starts a new one')
    merge_parser.add_argument('--results', required=True,
                              help='results file written by process_linkcheck')
    merge_parser.add_argument('--output', required=True,
                              help='where to write the merged history')
    merge_parser.add_argument('--retention', type=int, default=90,
                              help='days to keep a record for a URL that is '
                                   'no longer in the documentation')
    args = parser.parse_args()

    history = load_history(args.history)
    checked_at, results = load_results(args.results)
    merged = merge(history, results, checked_at, args.retention)
    save_history(args.output, merged)

    added = sum(1 for url in results if url not in history)
    dropped = len(history) + added - len(merged)
    message = (f'Merged {len(results)} link(s) into {len(history)} history '
               f'record(s): {len(merged)} record(s) written')
    if added:
        message += f', {added} new'
    if dropped > 0:
        message += f', {dropped} dropped'
    print(message)
    return 0


if __name__ == '__main__':
    sys.exit(main())
