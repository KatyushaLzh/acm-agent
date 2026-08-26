#!/usr/bin/env sh
set -eu

# Keep this bootstrap self-contained: it must work before Python is available.
PYTHON_VERSION=3.13.15
UV_VERSION=0.12.5
UV_DOMESTIC_BASE=https://uv.agentsmirror.com/github/astral-sh/uv/releases/download
UV_OFFICIAL_BASE=https://github.com/astral-sh/uv/releases/download
PYTHON_DOMESTIC_MIRROR=https://registry.npmmirror.com/-/binary/python-build-standalone
PYTHON_OFFICIAL_MIRROR=https://github.com/astral-sh/python-build-standalone/releases/download

REPO_ROOT=${1-}
if [ -z "$REPO_ROOT" ]; then
    REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
fi
RUNTIME_DIR=$REPO_ROOT/.acm/runtime
BOOTSTRAP_DIR=$RUNTIME_DIR/bootstrap
PYTHON_DIR=$RUNTIME_DIR/python
CACHE_DIR=$RUNTIME_DIR/cache
WEB_LOCK_FILE=$REPO_ROOT/tools/requirements-web-unix.lock
WEB_ENVS_DIR=$RUNTIME_DIR/web-envs
TEMP_DIR=
WEB_TEMP_DIR=
INSTALL_LOCK_DIR=
INSTALL_LOCK_OWNED=0

cleanup_temp() {
    if [ -n "$TEMP_DIR" ] && [ -d "$TEMP_DIR" ]; then
        rm -rf "$TEMP_DIR"
    fi
    if [ -n "$WEB_TEMP_DIR" ] && [ -d "$WEB_TEMP_DIR" ]; then
        rm -rf "$WEB_TEMP_DIR"
    fi
    if [ "$INSTALL_LOCK_OWNED" -eq 1 ] && [ -n "$INSTALL_LOCK_DIR" ]; then
        rm -f "$INSTALL_LOCK_DIR/owner"
        rmdir "$INSTALL_LOCK_DIR" 2>/dev/null || true
    fi
}
trap cleanup_temp EXIT
trap 'cleanup_temp; exit 130' HUP INT TERM

probe_python() {
    candidate=$1
    [ -x "$candidate" ] || return 1
    resolved=$(
        "$candidate" -c '
import os
import sqlite3
import ssl
import sys

if sys.version_info[:2] != (3, 13) or sys.version_info.releaselevel != "final":
    raise SystemExit(1)
print(os.path.realpath(sys.executable))
' 2>/dev/null
    ) || return 1
    [ -n "$resolved" ] || return 1
    case "$resolved" in
        *'
'*) return 1 ;;
    esac
    SELECTED_PYTHON=$resolved
    return 0
}

probe_command() {
    command_name=$1
    candidate=$(command -v "$command_name" 2>/dev/null || true)
    [ -n "$candidate" ] || return 1
    probe_python "$candidate"
}

find_python313() {
    SELECTED_PYTHON=
    for command_name in python3.13 python3 python; do
        if probe_command "$command_name"; then
            return 0
        fi
    done

    for candidate in \
        "$PYTHON_DIR"/*/bin/python3.13 \
        "$PYTHON_DIR"/*/bin/python; do
        if probe_python "$candidate"; then
            return 0
        fi
    done
    return 1
}

warn_tkinter_if_missing() {
    if ! "$SELECTED_PYTHON" -c 'import tkinter' >/dev/null 2>&1; then
        printf '%s\n' 'Warning: tkinter is unavailable; native file picker operations will be unavailable.' >&2
    fi
}

prompt_install() {
    printf '%s\n' 'Python 3.13 is required to start the ACM Dashboard, but no usable final 3.13.x interpreter was found.' >&2
    while :; do
        printf '%s' 'Download and install project-local Python 3.13.15 now? [y/n] ' >&2
        if ! IFS= read -r answer; then
            printf '\n%s\n' 'No input received. Web startup cancelled; install Python 3.13 manually or rerun in an interactive terminal.' >&2
            return 1
        fi
        case "$answer" in
            y|Y) return 0 ;;
            n|N)
                printf '%s\n' 'Web startup cancelled.' >&2
                return 1
                ;;
            *) printf '%s\n' 'Please enter y or n.' >&2 ;;
        esac
    done
}

