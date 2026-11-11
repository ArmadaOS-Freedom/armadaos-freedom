#!/usr/bin/env python3
'''Runtime tests for the lease panel-sleep mirror added by patch 0043.

The broker's lease socket thread must not read cv_drm_sleep_screens directly,
because ConVar<T>::m_Value is a plain T. Patch 0043 keeps a mutex-guarded mirror
for the broker and an atomic mirror for a companion. This test extracts the
added globals, helpers, reconnect block and the drm_prepare sleep expression
from the patch itself and runs them against small stubs, so the code under test
is the patched source and not a reimplementation. It needs g++ (the gamescope
build container) and no DRM hardware.

Limitation: the stubs replace the lease socket, so ordering is observed through
lease_send_control; the 100 ms window in the contention case is only a
scheduling allowance for the second thread, not a correctness margin.
'''

import pathlib
import shutil
import subprocess
import sys
import tempfile

NL = chr(10)

ROOT = pathlib.Path(__file__).resolve().parents[1]
PATCH = ROOT / 'packages' / 'gamescope' / 'patches' / (
    '0043-drm-mirror-internal-screen-sleep-to-the-lease-companion.patch'
)
BACKEND = 'src/Backends/DRMBackend.cpp'
MAIN_HPP = 'src/main.hpp'
WLSERVER = 'src/wlserver.cpp'


def parse_hunks(text):
    files = {}
    current = None
    hunk = None
    for line in text.splitlines():
        if line.startswith('diff --git '):
            path = line.split(' b/', 1)[-1]
            current = []
            files[path] = current
            hunk = None
        elif line.startswith('@@'):
            hunk = {'header': line, 'lines': []}
            current.append(hunk)
        elif hunk is not None:
            hunk['lines'].append(line)
    return files


def added(hunk):
    return [line[1:] for line in hunk['lines'] if line.startswith('+')]


def all_added(files):
    return [
        line
        for hunks in files.values()
        for hunk in hunks
        for line in added(hunk)
    ]


def only(hunks, needle):
    found = [hunk for hunk in hunks if any(needle in line for line in added(hunk))]
    if len(found) != 1:
        raise AssertionError(
            'expected exactly one hunk adding %r, found %d' % (needle, len(found))
        )
    return found[0]


