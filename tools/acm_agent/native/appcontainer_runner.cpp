#define UNICODE
#define _UNICODE
#include <windows.h>
#include <sddl.h>
#include <userenv.h>

#ifndef PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES
#define PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES ProcThreadAttributeValue(9, FALSE, TRUE, FALSE)
#endif

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

namespace fs = std::filesystem;

using CreateAppContainerProfileFn = HRESULT (WINAPI *)(
    PCWSTR, PCWSTR, PCWSTR, PSID_AND_ATTRIBUTES, DWORD, PSID *);
using DeriveAppContainerSidFn = HRESULT (WINAPI *)(PCWSTR, PSID *);
using GetAppContainerFolderPathFn = HRESULT (WINAPI *)(PCWSTR, PWSTR *);

class ScopedProfileMutex {
public:
    ScopedProfileMutex() {
        handle_ = CreateMutexW(
            nullptr, FALSE, L"Local\\AcmAgent.StressSandbox.ProfileLock");
        if (handle_) {
            const DWORD wait = WaitForSingleObject(handle_, 30000);
            acquired_ = wait == WAIT_OBJECT_0 || wait == WAIT_ABANDONED;
        }
    }
    ~ScopedProfileMutex() {
        release();
        if (handle_) CloseHandle(handle_);
    }
    bool acquired() const { return acquired_; }
    void release() {
        if (acquired_) {
            ReleaseMutex(handle_);
            acquired_ = false;
        }
    }
    ScopedProfileMutex(const ScopedProfileMutex &) = delete;
    ScopedProfileMutex &operator=(const ScopedProfileMutex &) = delete;

private:
    HANDLE handle_ = nullptr;
    bool acquired_ = false;
};

static HRESULT ResolveAppContainerSid(
    CreateAppContainerProfileFn create_profile,
    DeriveAppContainerSidFn derive_sid,
    PSID *sid) {
    if (!sid) return E_POINTER;
    *sid = nullptr;
    HRESULT last = E_FAIL;
    for (int attempt = 0; attempt < 4; ++attempt) {
        PSID created = nullptr;
        const HRESULT created_hr = create_profile(
            L"AcmAgent.StressSandbox", L"ACM Agent Stress Sandbox",
            L"No-network sandbox for explicitly approved stress helpers",
            nullptr, 0, &created);
        if (SUCCEEDED(created_hr) && created) {
            *sid = created;
            return created_hr;
        }
        if (created) FreeSid(created);

        // CreateAppContainerProfile can report more than ERROR_ALREADY_EXISTS
        // while the shared profile is concurrently opened by another launcher.
        // Deriving the SID is read-only and is the authoritative fallback for
        // every create failure, not only one exact HRESULT spelling.
        PSID derived = nullptr;
        const HRESULT derived_hr = derive_sid(L"AcmAgent.StressSandbox", &derived);
        if (SUCCEEDED(derived_hr) && derived) {
            *sid = derived;
            return derived_hr;
        }
        if (derived) FreeSid(derived);
        last = FAILED(derived_hr) ? derived_hr : created_hr;
        Sleep(25u * static_cast<DWORD>(attempt + 1));
    }
    return last;
}

static HRESULT ResolveAppContainerFolder(
    GetAppContainerFolderPathFn get_folder,
    PCWSTR sid_text,
    PWSTR *folder) {
    if (!folder) return E_POINTER;
    *folder = nullptr;
    HRESULT last = E_FAIL;
    for (int attempt = 0; attempt < 4; ++attempt) {
        PWSTR current = nullptr;
        const HRESULT hr = get_folder(sid_text, &current);
        if (SUCCEEDED(hr) && current) {
            *folder = current;
            return hr;
        }
        if (current) CoTaskMemFree(current);
        last = hr;
        Sleep(25u * static_cast<DWORD>(attempt + 1));
    }
    return last;
}

static std::wstring Quote(const std::wstring &value) {
    if (value.find_first_of(L" \t\"") == std::wstring::npos) return value;
    std::wstring result = L"\"";
    size_t slashes = 0;
    for (wchar_t ch : value) {
        if (ch == L'\\') {
            ++slashes;
        } else if (ch == L'\"') {
            result.append(slashes * 2 + 1, L'\\');
            result.push_back(ch);
            slashes = 0;
        } else {
            result.append(slashes, L'\\');
            slashes = 0;
            result.push_back(ch);
        }
    }
    result.append(slashes * 2, L'\\');
    result.push_back(L'\"');
    return result;
}

