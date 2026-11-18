#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <evntrace.h>
#include <evntcons.h>
#include <evntprov.h>
#include <cstdio>
#include <cstring>
#include <cwchar>
#include <vector>

static const GUID gpio = {0x55ab77f6, 0xfa04, 0x43ef, {0xaf,0x45,0x68,0x8f,0xbf,0x50,0x04,0x82}};
static constexpr wchar_t sessionName[] = L"SwitchGpio01a1ec52TouchIRQ";
static unsigned int events;

static void WINAPI record(EVENT_RECORD *event)
{
    if (memcmp(&event->EventHeader.ProviderId, &gpio, sizeof(gpio)) != 0)
        return;
    ++events;
    if (events > 8192 || event->UserDataLength > 256)
        return;
    const auto *bytes = static_cast<const unsigned char *>(event->UserData);
    std::printf("event,%lld,%u,%lu,%u,", event->EventHeader.TimeStamp.QuadPart,
                event->BufferContext.ProcessorNumber, event->EventHeader.ThreadId,
                event->EventHeader.EventDescriptor.Id);
    for (unsigned int i = 0; i < event->UserDataLength; ++i)
        std::printf("%02x", bytes[i]);
    std::putchar('\n');
}

static int readTrace(wchar_t *path)
{
    EVENT_TRACE_LOGFILEW file{};
    file.LogFileName = path;
    file.ProcessTraceMode = PROCESS_TRACE_MODE_EVENT_RECORD | PROCESS_TRACE_MODE_RAW_TIMESTAMP;
    file.EventRecordCallback = record;
    TRACEHANDLE handle = OpenTraceW(&file);
    if (handle == INVALID_PROCESSTRACE_HANDLE) {
        std::printf("open_error,%lu\n", GetLastError());
        return 9;
    }
    std::printf("qpc_frequency,%lld\n", file.LogfileHeader.PerfFreq.QuadPart);
    const ULONG status = ProcessTrace(&handle, 1, nullptr, nullptr);
    CloseTrace(handle);
    std::printf("process_trace,%lu,event_count,%u\n", status, events);
    return status != ERROR_SUCCESS || events > 8192 ? 10 : 0;
}

static DWORD restartTouch()
{
    wchar_t directory[MAX_PATH];
    if (!GetSystemDirectoryW(directory, MAX_PATH))
        return GetLastError();
    wchar_t path[MAX_PATH];
    if (std::swprintf(path, MAX_PATH, L"%ls\\pnputil.exe", directory) < 0)
        return ERROR_INSUFFICIENT_BUFFER;
    wchar_t args[] = L"pnputil.exe /restart-device \"ACPI\\SWOA0502\\0\"";
    STARTUPINFOW startup{};
    PROCESS_INFORMATION process{};
    startup.cb = sizeof(startup);
    if (!CreateProcessW(path, args, nullptr, nullptr, FALSE, CREATE_NO_WINDOW, nullptr,
                        nullptr, &startup, &process))
        return GetLastError();
    const DWORD wait = WaitForSingleObject(process.hProcess, 25000);
    DWORD result = wait == WAIT_TIMEOUT ? ERROR_TIMEOUT : ERROR_GEN_FAILURE;
    if (wait == WAIT_OBJECT_0 && !GetExitCodeProcess(process.hProcess, &result))
        result = GetLastError();
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    return result;
}

int wmain(int argc, wchar_t **argv)
{
    if (argc == 3 && std::wcscmp(argv[1], L"--read") == 0)
        return readTrace(argv[2]);
    if (argc != 2 || GetFileAttributesW(argv[1]) != INVALID_FILE_ATTRIBUTES)
        return 2;
    const size_t pathBytes = (std::wcslen(argv[1]) + 1) * sizeof(wchar_t);
    std::vector<unsigned char> bytes(sizeof(EVENT_TRACE_PROPERTIES) + sizeof(sessionName) + pathBytes);
    auto *properties = reinterpret_cast<EVENT_TRACE_PROPERTIES *>(bytes.data());
    properties->Wnode.BufferSize = static_cast<ULONG>(bytes.size());
    properties->Wnode.Flags = WNODE_FLAG_TRACED_GUID;
    properties->Wnode.ClientContext = 1;
    properties->BufferSize = 32;
    properties->MinimumBuffers = 4;
    properties->MaximumBuffers = 16;
    properties->MaximumFileSize = 8;
    properties->FlushTimer = 1;
    properties->LogFileMode = EVENT_TRACE_FILE_MODE_SEQUENTIAL;
    properties->LoggerNameOffset = sizeof(*properties);
    properties->LogFileNameOffset = sizeof(*properties) + sizeof(sessionName);
    memcpy(bytes.data() + properties->LoggerNameOffset, sessionName, sizeof(sessionName));
    memcpy(bytes.data() + properties->LogFileNameOffset, argv[1], pathBytes);
    TRACEHANDLE session{};
    ULONG status = StartTraceW(&session, sessionName, properties);
    if (status != ERROR_SUCCESS) {
        std::printf("start_error,%lu\n", status);
        return 3;
    }
    status = EnableTraceEx2(session, &gpio, EVENT_CONTROL_CODE_ENABLE_PROVIDER, TRACE_LEVEL_VERBOSE,
                            ~0ULL, 0, 2000, nullptr);
    DWORD restart = ERROR_NOT_READY;
    if (status == ERROR_SUCCESS) {
        Sleep(150);
        restart = restartTouch();
        Sleep(1700);
    }
    const ULONG stopped = ControlTraceW(session, nullptr, properties, EVENT_TRACE_CONTROL_STOP);
    std::printf("capture,enable,%lu,restart,%lu,stop,%lu,events_lost,%lu,buffers_lost,%lu\n",
                status, restart, stopped, properties->EventsLost, properties->LogBuffersLost);
    if (status || restart || stopped || properties->EventsLost || properties->LogBuffersLost)
        return 4;
    return readTrace(argv[1]);
}