detect_target() {
    os_name=${ACM_BOOTSTRAP_UNAME_S-}
    machine=${ACM_BOOTSTRAP_UNAME_M-}
    [ -n "$os_name" ] || os_name=$(uname -s 2>/dev/null || true)
    [ -n "$machine" ] || machine=$(uname -m 2>/dev/null || true)

    case "$machine" in
        x86_64|amd64) arch=x86_64 ;;
        arm64|aarch64) arch=aarch64 ;;
        *)
            printf 'Unsupported Unix architecture for automatic Python installation: %s/%s.\n' "$os_name" "$machine" >&2
            return 1
            ;;
    esac

    case "$os_name" in
        Darwin)
            UV_TARGET=$arch-apple-darwin
            ;;
        Linux)
            libc=${ACM_BOOTSTRAP_LIBC-}
            if [ -z "$libc" ]; then
                if command -v getconf >/dev/null 2>&1 && getconf GNU_LIBC_VERSION >/dev/null 2>&1; then
                    libc=gnu
                else
                    ldd_output=$(ldd --version 2>&1 || true)
                    case "$ldd_output" in
                        *musl*|*MUSL*) libc=musl ;;
                        *GLIBC*|*glibc*|*"GNU libc"*|*"GNU C Library"*) libc=gnu ;;
                        *)
                            for musl_loader in /lib/ld-musl-*.so.1 /usr/lib/ld-musl-*.so.1; do
                                if [ -e "$musl_loader" ]; then
                                    libc=musl
                                    break
                                fi
                            done
                            ;;
                    esac
                fi
            fi
            case "$libc" in
                gnu|glibc) libc=gnu ;;
                musl) ;;
                *)
                    printf 'Unsupported Linux libc for automatic Python installation: %s.\n' "$libc" >&2
                    return 1
                    ;;
            esac
            UV_TARGET=$arch-unknown-linux-$libc
            ;;
        *)
            printf 'Unsupported Unix platform for automatic Python installation: %s/%s.\n' "$os_name" "$machine" >&2
            return 1
            ;;
    esac

    UV_ASSET=uv-$UV_TARGET.tar.gz
    case "$UV_TARGET" in
        aarch64-apple-darwin) UV_SHA256=5bb0e5fe008a773c3dbcb97ff79cd89e1241464fe9d2f986d52ad8f1b037bd62 ;;
        x86_64-apple-darwin) UV_SHA256=b3b2137477cf96c9686ebfb71524614cec780c673fd73e59bce099aef02e70e8 ;;
        aarch64-unknown-linux-gnu) UV_SHA256=9bf43b4d1a07665bf64d4c4e710930b382321a785e0eb10aac07f46471f86a31 ;;
        x86_64-unknown-linux-gnu) UV_SHA256=68a509da24b06b4223a1c0175fb5eb5bc79342b76cbeff0cfe51ac3f5b17b6b2 ;;
        aarch64-unknown-linux-musl) UV_SHA256=8767a0e77f2cd45436401b1b42bf7e9ed5a4a91a74a5305d6fe93249d0f6dbc5 ;;
        x86_64-unknown-linux-musl) UV_SHA256=a4742988791c9aeae68c78150d6cba762062ad2a47e53738c2779d2b596bfcdb ;;
        *) return 1 ;;
    esac
}

download_to() {
    url=$1
    output=$2
    if command -v curl >/dev/null 2>&1; then
        curl --proto '=https' --tlsv1.2 --location --fail --silent --show-error \
            --connect-timeout 10 --max-time 120 --retry 2 --retry-delay 1 \
            --output "$output" "$url"
    elif command -v wget >/dev/null 2>&1; then
        wget -q --timeout=30 --tries=2 -O "$output" "$url"
    else
        printf '%s\n' 'Neither curl nor wget is available; cannot download the Python bootstrap.' >&2
        return 1
    fi
}