static std::vector<unsigned char> ReadBytes(const fs::path &path) {
    std::ifstream input(path, std::ios::binary);
    return std::vector<unsigned char>(std::istreambuf_iterator<char>(input), {});
}

static bool WriteBytes(const fs::path &path, const std::vector<unsigned char> &data, size_t limit) {
    std::ofstream output(path, std::ios::binary | std::ios::trunc);
    if (!output) return false;
    output.write(reinterpret_cast<const char *>(data.data()), static_cast<std::streamsize>(std::min(limit, data.size())));
    return static_cast<bool>(output);
}

static std::wstring ArgValue(int argc, wchar_t **argv, const std::wstring &name) {
    for (int i = 1; i + 1 < argc; ++i) {
        if (argv[i] == name) return argv[i + 1];
    }
    return L"";
}

static unsigned long long ParseNumber(const std::wstring &value, unsigned long long fallback) {
    try { return std::stoull(value); } catch (...) { return fallback; }
}

static int WriteMeta(const fs::path &path, DWORD returncode, bool timed_out, bool limited, DWORD elapsed, DWORD launcher_error) {
    std::ofstream output(path, std::ios::binary | std::ios::trunc);
    if (!output) return 20;
    output << "returncode=" << returncode << "\n"
           << "timed_out=" << (timed_out ? 1 : 0) << "\n"
           << "output_limited=" << (limited ? 1 : 0) << "\n"
           << "elapsed_ms=" << elapsed << "\n"
           << "launcher_error=" << launcher_error << "\n";
    return 0;
}