def check_source(text):
    files = parse_hunks(text)
    for path in (BACKEND, MAIN_HPP, WLSERVER):
        if path not in files:
            raise AssertionError('patch does not touch %s' % path)
    backend = files[BACKEND]

    globals_hunk = only(backend, 'static bool g_bLeaseClientPanelSleep = false;')
    helper_hunk = only(backend, 'static void drm_lease_send_sleep( bool bSleep )')
    sync_hunk = only(backend, 'lease_send_panel_sleep_locked( g_bLeaseClientPanelSleep );')
    bsleep_hunk = only(backend, 'g_bDrmLeasePanelSleep.load()')
    callback_hunk = only(backend, 'drm_lease_send_sleep( cv );')

    globals_adds = added(globals_hunk)
    helper_adds = added(helper_hunk)
    sync_adds = added(sync_hunk)
    bsleep_adds = added(bsleep_hunk)
    callback_adds = added(callback_hunk)

    init = [line for line in globals_adds if line.startswith('static bool g_bLeaseClientPanelSleep')]
    assert len(init) == 1 and init[0].rstrip().endswith('= false;'), init
    mirror = [line for line in globals_adds if 'static std::atomic<bool> g_bDrmLeasePanelSleep' in line]
    assert mirror == ['static std::atomic<bool> g_bDrmLeasePanelSleep{ false };'], mirror
    assert any(line.startswith('extern gamescope::ConVar<bool> cv_drm_sleep_screens[];') for line in globals_adds)
    print('PASS source: broker replay state and companion mirror both start false')

    assert 'lease_socket_thread_run' in sync_hunk['header'], sync_hunk['header']
    assert sync_adds, sync_adds
    assert not any('cv_drm_sleep_screens' in line for line in sync_adds), sync_adds
    assert not any('drm_lease_send_sleep(' in line for line in sync_adds), sync_adds
    assert any('std::lock_guard lock( g_leaseClientMutex );' in line for line in sync_adds), sync_adds
    assert any('lease_send_panel_sleep_locked( g_bLeaseClientPanelSleep );' in line for line in sync_adds), sync_adds
    print('PASS source: reconnect uses the mutex-guarded helper, no ConVar read')

    helper = NL.join(helper_adds)
    assert 'cv_drm_sleep_screens' not in helper, helper
    lock_at = helper.index('std::lock_guard lock( g_leaseClientMutex );')
    store_at = helper.index('g_bLeaseClientPanelSleep = bSleep;')
    send_at = helper.index('lease_send_panel_sleep_locked( bSleep );')
    assert lock_at < store_at < send_at, helper
    print('PASS source: the helper caches, then sends, under g_leaseClientMutex')

    shutdown_lines = [line.strip() for line in helper_adds if 'shutdown(' in line]
    assert shutdown_lines == ['shutdown( g_nLeaseClientFd, SHUT_RDWR );'], shutdown_lines
    assert not any('lease_disconnect_client' in line for line in helper_adds), helper
    assert not any(line.strip().startswith('close(') for line in helper_adds), helper
    fail_at = helper.index('if ( !lease_send_control(')
    shutdown_at = helper.index('shutdown( g_nLeaseClientFd, SHUT_RDWR );')
    assert fail_at < shutdown_at, helper
    print('PASS source: a failed Sleep/Wake send shuts the socket down once and nothing else')

    setter_at = helper.index('void drm_lease_client_set_panel_sleep( bool bSleep )')
    setter = helper[setter_at:]
    assert 'g_bDrmLeasePanelSleep.exchange( bSleep ) != bSleep' in setter, setter
    assert 'force_repaint();' in setter, setter
    print('PASS source: the companion sleep mirror repaints only on a change')

    stores = [
        line.strip()
        for line in all_added(files)
        if 'g_bLeaseClientPanelSleep =' in line
        and not line.strip().startswith('static bool')
    ]
    assert stores == ['g_bLeaseClientPanelSleep = bSleep;'], stores
    print('PASS source: nothing resets the desired state, disconnect keeps it')

    assert any(
        '&cv == &cv_drm_sleep_screens[ gamescope::GAMESCOPE_SCREEN_TYPE_INTERNAL ]' in line
        for line in callback_adds
    ), callback_adds
    assert any('force_repaint();' in line for line in callback_hunk['lines']), callback_hunk
    print('PASS source: only the broker internal callback forwards, external unchanged')

    assert len(bsleep_adds) == 2, bsleep_adds
    bsleep = NL.join(bsleep_adds)
    assert 'drm_prepare' in bsleep_hunk['header'], bsleep_hunk['header']
    assert 'const bool bSleep = drm->pConnector &&' in bsleep, bsleep
    assert 'cv_drm_sleep_screens[ drm->pConnector->GetScreenType() ]' in bsleep, bsleep
    assert 'g_sDrmLeaseClientSocket && ( frameInfo->layers.count() == 0 || g_bDrmLeasePanelSleep.load() )' in bsleep, bsleep
    print('PASS source: drm_prepare ORs the companion mirror only under --drm-lease-client')

    for hunk in backend:
        for line in added(hunk):
            if line.lstrip().startswith('//'):
                continue
            if 'cv_drm_sleep_screens' not in line:
                continue
            if line.startswith('extern gamescope::ConVar<bool> cv_drm_sleep_screens[]'):
                continue
            if '&cv == &cv_drm_sleep_screens[' in line:
                continue
            assert 'drm_prepare' in hunk['header'], line
    print('PASS source: only drm_prepare reads the ConVar, never the socket thread')

    main_adds = all_added({MAIN_HPP: files[MAIN_HPP]})
    assert any('void drm_lease_client_set_panel_sleep( bool bSleep );' in line for line in main_adds), main_adds
    assert any(line.strip() == 'Sleep,' for line in main_adds), main_adds
    assert any(line.strip() == 'Wake,' for line in main_adds), main_adds
    print('PASS source: the companion setter is exported and Sleep/Wake are on the wire')

    wlserver_adds = all_added({WLSERVER: files[WLSERVER]})
    assert any(
        'drm_lease_client_set_panel_sleep( g_drmLeaseEvent.type == DrmLeaseEventType::Sleep );' in line
        for line in wlserver_adds
    ), wlserver_adds
    assert not any('drm_sleep_screen' in line for line in wlserver_adds), wlserver_adds
    print('PASS source: Sleep/Wake dispatch uses the companion mirror, not the ConVar')

    assert not any(
        'output_luts_applied' in line or 'needs_modeset' in line
        for line in all_added(files)
    ), '0043 must leave the existing bSleep LUT/modeset path alone'
    print('PASS source: the existing single bSleep still drives LUTs and modeset')

    return globals_adds, helper_adds, sync_adds, bsleep_adds