download_verified_uv() {
    source_name=$1
    url=$2
    output=$3
    if ! download_to "$url" "$output"; then
        printf '%s uv download failed.\n' "$source_name" >&2
        return 1
    fi
    actual_sha256=$(sha256_file "$output") || return 1
    if [ "$actual_sha256" != "$UV_SHA256" ]; then
        printf '%s uv archive SHA-256 mismatch for %s.\n' "$source_name" "$UV_ASSET" >&2
        return 1
    fi
}

sha256_file() {
    file=$1
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$file" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$file" | awk '{print $1}'
    elif command -v openssl >/dev/null 2>&1; then
        openssl dgst -sha256 "$file" | awk '{print $NF}'
    else
        printf '%s\n' 'No SHA-256 utility found (sha256sum, shasum, or openssl is required).' >&2
        return 1
    fi
}

obtain_uv() {
    detect_target || return 1
    mkdir -p "$BOOTSTRAP_DIR" "$PYTHON_DIR" "$CACHE_DIR"
    UV_BIN=$BOOTSTRAP_DIR/uv-$UV_VERSION-$UV_TARGET

    if [ -x "$UV_BIN" ] && [ "$("$UV_BIN" --version 2>/dev/null || true)" = "uv $UV_VERSION" ]; then
        return 0
    fi

    TEMP_DIR=$(mktemp -d "$BOOTSTRAP_DIR/.uv-$UV_VERSION-$UV_TARGET.XXXXXXXX") || {
        printf '%s\n' 'Failed to create a unique temporary bootstrap directory.' >&2
        return 1
    }
    archive=$TEMP_DIR/$UV_ASSET
    domestic_url=$UV_DOMESTIC_BASE/$UV_VERSION/$UV_ASSET
    official_url=$UV_OFFICIAL_BASE/$UV_VERSION/$UV_ASSET

    printf 'Downloading uv %s from the domestic mirror...\n' "$UV_VERSION" >&2
    if ! download_verified_uv Domestic "$domestic_url" "$archive"; then
        printf '%s\n' 'Domestic uv source failed verification; retrying the official GitHub release.' >&2
        rm -f "$archive"
        if ! download_verified_uv Official "$official_url" "$archive"; then
            printf '%s\n' 'Both uv sources failed download or verification; Python was not installed.' >&2
            return 1
        fi
    fi
    if ! tar -xzf "$archive" -C "$TEMP_DIR"; then
        printf '%s\n' 'Failed to extract the verified uv archive.' >&2
        return 1
    fi
    extracted_uv=$TEMP_DIR/uv-$UV_TARGET/uv
    if [ ! -f "$extracted_uv" ]; then
        printf '%s\n' 'The verified uv archive did not contain the expected executable.' >&2
        return 1
    fi
    chmod 755 "$extracted_uv"
    mv -f "$extracted_uv" "$UV_BIN"
    if [ "$("$UV_BIN" --version 2>/dev/null || true)" != "uv $UV_VERSION" ]; then
        rm -f "$UV_BIN"
        printf '%s\n' 'The installed uv bootstrap failed its version check.' >&2
        return 1
    fi
    cleanup_temp
    TEMP_DIR=
}

uv_python_install() {
    mirror=$1
    UV_CACHE_DIR=$CACHE_DIR \
    UV_PYTHON_INSTALL_DIR=$PYTHON_DIR \
    UV_NO_CONFIG=1 \
        "$UV_BIN" python install "$PYTHON_VERSION" \
        --managed-python --no-bin --no-config --mirror "$mirror"
}

install_python313() {
    obtain_uv || return 1
    printf 'Installing project-local Python %s from npmmirror...\n' "$PYTHON_VERSION" >&2
    if ! uv_python_install "$PYTHON_DOMESTIC_MIRROR"; then
        printf '%s\n' 'Domestic Python download failed; retrying the official Astral release source.' >&2
        if ! uv_python_install "$PYTHON_OFFICIAL_MIRROR"; then
            printf '%s\n' 'Both Python download sources failed; Web startup was not attempted.' >&2
            return 1
        fi
    fi

    managed_python=$(
        UV_CACHE_DIR=$CACHE_DIR \
        UV_PYTHON_INSTALL_DIR=$PYTHON_DIR \
        UV_NO_CONFIG=1 \
            "$UV_BIN" python find "$PYTHON_VERSION" --managed-python --no-config 2>/dev/null
    ) || {
        printf '%s\n' 'uv installed Python but could not resolve its exact interpreter path.' >&2
        return 1
    }
    if ! probe_python "$managed_python"; then
        printf '%s\n' 'Installed Python failed the 3.13 final-version or core ssl/sqlite3 checks.' >&2
        return 1
    fi
}

