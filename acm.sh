#!/usr/bin/env sh
set -eu

REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$REPO_ROOT"

ACM_COMMAND=
ACM_EXPECT_ROOT=0
for ACM_ARG in "$@"; do
    if [ "$ACM_EXPECT_ROOT" -eq 1 ]; then
        ACM_EXPECT_ROOT=0
        continue
    fi
    case "$ACM_ARG" in
        --root) ACM_EXPECT_ROOT=1 ;;
        --root=*) ;;
        -*) ;;
        *) ACM_COMMAND=$ACM_ARG; break ;;
    esac
done

if [ "$ACM_COMMAND" = "web" ]; then
    ACM_PYTHON=$(sh "$REPO_ROOT/tools/ensure-python313.sh" "$REPO_ROOT") || exit $?
    exec "$ACM_PYTHON" -m tools.acm_agent "$@"
fi

exec python3 -m tools.acm_agent "$@"