FIXTURE_HEAD = r'''#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <iostream>
#include <mutex>
#include <sys/socket.h>
#include <thread>
#include <vector>

namespace gamescope
{
    enum GamescopeScreenType
    {
        GAMESCOPE_SCREEN_TYPE_INTERNAL = 0,
        GAMESCOPE_SCREEN_TYPE_EXTERNAL = 1,
        GAMESCOPE_SCREEN_TYPE_COUNT,
    };

    template <typename T>
    struct ConVar
    {
        T m_Value{};

        // Same conversion as upstream src/convar.h, so `||` and other bool
        // contexts accept the ConVar the way the extracted patch code does.
        operator T() const { return m_Value; }
    };
}

gamescope::ConVar<bool> cv_drm_sleep_screens[gamescope::GAMESCOPE_SCREEN_TYPE_COUNT];

struct TestConnector
{
    gamescope::GamescopeScreenType eType;
    gamescope::GamescopeScreenType GetScreenType() const { return eType; }
};

struct TestLayers
{
    int nCount = 0;
    int count() const { return nCount; }
};

struct FrameInfo_t
{
    TestLayers layers;
};

struct drm_t
{
    TestConnector *pConnector;
};

static const char *g_sDrmLeaseClientSocket = nullptr;

// Stubs for the state the extracted patch code touches; the mirrors and the
// helpers themselves come from the patch.
static std::mutex g_leaseClientMutex;
static int g_nLeaseClientFd = -1;

enum class DrmLeaseEventType
{
    Down = 1,
    Motion,
    Up,
    Suspend,
    Resume,
    Sleep,
    Wake,
};

static int g_repaints = 0;
static void force_repaint()
{
    g_repaints++;
}

static int g_shutdownCalls = 0;
extern "C" int __wrap_shutdown( int, int )
{
    g_shutdownCalls++;
    return 0;
}

static std::mutex g_sentMutex;
static std::vector<DrmLeaseEventType> g_sent;
static int g_sendAttempts = 0;
static DrmLeaseEventType g_lastAttempt = DrmLeaseEventType::Down;
static bool g_sendShouldFail = false;

// Keeps a send open while g_leaseClientMutex is held so a second sender has
// to wait for it; that is how reconnect/wake ordering becomes observable.
static std::mutex g_hookMutex;
static std::condition_variable g_hookCv;
static bool g_hookArmed = false;
static bool g_hookEntered = false;
static bool g_hookRelease = false;

static bool lease_send_control( DrmLeaseEventType type )
{
    {
        std::unique_lock lock( g_hookMutex );
        if ( g_hookArmed )
        {
            g_hookEntered = true;
            g_hookCv.notify_all();
            g_hookCv.wait_for( lock, std::chrono::seconds( 2 ), [] { return g_hookRelease; } );
            g_hookArmed = false;
        }
    }
    {
        std::lock_guard lock( g_sentMutex );
        g_lastAttempt = type;
        g_sendAttempts++;
        if ( g_sendShouldFail )
            return false;
        g_sent.push_back( type );
    }
    return true;
}'''