web_lock_digest() {
    [ -f "$WEB_LOCK_FILE" ] || return 1
    "$SELECTED_PYTHON" -c '
import hashlib
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
print(hashlib.sha256(path.read_bytes()).hexdigest())
' "$WEB_LOCK_FILE" 2>/dev/null
}

validate_web_environment() {
    environment_dir=$1
    environment_python=$environment_dir/bin/python
    [ -x "$environment_python" ] || return 1
    [ -f "$environment_dir/.acm-web-ready" ] || return 1
    [ "$(cat "$environment_dir/.acm-web-ready" 2>/dev/null || true)" = "$WEB_LOCK_DIGEST" ] || return 1
    "$environment_python" -c '
import importlib.metadata
import sqlite3
import ssl
import sys

if sys.version_info[:2] != (3, 13) or sys.version_info.releaselevel != "final":
    raise SystemExit(1)

expected = {
    "jaraco.classes": "3.4.0",
    "jaraco.context": "6.1.2",
    "jaraco.functools": "4.6.0",
    "keyring": "25.7.0",
    "more-itertools": "11.1.0",
}
if sys.platform == "linux":
    expected.update({
        "cffi": "2.1.1",
        "cryptography": "49.0.0",
        "jeepney": "0.9.0",
        "pycparser": "3.0",
        "secretstorage": "3.5.0",
    })
for distribution, version in expected.items():
    if importlib.metadata.version(distribution) != version:
        raise SystemExit(1)
' >/dev/null 2>&1
}

release_install_lock() {
    if [ "$INSTALL_LOCK_OWNED" -eq 1 ]; then
        rm -f "$INSTALL_LOCK_DIR/owner"
        rmdir "$INSTALL_LOCK_DIR" 2>/dev/null || true
        INSTALL_LOCK_OWNED=0
    fi
}

acquire_install_lock() {
    attempts=0
    while [ "$attempts" -lt 30 ]; do
        if mkdir "$INSTALL_LOCK_DIR" 2>/dev/null; then
            printf '%s\n' "$$" > "$INSTALL_LOCK_DIR/owner"
            INSTALL_LOCK_OWNED=1
            return 0
        fi
        if validate_web_environment "$WEB_ENV_DIR"; then
            return 2
        fi
        owner=$(cat "$INSTALL_LOCK_DIR/owner" 2>/dev/null || true)
        case "$owner" in
            ''|*[!0-9]*) ;;
            *)
                if ! kill -0 "$owner" 2>/dev/null; then
                    rm -f "$INSTALL_LOCK_DIR/owner"
                    rmdir "$INSTALL_LOCK_DIR" 2>/dev/null || true
                    continue
                fi
                ;;
        esac
        attempts=$((attempts + 1))
        sleep 1
    done
    return 1
}

