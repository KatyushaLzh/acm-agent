#!/usr/bin/env sh
set -u

PYTHON=${1-}
REPO_ROOT=${2-}
OS_RELEASE_FILE=${ACM_SECRET_SERVICE_OS_RELEASE_FILE-/etc/os-release}

warn_unavailable() {
    printf '%s\n' 'Warning: Linux Secret Service 不可用；API Key 持久化将继续被拒绝，核心 Dashboard 仍会启动。' >&2
}

optional_diagnostic_hint() {
    printf '%s\n' '可选诊断工具：可自行安装 libsecret-tools 以使用 secret-tool；它不是安全持久化的必需组件。' >&2
}

probe_secret_service() {
    [ -x "$PYTHON" ] || return 1
    [ -n "$REPO_ROOT" ] || return 1
    "$PYTHON" - "$REPO_ROOT" >/dev/null 2>&1 <<'PY'
import sys

sys.path.insert(0, sys.argv[1])
from tools.acm_agent.credentials import probe_linux_secret_service

probe_linux_secret_service()
PY
}

is_debian_or_ubuntu() {
    [ -r "$OS_RELEASE_FILE" ] || return 1
    distro_id=$(sed -n 's/^ID=//p' "$OS_RELEASE_FILE" | sed -n '1p' | tr -d '"')
    case "$distro_id" in
        debian|ubuntu) return 0 ;;
    esac
    return 1
}

prompt_keyring_packages_install() {
    printf '%s\n' '检测到 Debian/Ubuntu 可尝试补充 Secret Service 系统组件。' >&2
    printf '%s\n' '是否执行以下命令？' >&2
    printf '%s\n' 'sudo apt-get install --no-install-recommends gnome-keyring seahorse' >&2
    while :; do
        printf '%s' '执行安装？ [y/n] ' >&2
        if ! IFS= read -r answer; then
            printf '\n%s\n' '未收到输入，跳过系统包安装；核心 Dashboard 将继续启动。' >&2
            return 1
        fi
        case "$answer" in
            y|Y) return 0 ;;
            n|N)
                printf '%s\n' '已跳过系统包安装；核心 Dashboard 将继续启动。' >&2
                return 1
                ;;
            *) printf '%s\n' '请输入 y 或 n。' >&2 ;;
        esac
    done
}

if ! is_debian_or_ubuntu || ! command -v apt-get >/dev/null 2>&1; then
    exit 0
fi

if probe_secret_service; then
    exit 0
fi

warn_unavailable

if ! prompt_keyring_packages_install; then
    exit 0
fi

if ! command -v sudo >/dev/null 2>&1; then
    printf '%s\n' '未找到 sudo，未执行系统包安装；不会尝试代输密码。' >&2
    optional_diagnostic_hint
    exit 0
fi

if ! sudo apt-get install --no-install-recommends gnome-keyring seahorse; then
    printf '%s\n' 'gnome-keyring 与 seahorse 安装命令失败；未执行 apt update，核心 Dashboard 将继续启动。' >&2
    optional_diagnostic_hint
    exit 0
fi

if probe_secret_service; then
    printf '%s\n' 'Linux Secret Service 已通过 D-Bus 与无敏感数据 keyring 探测。' >&2
else
    printf '%s\n' 'gnome-keyring 与 seahorse 安装命令已完成，但 Secret Service 仍不可用：需启动/解锁用户钥匙环。' >&2
    optional_diagnostic_hint
fi
exit 0