FIXTURE_TAIL = r'''static void reset_state()
{
    {
        std::lock_guard lock( g_sentMutex );
        g_sent.clear();
        g_sendAttempts = 0;
        g_lastAttempt = DrmLeaseEventType::Down;
        g_sendShouldFail = false;
    }
    {
        std::lock_guard lock( g_hookMutex );
        g_hookArmed = false;
        g_hookEntered = false;
        g_hookRelease = false;
    }
    g_nLeaseClientFd = -1;
    g_bLeaseClientPanelSleep = false;
    g_bDrmLeasePanelSleep.store( false );
    g_shutdownCalls = 0;
    g_repaints = 0;
}

static std::vector<DrmLeaseEventType> sent_now()
{
    std::lock_guard lock( g_sentMutex );
    return g_sent;
}

static void check( bool ok, int line, const char *what )
{
    if ( !ok )
    {
        std::cerr << "CHECK failed at line " << line << ": " << what << std::endl;
        std::exit( 1 );
    }
}

#define CHECK( cond ) check( ( cond ), __LINE__, #cond )

static void run_no_client_state_change()
{
    std::cout << "RUN no-client state change is cached" << std::endl;
    reset_state();

    drm_lease_send_sleep( true );
    CHECK( g_bLeaseClientPanelSleep );
    CHECK( sent_now().empty() );

    drm_lease_send_sleep( false );
    CHECK( !g_bLeaseClientPanelSleep );
    CHECK( sent_now().empty() );

    std::cout << "PASS no-client state change is cached" << std::endl;
}

static void run_sleep_before_connect()
{
    std::cout << "RUN sleep before connect is replayed" << std::endl;
    reset_state();

    drm_lease_send_sleep( true );
    CHECK( g_bLeaseClientPanelSleep );

    g_nLeaseClientFd = 7;
    reconnect_sync();
    auto sent = sent_now();
    CHECK( sent.size() == 1 );
    CHECK( sent[0] == DrmLeaseEventType::Sleep );

    g_nLeaseClientFd = -1;
    CHECK( g_bLeaseClientPanelSleep );
    g_nLeaseClientFd = 9;
    reconnect_sync();
    sent = sent_now();
    CHECK( sent.size() == 2 );
    CHECK( sent[1] == DrmLeaseEventType::Sleep );

    std::cout << "PASS sleep before connect is replayed" << std::endl;
}

static void run_sync_after_reconnect()
{
    std::cout << "RUN reconnect while awake syncs Wake" << std::endl;
    reset_state();

    g_nLeaseClientFd = 7;
    reconnect_sync();
    const auto sent = sent_now();
    CHECK( sent.size() == 1 );
    CHECK( sent[0] == DrmLeaseEventType::Wake );

    std::cout << "PASS reconnect while awake syncs Wake" << std::endl;
}

static void run_wake_after_reconnect()
{
    std::cout << "RUN wake after a reconnect ends in Wake" << std::endl;
    reset_state();

    drm_lease_send_sleep( true );
    g_nLeaseClientFd = 7;
    reconnect_sync();
    drm_lease_send_sleep( false );

    const auto sent = sent_now();
    CHECK( sent.size() == 2 );
    CHECK( sent[0] == DrmLeaseEventType::Sleep );
    CHECK( sent[1] == DrmLeaseEventType::Wake );
    CHECK( !g_bLeaseClientPanelSleep );

    std::cout << "PASS wake after a reconnect ends in Wake" << std::endl;
}

static void run_reconnect_wake_contention()
{
    std::cout << "RUN reconnect and wake are ordered by g_leaseClientMutex" << std::endl;
    reset_state();

    drm_lease_send_sleep( true );
    g_nLeaseClientFd = 7;
    {
        std::lock_guard lock( g_hookMutex );
        g_hookArmed = true;
        g_hookEntered = false;
        g_hookRelease = false;
    }

    std::atomic<bool> wake_started{ false };
    std::atomic<bool> wake_done{ false };

    std::thread reconnect( [] { reconnect_sync(); } );
    {
        std::unique_lock lock( g_hookMutex );
        CHECK( g_hookCv.wait_for( lock, std::chrono::seconds( 2 ), [] { return g_hookEntered; } ) );
    }

    std::thread wake( [&] {
        wake_started.store( true, std::memory_order_release );
        drm_lease_send_sleep( false );
        wake_done.store( true, std::memory_order_release );
    } );

    for ( int i = 0; i < 2000 && !wake_started.load( std::memory_order_acquire ); i++ )
        std::this_thread::sleep_for( std::chrono::milliseconds( 1 ) );
    CHECK( wake_started.load( std::memory_order_acquire ) );

    // The reconnect's Sleep is on the wire while it holds the mutex; a wake
    // that shares that mutex cannot finish first, so the last control is Wake.
    std::this_thread::sleep_for( std::chrono::milliseconds( 100 ) );
    CHECK( !wake_done.load( std::memory_order_acquire ) );

    {
        std::lock_guard lock( g_hookMutex );
        g_hookRelease = true;
        g_hookCv.notify_all();
    }

    reconnect.join();
    wake.join();

    const auto sent = sent_now();
    CHECK( sent.size() == 2 );
    CHECK( sent[0] == DrmLeaseEventType::Sleep );
    CHECK( sent[1] == DrmLeaseEventType::Wake );
    CHECK( !g_bLeaseClientPanelSleep );

    std::cout << "PASS reconnect and wake are ordered by g_leaseClientMutex" << std::endl;
}

static void run_failed_sleep_shutdown()
{
    std::cout << "RUN a failed Sleep send shuts down once and retains the state" << std::endl;
    reset_state();

    g_nLeaseClientFd = 7;
    g_sendShouldFail = true;
    drm_lease_send_sleep( true );

    CHECK( g_bLeaseClientPanelSleep );
    CHECK( g_shutdownCalls == 1 );
    CHECK( g_sendAttempts == 1 );
    CHECK( g_lastAttempt == DrmLeaseEventType::Sleep );
    CHECK( sent_now().empty() );

    // The socket thread still owns the fd; once it recycles the client, a
    // reconnect replays the retained state.
    g_sendShouldFail = false;
    g_nLeaseClientFd = -1;
    g_nLeaseClientFd = 9;
    reconnect_sync();

    const auto sent = sent_now();
    CHECK( sent.size() == 1 );
    CHECK( sent[0] == DrmLeaseEventType::Sleep );
    CHECK( g_shutdownCalls == 1 );

    std::cout << "PASS a failed Sleep send shuts down once and retains the state" << std::endl;
}

static void run_failed_wake_shutdown()
{
    std::cout << "RUN a failed Wake send shuts down once and retains the state" << std::endl;
    reset_state();

    drm_lease_send_sleep( true );
    CHECK( g_bLeaseClientPanelSleep );
    CHECK( g_shutdownCalls == 0 );

    g_nLeaseClientFd = 7;
    g_sendShouldFail = true;
    drm_lease_send_sleep( false );

    CHECK( !g_bLeaseClientPanelSleep );
    CHECK( g_shutdownCalls == 1 );
    CHECK( g_sendAttempts == 1 );
    CHECK( g_lastAttempt == DrmLeaseEventType::Wake );
    CHECK( sent_now().empty() );

    g_sendShouldFail = false;
    g_nLeaseClientFd = -1;
    g_nLeaseClientFd = 9;
    reconnect_sync();

    const auto sent = sent_now();
    CHECK( sent.size() == 1 );
    CHECK( sent[0] == DrmLeaseEventType::Wake );
    CHECK( g_shutdownCalls == 1 );

    std::cout << "PASS a failed Wake send shuts down once and retains the state" << std::endl;
}

static void run_no_client_no_shutdown()
{
    std::cout << "RUN no client means no send and no shutdown" << std::endl;
    reset_state();

    drm_lease_send_sleep( true );
    CHECK( g_bLeaseClientPanelSleep );
    CHECK( g_shutdownCalls == 0 );
    CHECK( g_sendAttempts == 0 );

    std::cout << "PASS no client means no send and no shutdown" << std::endl;
}

static void run_panel_sleep_mirror()
{
    std::cout << "RUN the companion sleep mirror repaints only on change" << std::endl;
    reset_state();

    drm_lease_client_set_panel_sleep( true );
    CHECK( g_bDrmLeasePanelSleep.load() );
    CHECK( g_repaints == 1 );

    drm_lease_client_set_panel_sleep( true );
    CHECK( g_bDrmLeasePanelSleep.load() );
    CHECK( g_repaints == 1 );

    drm_lease_client_set_panel_sleep( false );
    CHECK( !g_bDrmLeasePanelSleep.load() );
    CHECK( g_repaints == 2 );

    std::cout << "PASS the companion sleep mirror repaints only on change" << std::endl;
}

static bool bSleep_case( gamescope::GamescopeScreenType type, bool bInternalSleep,
                         bool bExternalSleep, bool bCompanion, bool bMirror, int nLayers )
{
    TestConnector connector;
    connector.eType = type;
    FrameInfo_t frame;
    frame.layers.nCount = nLayers;
    drm_t drm;
    drm.pConnector = &connector;
    cv_drm_sleep_screens[gamescope::GAMESCOPE_SCREEN_TYPE_INTERNAL].m_Value = bInternalSleep;
    cv_drm_sleep_screens[gamescope::GAMESCOPE_SCREEN_TYPE_EXTERNAL].m_Value = bExternalSleep;
    g_sDrmLeaseClientSocket = bCompanion ? "socket" : nullptr;
    g_bDrmLeasePanelSleep.store( bMirror );
    return compute_bSleep( &drm, &frame );
}

static void run_bsleep_visible_layers()
{
    std::cout << "RUN a companion Sleep reaches internal and external connectors with a visible layer" << std::endl;

    CHECK( bSleep_case( gamescope::GAMESCOPE_SCREEN_TYPE_INTERNAL, false, false, true, true, 3 ) );
    CHECK( bSleep_case( gamescope::GAMESCOPE_SCREEN_TYPE_EXTERNAL, false, false, true, true, 3 ) );

    std::cout << "PASS companion Sleep reaches both connector types with a visible layer" << std::endl;
}

static void run_bsleep_wake_respects_independent()
{
    std::cout << "RUN a companion Wake respects an independently sleeping connector" << std::endl;

    CHECK( bSleep_case( gamescope::GAMESCOPE_SCREEN_TYPE_INTERNAL, true, false, true, false, 3 ) );
    CHECK( bSleep_case( gamescope::GAMESCOPE_SCREEN_TYPE_EXTERNAL, false, true, true, false, 3 ) );

    // The internal ConVar must not put an external connector to sleep.
    CHECK( !bSleep_case( gamescope::GAMESCOPE_SCREEN_TYPE_EXTERNAL, true, false, true, false, 3 ) );

    std::cout << "PASS a companion Wake respects independent per-connector sleep" << std::endl;
}

static void run_bsleep_no_client()
{
    std::cout << "RUN the companion mirror is ignored outside --drm-lease-client" << std::endl;

    CHECK( !bSleep_case( gamescope::GAMESCOPE_SCREEN_TYPE_INTERNAL, false, false, false, true, 3 ) );
    CHECK( !bSleep_case( gamescope::GAMESCOPE_SCREEN_TYPE_EXTERNAL, false, false, false, true, 3 ) );

    std::cout << "PASS the broker ignores the companion mirror" << std::endl;
}

static void run_bsleep_idle_companion()
{
    std::cout << "RUN an idle companion still sleeps without the mirror" << std::endl;

    CHECK( bSleep_case( gamescope::GAMESCOPE_SCREEN_TYPE_INTERNAL, false, false, true, false, 0 ) );
    CHECK( bSleep_case( gamescope::GAMESCOPE_SCREEN_TYPE_EXTERNAL, false, false, true, false, 0 ) );

    std::cout << "PASS an idle companion still sleeps" << std::endl;
}

static void run_bsleep_awake_visible()
{
    std::cout << "RUN a companion Wake with a visible layer leaves the connector on" << std::endl;

    CHECK( !bSleep_case( gamescope::GAMESCOPE_SCREEN_TYPE_INTERNAL, false, false, true, false, 3 ) );
    CHECK( !bSleep_case( gamescope::GAMESCOPE_SCREEN_TYPE_EXTERNAL, false, false, true, false, 3 ) );

    std::cout << "PASS a companion Wake with a visible layer leaves the connector on" << std::endl;
}

int main()
{
    run_no_client_state_change();
    run_sleep_before_connect();
    run_sync_after_reconnect();
    run_wake_after_reconnect();
    run_reconnect_wake_contention();
    run_failed_sleep_shutdown();
    run_failed_wake_shutdown();
    run_no_client_no_shutdown();
    run_panel_sleep_mirror();
    run_bsleep_visible_layers();
    run_bsleep_wake_respects_independent();
    run_bsleep_no_client();
    run_bsleep_idle_companion();
    run_bsleep_awake_visible();
    std::cout << "OK all runtime checks passed" << std::endl;
    return 0;
}'''


