#include <windows.h>
#include <audioclient.h>
#include <mmdeviceapi.h>
#include <functiondiscoverykeys_devpkey.h>
#include <cstdio>
#include <cwchar>
#include <string>
#include <vector>

struct DiagnosticError { const char *operation; HRESULT status; };
static void require(HRESULT hr, const char *operation) {
    if (FAILED(hr)) throw DiagnosticError{operation, hr};
}
template<class T> struct Interface {
    T *value = nullptr;
    Interface() = default;
    Interface(const Interface&) = delete;
    Interface& operator=(const Interface&) = delete;
    ~Interface() { clear(); }
    T* operator->() const { return value; }
    void clear() { if (value) { value->Release(); value = nullptr; } }
    void** out() { return reinterpret_cast<void**>(&value); }
};
static LONGLONG counter() {
    LARGE_INTEGER t;
    QueryPerformanceCounter(&t);
    return t.QuadPart;
}
static std::string utf8(const wchar_t *text) {
    if (!text) return {};
    int n = WideCharToMultiByte(CP_UTF8, 0, text, -1, nullptr, 0, nullptr, nullptr);
    if (n<1) return {};
    std::vector<char> buffer(static_cast<size_t>(n));
    WideCharToMultiByte(CP_UTF8, 0, text, -1, buffer.data(), n, nullptr, nullptr);
    return std::string(buffer.data());
}
static void list_devices(IMMDeviceEnumerator *enumerator) {
    Interface<IMMDeviceCollection> devices;
    require(enumerator->EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE, &devices.value),"enumerate_render");
    UINT count=0;
    require(devices->GetCount(&count),"render_count");
    for (UINT n=0; n<count; ++n) {
        Interface<IMMDevice> device;
        Interface<IPropertyStore> props;
        require(devices->Item(n,&device.value),"render_endpoint");
        require(device->OpenPropertyStore(STGM_READ,&props.value),"endpoint_properties");
        LPWSTR id=nullptr;
        require(device->GetId(&id),"endpoint_id");
        PROPVARIANT name; PropVariantInit(&name);
        HRESULT hr=props->GetValue(PKEY_Device_FriendlyName,&name);
        std::printf("ENDPOINT\t%s\t%s\n",utf8(id).c_str(),SUCCEEDED(hr)&&name.vt==VT_LPWSTR?utf8(name.pwszVal).c_str():"");
        PropVariantClear(&name);
        CoTaskMemFree(id);
    }
}
static void queue_silence(IAudioRenderClient *render,UINT32 frames) {
    if (!frames) return;
    BYTE *data=nullptr;
    require(render->GetBuffer(frames,&data),"get_render_buffer");
    require(render->ReleaseBuffer(frames,AUDCLNT_BUFFERFLAGS_SILENT),"release_silent_frames");
}
static void run_cycle(IMMDevice *device, unsigned cycle, LONGLONG hz) {
    Interface<IAudioClient> client;
    require(device->Activate(__uuidof(IAudioClient),CLSCTX_INPROC_SERVER,nullptr,client.out()),"activate");
    WAVEFORMATEX format={WAVE_FORMAT_PCM,2,48000,192000,4,16,0};
    require(client->IsFormatSupported(AUDCLNT_SHAREMODE_EXCLUSIVE,&format,nullptr),"exclusive_pcm16_stereo_48000");
    REFERENCE_TIME period=0,minimum=0;
    require(client->GetDevicePeriod(&period,&minimum),"get_period");
    const LONGLONG opening=counter();
    REFERENCE_TIME duration=1600000;
    HRESULT init=client->Initialize(AUDCLNT_SHAREMODE_EXCLUSIVE,0,duration,0,&format,nullptr);
    if(init==AUDCLNT_E_BUFFER_SIZE_NOT_ALIGNED) {
        UINT32 aligned=0; require(client->GetBufferSize(&aligned),"get_aligned_frames");
        client.clear();
        require(device->Activate(__uuidof(IAudioClient),CLSCTX_INPROC_SERVER,nullptr,client.out()),"reactivate_aligned");
        duration=(10000000LL*aligned+format.nSamplesPerSec/2)/format.nSamplesPerSec;
        init=client->Initialize(AUDCLNT_SHAREMODE_EXCLUSIVE,0,duration,0,&format,nullptr);
    }
    require(init,"initialize_exclusive");
    const LONGLONG initialized=counter();
    UINT32 frames=0;require(client->GetBufferSize(&frames),"get_initial_buffer_size");
    Interface<IAudioRenderClient> render;
    Interface<IAudioClock> clock;
    require(client->GetService(__uuidof(IAudioRenderClient),render.out()),"render_service");
    require(client->GetService(__uuidof(IAudioClock),clock.out()),"audio_clock_service");
    UINT64 clock_hz=0;require(clock->GetFrequency(&clock_hz),"get_audio_clock_rate");
    queue_silence(render.value,frames);
    const LONGLONG start=counter();
    require(client->Start(),"start");
    const LONGLONG started=counter();
    UINT64 pos=0,first_pos=0,written=frames; LONGLONG first_seen=0;
    try {
        do {
            UINT32 padding=0;require(client->GetCurrentPadding(&padding),"get_padding");
            if (padding>frames) throw DiagnosticError{"invalid_padding",E_UNEXPECTED};
            queue_silence(render.value,frames-padding);written+=frames-padding;
            HRESULT read=clock->GetPosition(&pos,nullptr);
            require(read,"read_position");
            if (read==S_OK && pos && !first_seen) { first_seen=counter();first_pos=pos; }
            Sleep(6);
        } while (counter()-started < hz/3);
    } catch (...) { client->Stop(); throw; }
    const LONGLONG stopping=counter();
    require(client->Stop(),"stop");
    const LONGLONG stopped=counter();
    auto ms=[hz](LONGLONG ticks){return 1000.0*static_cast<double>(ticks)/static_cast<double>(hz);};
    std::printf("CYCLE %u init_ms=%.3f start_ms=%.3f first_position_ms=%.3f stop_ms=%.3f buffer_frames=%u written=%llu audio_hz=%llu first=%llu last=%llu default_period_100ns=%lld min_period_100ns=%lld\n",
        cycle,ms(initialized-opening),ms(started-start),first_seen?ms(first_seen-start):-1.0,ms(stopped-stopping),frames,written,clock_hz,first_pos,pos,period,minimum);
    if(!clock_hz||!first_seen||pos<=first_pos) throw DiagnosticError{"audio_clock_did_not_advance",E_FAIL};
}
int wmain(int argc,wchar_t **argv) {
    std::setvbuf(stdout,nullptr,_IONBF,0);
    if(argc!=2 && argc!=3)return 2;
    HRESULT init=CoInitializeEx(nullptr,COINIT_APARTMENTTHREADED);
    if(FAILED(init))return 3;
    int exit_code=0;
    try {
        Interface<IMMDeviceEnumerator> enumerator;
        require(CoCreateInstance(__uuidof(MMDeviceEnumerator),nullptr,CLSCTX_INPROC_SERVER,__uuidof(IMMDeviceEnumerator),enumerator.out()),"create_enumerator");
        if(argc==2 && !std::wcscmp(argv[1],L"--list")) list_devices(enumerator.value);
        else if(argc==3 && !std::wcscmp(argv[1],L"--run")) {
            Interface<IMMDevice> device;require(enumerator->GetDevice(argv[2],&device.value),"select_endpoint");
            LARGE_INTEGER hz;QueryPerformanceFrequency(&hz);
            std::printf("FORMAT exclusive 48000 16 2 silent QPC=%lld\n",hz.QuadPart);
            for(unsigned i=0;i<7;++i){run_cycle(device.value,i,hz.QuadPart);Sleep(250);}
        } else exit_code=2;
    } catch (const DiagnosticError& err) {
        std::printf("ERROR %s 0x%08lx\n",err.operation,static_cast<unsigned long>(err.status));exit_code=4;
    }
    CoUninitialize();
    return exit_code;
}
