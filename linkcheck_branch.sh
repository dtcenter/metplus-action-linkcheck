#!/usr/bin/env bash
#
# Read and write the link check history on an orphan branch of the repository
# being checked.
#
#   linkcheck_branch.sh fetch   read the history file from the history branch
#   linkcheck_branch.sh push    merge this run's results in and push them back
#
# The branch holds the history file and nothing else, the way gh-pages holds
# built documentation, so it never appears in the history of the branches
# being built. The commit is built with plumbing commands and pushed straight
# from the object database, so neither command touches the working tree or the
# checked out branch.
#
# Configured through the environment:
#
#   HISTORY_BRANCH     branch holding the history
#   HISTORY_FILE       path to the history file on that branch
#   HISTORY_TOKEN      token used to read and write the branch
#   HISTORY_RETENTION  days to keep a record for a URL no longer in the docs
#   HISTORY_WORK       directory for the history, results, and merged files
#   HISTORY_REMOTE     remote to read and write (default: this repository)
#   ACTION_PATH        directory holding linkcheck_history.py
#   PYTHON             python used to merge the history (default: python3)
#
# Neither command fails the job. A history that cannot be read is reported as
# a warning and the run decides from its own results instead; a history that
# cannot be written is reported as a warning and the run is otherwise
# unaffected.

HISTORY_BRANCH="${HISTORY_BRANCH:-}"
HISTORY_FILE="${HISTORY_FILE:-linkcheck-history.json}"
HISTORY_TOKEN="${HISTORY_TOKEN:-}"
HISTORY_RETENTION="${HISTORY_RETENTION:-90}"
HISTORY_WORK="${HISTORY_WORK:-${RUNNER_TEMP:-/tmp}/linkcheck-history}"
ACTION_PATH="${ACTION_PATH:-$(cd "$(dirname "$0")" && pwd)}"
PYTHON="${PYTHON:-python3}"
SERVER_URL="${GITHUB_SERVER_URL:-https://github.com}"
REMOTE="${HISTORY_REMOTE:-${SERVER_URL}/${GITHUB_REPOSITORY:-}.git}"
PUSH_ATTEMPTS="${HISTORY_PUSH_ATTEMPTS:-3}"
PUSH_DELAY="${HISTORY_PUSH_DELAY:-5}"

LOCAL_REF='refs/linkcheck/history'
HISTORY_JSON="$HISTORY_WORK/history.json"
RESULTS_JSON="$HISTORY_WORK/results.json"
MERGED_JSON="$HISTORY_WORK/merged.json"
INDEX_FILE="$HISTORY_WORK/index"

git_auth_args=()
if [ -n "$HISTORY_TOKEN" ]; then
  git_auth_args=(-c "http.extraheader=AUTHORIZATION: basic $(printf '%s' \
      "x-access-token:${HISTORY_TOKEN}" | base64 | tr -d '\n')")
fi

warn() {
  printf '::warning title=Linkcheck::%s\n' "$1"
}

set_output() {
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    printf '%s=%s\n' "$1" "$2" >> "$GITHUB_OUTPUT"
  else
    printf '%s=%s\n' "$1" "$2"
  fi
}

# Run git against the remote with the token, if one was given
git_remote() {
  git ${git_auth_args[@]+"${git_auth_args[@]}"} "$@"
}

# Fetch the history branch into a local ref. Returns 0 if it was fetched,
# 1 if the branch does not exist yet, and 2 if it could not be read.
fetch_branch() {
  git_remote ls-remote --exit-code --heads "$REMOTE" \
      "refs/heads/${HISTORY_BRANCH}" > /dev/null 2>&1
  local status=$?
  if [ "$status" -eq 2 ]; then
    # The branch is gone. Drop any ref left behind by an earlier run of this
    # workspace, so that the commit this run builds starts a new history
    # rather than being parented on a commit the remote no longer has, which
    # it would refuse to accept.
    git update-ref -d "$LOCAL_REF" 2> /dev/null
    return 1
  fi
  if [ "$status" -ne 0 ]; then
    return 2
  fi
  git_remote fetch --quiet --depth=1 "$REMOTE" \
      "+refs/heads/${HISTORY_BRANCH}:${LOCAL_REF}" || return 2
  return 0
}

# Write the history file from the fetched branch, if the branch holds one
read_file() {
  rm -f "$HISTORY_JSON"
  if git cat-file -e "${LOCAL_REF}:${HISTORY_FILE}" 2> /dev/null; then
    git show "${LOCAL_REF}:${HISTORY_FILE}" > "$HISTORY_JSON" || return 1
  fi
  return 0
}

history_message() {
  local ref="${GITHUB_REF_NAME:-}"
  printf 'Update the link check history\n'
  if [ -n "$ref" ]; then
    printf '\nChecked by %s on %s.\n' "${GITHUB_WORKFLOW:-linkcheck}" "$ref"
  fi
  if [ -n "${GITHUB_RUN_ID:-}" ]; then
    printf '%s/%s/actions/runs/%s\n' "$SERVER_URL" "$GITHUB_REPOSITORY" \
        "$GITHUB_RUN_ID"
  fi
}