def build_fixture(globals_adds, helper_adds, sync_adds, bsleep_adds):
    reconnect_fn = NL.join([
        'static void reconnect_sync()',
        '{',
        NL.join(sync_adds),
        '}',
    ])
    bsleep_fn = NL.join([
        'static bool compute_bSleep( drm_t *drm, const FrameInfo_t *frameInfo )',
        '{',
        NL.join(bsleep_adds),
        '\treturn bSleep;',
        '}',
    ])
    blocks = [
        FIXTURE_HEAD,
        NL.join(globals_adds),
        '',
        NL.join(helper_adds),
        '',
        reconnect_fn,
        '',
        bsleep_fn,
        '',
        FIXTURE_TAIL,
    ]
    return NL.join(blocks) + NL


def main():
    text = PATCH.read_text(encoding='utf-8')
    globals_adds, helper_adds, sync_adds, bsleep_adds = check_source(text)
    fixture = build_fixture(globals_adds, helper_adds, sync_adds, bsleep_adds)

    if shutil.which('g++') is None:
        raise SystemExit('g++ not found; run this test in the gamescope container')

    with tempfile.TemporaryDirectory(prefix='gamescope-lowpower-') as work:
        source = pathlib.Path(work) / 'lease_sleep_fixture.cpp'
        binary = pathlib.Path(work) / 'lease_sleep_fixture'
        source.write_text(fixture, encoding='utf-8')
        build = subprocess.run(
            ['g++', '-std=c++20', '-O0', '-g', '-pthread', '-Wl,--wrap=shutdown',
             '-o', str(binary), str(source)],
            capture_output=True,
            text=True,
        )
        if build.returncode != 0:
            sys.stderr.write(build.stdout)
            sys.stderr.write(build.stderr)
            raise SystemExit('fixture failed to compile')
        run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60)
        sys.stdout.write(run.stdout)
        if run.returncode != 0:
            sys.stderr.write(run.stderr)
            raise SystemExit('fixture failed')

    print('OK gamescope-lowpower-test')


if __name__ == '__main__':
    main()