int wmain(int argc, wchar_t **argv) {
    ScopedProfileMutex profile_mutex;
    if (!profile_mutex.acquired()) {
        std::wcerr << L"AppContainer profile mutex unavailable\n";
        return 21;
    }
    if (argc == 2 && std::wstring(argv[1]) == L"--probe") {
        HMODULE userenv = LoadLibraryW(L"userenv.dll");
        if (!userenv) return 2;
        auto create_profile = reinterpret_cast<CreateAppContainerProfileFn>(
            GetProcAddress(userenv, "CreateAppContainerProfile"));
        auto derive_sid = reinterpret_cast<DeriveAppContainerSidFn>(
            GetProcAddress(userenv, "DeriveAppContainerSidFromAppContainerName"));
        auto get_folder = reinterpret_cast<GetAppContainerFolderPathFn>(
            GetProcAddress(userenv, "GetAppContainerFolderPath"));
        bool ok = create_profile && derive_sid && get_folder;
        PSID sid = nullptr;
        if (ok) {
            HRESULT hr = ResolveAppContainerSid(create_profile, derive_sid, &sid);
            ok = SUCCEEDED(hr) && sid;
            if (!ok) std::wcerr << L"resolve AppContainer SID failed: 0x"
                                << std::hex << static_cast<unsigned long>(hr) << L"\n";
        }
        if (sid) FreeSid(sid);
        FreeLibrary(userenv);
        return ok ? 0 : 3;
    }
    if (argc < 4 || std::wstring(argv[1]) != L"--run") return 4;

    HMODULE userenv = LoadLibraryW(L"userenv.dll");
    if (!userenv) return 2;
    auto create_profile = reinterpret_cast<CreateAppContainerProfileFn>(
        GetProcAddress(userenv, "CreateAppContainerProfile"));
    auto derive_sid = reinterpret_cast<DeriveAppContainerSidFn>(
        GetProcAddress(userenv, "DeriveAppContainerSidFromAppContainerName"));
    auto get_folder = reinterpret_cast<GetAppContainerFolderPathFn>(
        GetProcAddress(userenv, "GetAppContainerFolderPath"));
    if (!create_profile || !derive_sid || !get_folder) {
        FreeLibrary(userenv);
        return 3;
    }

    const std::wstring stdin_host = ArgValue(argc, argv, L"--stdin");
    const std::wstring stdout_host = ArgValue(argc, argv, L"--stdout");
    const std::wstring stderr_host = ArgValue(argc, argv, L"--stderr");
    const std::wstring meta_host = ArgValue(argc, argv, L"--meta");
    const DWORD timeout_ms = static_cast<DWORD>(ParseNumber(ArgValue(argc, argv, L"--timeout-ms"), 2000));
    const SIZE_T memory_bytes = static_cast<SIZE_T>(ParseNumber(ArgValue(argc, argv, L"--memory"), 512ULL << 20));
    const size_t stdout_limit = static_cast<size_t>(ParseNumber(ArgValue(argc, argv, L"--stdout-limit"), 2ULL << 20));
    const size_t stderr_limit = static_cast<size_t>(ParseNumber(ArgValue(argc, argv, L"--stderr-limit"), 2ULL << 20));
    int command_index = -1;
    for (int i = 2; i < argc; ++i) if (std::wstring(argv[i]) == L"--") { command_index = i + 1; break; }
    if (stdin_host.empty() || stdout_host.empty() || stderr_host.empty() || meta_host.empty() || command_index < 0 || command_index >= argc) return 5;

    PSID app_sid = nullptr;
    HRESULT hr = ResolveAppContainerSid(create_profile, derive_sid, &app_sid);
    if (FAILED(hr) || !app_sid) {
        std::wcerr << L"resolve AppContainer SID failed: 0x"
                   << std::hex << static_cast<unsigned long>(hr) << L"\n";
        return 6;
    }

    LPWSTR sid_text = nullptr;
    if (!ConvertSidToStringSidW(app_sid, &sid_text)) { FreeSid(app_sid); return 7; }
    PWSTR profile_folder_raw = nullptr;
    hr = ResolveAppContainerFolder(get_folder, sid_text, &profile_folder_raw);
    LocalFree(sid_text);
    if (FAILED(hr) || !profile_folder_raw) {
        std::wcerr << L"resolve AppContainer folder failed: 0x"
                   << std::hex << static_cast<unsigned long>(hr) << L"\n";
        FreeSid(app_sid);
        return 8;
    }
    fs::path profile_folder(profile_folder_raw);
    CoTaskMemFree(profile_folder_raw);
    fs::path run_dir = profile_folder / (L"run-" + std::to_wstring(GetCurrentProcessId()) + L"-" + std::to_wstring(GetTickCount64()));
    std::error_code filesystem_error;
    fs::create_directories(run_dir, filesystem_error);
    if (filesystem_error) { FreeSid(app_sid); return 9; }

    fs::path source_exe(argv[command_index]);
    fs::path target_exe = run_dir / L"target.exe";
    fs::copy_file(source_exe, target_exe, fs::copy_options::overwrite_existing, filesystem_error);
    if (filesystem_error) { fs::remove_all(run_dir); FreeSid(app_sid); return 10; }
    fs::path stdin_path = run_dir / L"stdin.bin";
    fs::path stdout_path = run_dir / L"stdout.bin";
    fs::path stderr_path = run_dir / L"stderr.bin";
    WriteBytes(stdin_path, ReadBytes(stdin_host), static_cast<size_t>(-1));

    HANDLE stdin_handle = CreateFileW(stdin_path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    HANDLE stdout_handle = CreateFileW(stdout_path.c_str(), GENERIC_WRITE | GENERIC_READ, FILE_SHARE_READ, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    HANDLE stderr_handle = CreateFileW(stderr_path.c_str(), GENERIC_WRITE | GENERIC_READ, FILE_SHARE_READ, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (stdin_handle == INVALID_HANDLE_VALUE || stdout_handle == INVALID_HANDLE_VALUE || stderr_handle == INVALID_HANDLE_VALUE) {
        fs::remove_all(run_dir); FreeSid(app_sid); return 11;
    }
    SetHandleInformation(stdin_handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT);
    SetHandleInformation(stdout_handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT);
    SetHandleInformation(stderr_handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT);

    SIZE_T attribute_size = 0;
    InitializeProcThreadAttributeList(nullptr, 1, 0, &attribute_size);
    auto attributes = reinterpret_cast<LPPROC_THREAD_ATTRIBUTE_LIST>(HeapAlloc(GetProcessHeap(), 0, attribute_size));
    if (!attributes || !InitializeProcThreadAttributeList(attributes, 1, 0, &attribute_size)) return 12;
    SECURITY_CAPABILITIES capabilities{};
    capabilities.AppContainerSid = app_sid;
    if (!UpdateProcThreadAttribute(attributes, 0, PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
                                   &capabilities, sizeof(capabilities), nullptr, nullptr)) return 13;

    STARTUPINFOEXW startup{};
    startup.StartupInfo.cb = sizeof(startup);
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    startup.StartupInfo.hStdInput = stdin_handle;
    startup.StartupInfo.hStdOutput = stdout_handle;
    startup.StartupInfo.hStdError = stderr_handle;
    startup.lpAttributeList = attributes;
    std::wstring command_line = Quote(target_exe.wstring());
    for (int i = command_index + 1; i < argc; ++i) command_line += L" " + Quote(argv[i]);
    const wchar_t *system_root = _wgetenv(L"SystemRoot");
    std::wstring environment = L"SystemRoot=" + std::wstring(system_root ? system_root : L"C:\\Windows");
    environment.push_back(L'\0');
    environment += L"TEMP=" + run_dir.wstring();
    environment.push_back(L'\0');
    environment += L"TMP=" + run_dir.wstring();
    environment.push_back(L'\0');
    environment += L"LOCALAPPDATA=" + profile_folder.wstring();
    environment.push_back(L'\0');
    environment += L"APPDATA=" + profile_folder.wstring();
    environment.push_back(L'\0');
    environment += L"USERPROFILE=" + profile_folder.wstring();
    environment.push_back(L'\0');
    environment.push_back(L'\0');

    PROCESS_INFORMATION process{};
    const ULONGLONG started = GetTickCount64();
    BOOL created = FALSE;
    DWORD launcher_error = ERROR_SUCCESS;
    for (int attempt = 0; attempt < 4 && !created; ++attempt) {
        process = PROCESS_INFORMATION{};
        std::vector<wchar_t> mutable_command(
            command_line.begin(), command_line.end());
        mutable_command.push_back(L'\0');
        created = CreateProcessW(
            target_exe.c_str(), mutable_command.data(), nullptr, nullptr, TRUE,
            CREATE_SUSPENDED | CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT,
            environment.data(), run_dir.c_str(), &startup.StartupInfo, &process);
        launcher_error = created ? ERROR_SUCCESS : GetLastError();
        if (!created) Sleep(25u * static_cast<DWORD>(attempt + 1));
    }
    // Profile/SID/folder resolution and AppContainer process creation share a
    // global Windows boundary.  Once CreateProcessW has either succeeded or
    // exhausted its retries, the child/job execution itself is independent.
    profile_mutex.release();
    HANDLE job = CreateJobObjectW(nullptr, nullptr);
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE |
                                              JOB_OBJECT_LIMIT_PROCESS_MEMORY |
                                              JOB_OBJECT_LIMIT_PROCESS_TIME |
                                              JOB_OBJECT_LIMIT_ACTIVE_PROCESS;
    limits.BasicLimitInformation.ActiveProcessLimit = 1;
    limits.BasicLimitInformation.PerProcessUserTimeLimit.QuadPart =
        static_cast<LONGLONG>(timeout_ms) * 10000LL;
    limits.ProcessMemoryLimit = memory_bytes;
    bool timed_out = false;
    DWORD returncode = 127;
    if (created && job && SetInformationJobObject(job, JobObjectExtendedLimitInformation, &limits, sizeof(limits)) &&
        AssignProcessToJobObject(job, process.hProcess)) {
        ResumeThread(process.hThread);
        const ULONGLONG deadline = GetTickCount64() + timeout_ms;
        for (;;) {
            const DWORD wait = WaitForSingleObject(process.hProcess, 20);
            if (wait == WAIT_OBJECT_0) break;
            std::error_code size_error;
            const auto out_size = fs::file_size(stdout_path, size_error);
            size_error.clear();
            const auto err_size = fs::file_size(stderr_path, size_error);
            if ((!size_error && (out_size > stdout_limit || err_size > stderr_limit)) ||
                GetTickCount64() >= deadline) {
                timed_out = GetTickCount64() >= deadline;
                TerminateJobObject(job, timed_out ? 124 : 125);
                WaitForSingleObject(process.hProcess, 2000);
                break;
            }
        }
        GetExitCodeProcess(process.hProcess, &returncode);
    }
    if (process.hThread) CloseHandle(process.hThread);
    if (process.hProcess) CloseHandle(process.hProcess);
    CloseHandle(stdin_handle); CloseHandle(stdout_handle); CloseHandle(stderr_handle);
    if (job) CloseHandle(job);
    DeleteProcThreadAttributeList(attributes); HeapFree(GetProcessHeap(), 0, attributes);
    FreeSid(app_sid);

    const auto stdout_data = ReadBytes(stdout_path);
    const auto stderr_data = ReadBytes(stderr_path);
    const bool limited = stdout_data.size() > stdout_limit || stderr_data.size() > stderr_limit;
    WriteBytes(stdout_host, stdout_data, stdout_limit);
    WriteBytes(stderr_host, stderr_data, stderr_limit);
    const DWORD elapsed = static_cast<DWORD>(GetTickCount64() - started);
    const int result = WriteMeta(meta_host, returncode, timed_out, limited, elapsed, launcher_error);
    fs::remove_all(run_dir, filesystem_error);
    FreeLibrary(userenv);
    return result;
}