# Build a commit holding the merged history, without touching the working
# tree or the index of the branch being built. Prints the commit, or nothing
# if the history is unchanged.
build_commit() {
  local blob tree parent message
  blob=$(git hash-object -w "$MERGED_JSON") || return 1
  rm -f "$INDEX_FILE"
  GIT_INDEX_FILE="$INDEX_FILE" git read-tree --empty || return 1
  GIT_INDEX_FILE="$INDEX_FILE" git update-index --add \
      --cacheinfo "100644,${blob},${HISTORY_FILE}" || return 1
  tree=$(GIT_INDEX_FILE="$INDEX_FILE" git write-tree) || return 1
  parent=$(git rev-parse --quiet --verify "$LOCAL_REF" 2> /dev/null)
  message=$(history_message)
  if [ -n "$parent" ]; then
    if [ "$(git rev-parse "${parent}^{tree}")" = "$tree" ]; then
      return 0
    fi
    git commit-tree "$tree" -p "$parent" -m "$message" || return 1
  else
    git commit-tree "$tree" -m "$message" || return 1
  fi
  return 0
}

cmd_fetch() {
  mkdir -p "$HISTORY_WORK"
  rm -f "$HISTORY_JSON" "$RESULTS_JSON" "$MERGED_JSON"
  set_output 'path' "$HISTORY_JSON"

  if [ -z "$HISTORY_BRANCH" ]; then
    set_output 'enabled' 'false'
    return 0
  fi
  if ! git rev-parse --git-dir > /dev/null 2>&1; then
    warn "The workspace is not a git repository, so the link check history \
is unavailable. Check out the repository before running this action."
    set_output 'enabled' 'false'
    return 0
  fi

  fetch_branch
  case $? in
    0)
      if ! read_file; then
        warn "Could not read ${HISTORY_FILE} from branch ${HISTORY_BRANCH}, \
so the link check history is not used for this run."
        set_output 'enabled' 'false'
        return 0
      fi
      if [ -f "$HISTORY_JSON" ]; then
        echo "Read ${HISTORY_FILE} from branch ${HISTORY_BRANCH} \
($(wc -c < "$HISTORY_JSON" | tr -d ' ') bytes)"
      else
        echo "Branch ${HISTORY_BRANCH} has no ${HISTORY_FILE} yet, starting \
a new link check history"
      fi
      ;;
    1)
      echo "Branch ${HISTORY_BRANCH} does not exist yet, starting a new link \
check history"
      ;;
    *)
      warn "Could not read branch ${HISTORY_BRANCH}, so the link check \
history is not used for this run."
      set_output 'enabled' 'false'
      return 0
      ;;
  esac
  set_output 'enabled' 'true'
}

cmd_push() {
  if [ ! -f "$RESULTS_JSON" ]; then
    echo 'No link check results to record in the link check history'
    return 0
  fi

  export GIT_AUTHOR_NAME="${HISTORY_AUTHOR_NAME:-github-actions[bot]}"
  export GIT_AUTHOR_EMAIL="${HISTORY_AUTHOR_EMAIL:-41898282+github-actions[bot]@users.noreply.github.com}"
  export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME"
  export GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"

  local attempt=1
  local commit
  while [ "$attempt" -le "$PUSH_ATTEMPTS" ]; do
    # a push rejected by a concurrent run is merged into its result and retried
    if [ "$attempt" -gt 1 ]; then
      fetch_branch
      if [ $? -eq 2 ] || ! read_file; then
        warn "Could not re-read branch ${HISTORY_BRANCH} to merge this run \
into it. The link check history was not updated."
        return 0
      fi
    fi

    if ! "$PYTHON" "${ACTION_PATH}/linkcheck_history.py" merge \
        --history "$HISTORY_JSON" --results "$RESULTS_JSON" \
        --output "$MERGED_JSON" --retention "$HISTORY_RETENTION"; then
      warn 'Could not merge this run into the link check history.'
      return 0
    fi

    commit=$(build_commit)
    if [ $? -ne 0 ]; then
      warn 'Could not build the link check history commit.'
      return 0
    fi
    if [ -z "$commit" ]; then
      echo 'The link check history is unchanged'
      return 0
    fi

    if git_remote push --quiet "$REMOTE" \
        "${commit}:refs/heads/${HISTORY_BRANCH}"; then
      git update-ref "$LOCAL_REF" "$commit"
      echo "Updated ${HISTORY_FILE} on branch ${HISTORY_BRANCH} \
($(git rev-parse --short "$commit"))"
      return 0
    fi

    echo "Could not push to branch ${HISTORY_BRANCH} (attempt ${attempt} of \
${PUSH_ATTEMPTS})"
    attempt=$((attempt + 1))
    if [ "$attempt" -le "$PUSH_ATTEMPTS" ]; then
      sleep "$PUSH_DELAY"
    fi
  done

  warn "Could not update the link check history on branch ${HISTORY_BRANCH}. \
The job result is unaffected. Writing the history needs \
'permissions: contents: write' in the calling workflow."
}

case "${1:-}" in
  fetch) cmd_fetch ;;
  push) cmd_push ;;
  *)
    echo "usage: $(basename "$0") fetch|push" >&2
    exit 2
    ;;
esac
exit 0