sync_web_environment() {
    mkdir -p "$WEB_ENVS_DIR" "$CACHE_DIR"
    INSTALL_LOCK_DIR=$WEB_ENVS_DIR/.install-$WEB_LOCK_DIGEST.lock
    if acquire_install_lock; then
        :
    else
        lock_status=$?
        if [ "$lock_status" -eq 2 ] && validate_web_environment "$WEB_ENV_DIR"; then
            SELECTED_PYTHON=$WEB_ENV_DIR/bin/python
            return 0
        fi
        printf '%s\n' 'Warning: timed out waiting for the project-local Web dependency environment; starting the core Dashboard without secure Unix credential storage.' >&2
        return 1
    fi

    # Re-check after taking the lock because another launcher may have finished
    # between our initial ready check and lock acquisition.
    if validate_web_environment "$WEB_ENV_DIR"; then
        SELECTED_PYTHON=$WEB_ENV_DIR/bin/python
        release_install_lock
        return 0
    fi

    if ! obtain_uv; then
        printf '%s\n' 'Warning: the verified uv bootstrap is unavailable; starting the core Dashboard without secure Unix credential storage. The launcher will retry next time.' >&2
        release_install_lock
        return 1
    fi
    WEB_TEMP_DIR=$(mktemp -d "$WEB_ENVS_DIR/.web-$WEB_LOCK_DIGEST.XXXXXXXX") || {
        printf '%s\n' 'Warning: could not create the project-local Web dependency environment.' >&2
        release_install_lock
        return 1
    }

    printf '%s\n' 'Preparing the project-local Unix Dashboard dependencies...' >&2
    if ! UV_CACHE_DIR=$CACHE_DIR UV_NO_CONFIG=1 \
        "$UV_BIN" venv "$WEB_TEMP_DIR" --python "$SELECTED_PYTHON" \
        --no-python-downloads --no-config; then
        printf '%s\n' 'Warning: Web dependency environment creation failed; starting the core Dashboard without secure Unix credential storage.' >&2
        release_install_lock
        return 1
    fi
    if ! UV_CACHE_DIR=$CACHE_DIR UV_NO_CONFIG=1 UV_HTTP_TIMEOUT=30 UV_HTTP_RETRIES=2 \
        "$UV_BIN" pip sync "$WEB_LOCK_FILE" \
        --python "$WEB_TEMP_DIR/bin/python" --require-hashes --only-binary=:all: \
        --no-python-downloads --no-config; then
        printf '%s\n' 'Warning: pinned Web dependencies could not be downloaded or verified; starting the core Dashboard without secure Unix credential storage. The launcher will retry next time.' >&2
        release_install_lock
        return 1
    fi
    printf '%s\n' "$WEB_LOCK_DIGEST" > "$WEB_TEMP_DIR/.acm-web-ready"
    if ! validate_web_environment "$WEB_TEMP_DIR"; then
        printf '%s\n' 'Warning: the installed Web dependencies failed their exact-version check; starting the core Dashboard without secure Unix credential storage.' >&2
        release_install_lock
        return 1
    fi

    previous_dir=$WEB_ENVS_DIR/.previous-$WEB_LOCK_DIGEST-$$
    had_previous=0
    if [ -e "$WEB_ENV_DIR" ]; then
        if ! mv "$WEB_ENV_DIR" "$previous_dir"; then
            printf '%s\n' 'Warning: the existing Web dependency environment could not be replaced; starting the core Dashboard without secure Unix credential storage.' >&2
            release_install_lock
            return 1
        fi
        had_previous=1
    fi
    if ! mv "$WEB_TEMP_DIR" "$WEB_ENV_DIR"; then
        if [ "$had_previous" -eq 1 ]; then
            mv "$previous_dir" "$WEB_ENV_DIR" 2>/dev/null || true
        fi
        printf '%s\n' 'Warning: the verified Web dependency environment could not be activated; starting the core Dashboard without secure Unix credential storage.' >&2
        release_install_lock
        return 1
    fi
    WEB_TEMP_DIR=
    if [ "$had_previous" -eq 1 ]; then
        rm -rf "$previous_dir"
    fi
    SELECTED_PYTHON=$WEB_ENV_DIR/bin/python
    release_install_lock
}

select_web_python() {
    WEB_LOCK_DIGEST=$(web_lock_digest) || {
        printf '%s\n' 'Warning: the Unix Web dependency lock is missing or unreadable; starting the core Dashboard without secure Unix credential storage.' >&2
        return 1
    }
    WEB_ENV_DIR=$WEB_ENVS_DIR/$WEB_LOCK_DIGEST
    if validate_web_environment "$WEB_ENV_DIR"; then
        SELECTED_PYTHON=$WEB_ENV_DIR/bin/python
        return 0
    fi
    sync_web_environment
}

if ! find_python313; then
    prompt_install || exit 1
    install_python313 || exit 1
fi

warn_tkinter_if_missing
BASE_PYTHON=$SELECTED_PYTHON
if ! select_web_python; then
    SELECTED_PYTHON=$BASE_PYTHON
fi
printf '%s\n' "$SELECTED_PYTHON"
