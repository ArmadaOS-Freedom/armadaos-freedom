#!/usr/bin/env python3
'''Runtime tests for the lease panel-sleep mirror added by patch 0043.

The broker's lease socket thread must not read cv_drm_sleep_screens directly,
because ConVar<T>::m_Value is a plain T. Patch 0043 keeps a mutex-guarded mirror
for the broker and an atomic mirror for a companion. This test extracts the
added globals, helpers, reconnect block and the drm_prepare sleep expression
from the patch itself and runs them against small stubs, so the code under test
is the patched source and not a reimplementation. The sleep expression is
rebuilt from the hunk's new side because a canonical diff keeps its unchanged
leading line as context and only re-emits the changed continuation. It needs
g++ (the gamescope build container) and no DRM hardware.

The socket is stubbed: send outcomes are chosen with errno (EAGAIN for a benign
transient failure, EPIPE for a permanent one), so a retry is observed without
inducing a real transport stall. The socket thread's flush is called directly
by the test for each simulated poll; the production retry is the existing
bounded poll, not a caller-side wait. The one-shot hook disarms itself before
it waits, so only the send a test armed is ever held; a contention test starts
a newer command while that send is held and checks that it serializes behind
g_leaseClientMutex instead of overwriting the packet mid-flight.
'''

import pathlib
import re
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

HUNK_HEADER = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')


def parse_hunks(text):
    '''Group the patch into files and hunks, rejecting malformed lengths.'''
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
            match = HUNK_HEADER.match(line)
            if match is None:
                raise AssertionError('malformed hunk header %r' % line)
            if current is None:
                raise AssertionError('hunk before any diff header: %r' % line)
            hunk = {
                'header': line,
                'lines': [],
                'old_count': int(match.group(2) or 1),
                'new_count': int(match.group(4) or 1),
            }
            current.append(hunk)
        elif hunk is not None:
            hunk['lines'].append(line)

    for path, hunks in files.items():
        if not hunks:
            raise AssertionError('%s has no hunks' % path)
        for entry in hunks:
            check_hunk_lengths(path, entry)
    return files


def check_hunk_lengths(path, hunk):
    old = 0
    new = 0
    for line in hunk['lines']:
        if line.startswith('\\'):
            continue
        prefix = line[:1]
        if prefix == ' ':
            old += 1
            new += 1
        elif prefix == '-':
            old += 1
        elif prefix == '+':
            new += 1
        else:
            raise AssertionError('%s: hunk line without a diff prefix: %r' % (path, line))
    if (old, new) != (hunk['old_count'], hunk['new_count']):
        raise AssertionError(
            '%s: %r declares -%d +%d but carries -%d +%d'
            % (path, hunk['header'], hunk['old_count'], hunk['new_count'], old, new)
        )


def added(hunk):
    return [line[1:] for line in hunk['lines'] if line.startswith('+')]


def new_side(hunk):
    return [line[1:] for line in hunk['lines'] if line[:1] in (' ', '+')]


def new_statement(hunk, start_marker):
    '''Rebuild one whole statement from the hunk's new side.

    A canonical diff re-emits only the changed lines, so the unchanged first
    line of the drm_prepare sleep expression is context and added() alone
    cannot yield compilable code. Walk the new side from the declaration
    through the terminating semicolon instead, ignoring how the diff split
    context, removals and additions.
    '''
    lines = new_side(hunk)
    starts = [index for index, line in enumerate(lines) if start_marker in line]
    if len(starts) != 1:
        raise AssertionError(
            'expected exactly one new-side line with %r, found %d'
            % (start_marker, len(starts))
        )
    statement = []
    for line in lines[starts[0]:]:
        statement.append(line)
        if line.rstrip().endswith(';'):
            return statement
    raise AssertionError(
        'statement starting with %r has no terminating semicolon' % start_marker
    )


def all_added(files):
    return [
        line
        for hunks in files.values()
        for hunk in hunks
        for line in added(hunk)
    ]


def only_new(hunks, needle):
    found = [hunk for hunk in hunks if any(needle in line for line in new_side(hunk))]
    if len(found) != 1:
        raise AssertionError(
            'expected exactly one hunk whose new side has %r, found %d' % (needle, len(found))
        )
    return found[0]


def check_source(text):
    files = parse_hunks(text)
    for path in (BACKEND, MAIN_HPP, WLSERVER):
        if path not in files:
            raise AssertionError('patch does not touch %s' % path)
    backend = files[BACKEND]

    every_add = all_added(files)
    assert not any('std::thread' in line for line in every_add), every_add
    assert not any('sleep_for' in line for line in every_add), every_add
    assert not any('this_thread' in line for line in every_add), every_add
    assert not any('poll(' in line for line in every_add), every_add
    assert not any('kLeasePanelSleepRetry' in line for line in every_add), every_add
    print('PASS source: no caller poll, extra thread, sleep, or fixed retry count')

    globals_hunk = only_new(backend, 'static bool g_bLeaseClientPanelSleepPending = false;')
    panel_hunk = only_new(backend, 'static void drm_lease_send_sleep( bool bSleep )')
    poll_hunk = only_new(backend, 'pfds[1].events |= POLLOUT;')
    flush_hunk = only_new(backend, 'lease_flush_panel_sleep( nClientFd )')
    sync_hunk = only_new(backend, 'g_nActiveLeaseClients.fetch_add( 1 );')
    bsleep_hunk = only_new(backend, 'g_bDrmLeasePanelSleep.load()')
    callback_hunk = only_new(backend, 'drm_lease_send_sleep( cv );')

    globals_adds = added(globals_hunk)
    panel_adds = added(panel_hunk)
    sync_adds = added(sync_hunk)
    bsleep_statement = new_statement(
        bsleep_hunk, 'const bool bSleep = drm->pConnector &&'
    )
    callback_adds = added(callback_hunk)

    assert any('static bool g_bLeaseClientPanelSleep = false;' in line for line in globals_adds)
    assert any('static bool g_bLeaseClientPanelSleepPending = false;' in line for line in globals_adds)
    assert any('static std::atomic<bool> g_bDrmLeasePanelSleep{ false };' in line for line in globals_adds)
    assert any('extern gamescope::ConVar<bool> cv_drm_sleep_screens[];' in line for line in globals_adds)
    print('PASS source: pending flag, latest desired value and companion mirror start false')

    panel = NL.join(panel_adds)
    assert 'MSG_DONTWAIT | MSG_NOSIGNAL' in panel, panel
    assert 'LeasePanelSleepSend::Retry' in panel, panel
    assert 'LeasePanelSleepSend::Dead' in panel, panel
    assert 'shutdown( g_nLeaseClientFd, SHUT_RDWR );' in panel, panel
    assert 'lease_send_control' not in panel, panel
    assert 'WantsTouch' not in panel and 'Yields' not in panel, panel
    assert 'g_bLeaseClientPanelSleepPending = true;' in panel, panel
    assert 'g_bLeaseClientPanelSleepPending = false;' in panel, panel
    assert 'std::lock_guard lock( g_leaseClientMutex );' in panel, panel
    print('PASS source: panel packet has its own raw send and never touches touch/yield')

    assert not any(
        'lease_send_control' in line
        for hunk in backend
        for line in hunk['lines']
    ), '0043 must leave the Suspend/Resume helper untouched'
    print('PASS source: the legacy control helper is left untouched')

    poll = NL.join(added(poll_hunk))
    assert 'lease_panel_sleep_pending()' in poll, poll
    assert 'pfds[1].events |= POLLOUT;' in poll, poll
    flush = NL.join(added(flush_hunk))
    assert 'lease_flush_panel_sleep( nClientFd )' in flush, flush
    assert 'lease_disconnect_client( nClientFd )' in flush, flush
    print('PASS source: the socket thread polls POLLOUT while pending and may recycle a dead client')

    assert any('g_bLeaseClientPanelSleepPending = true;' in line for line in sync_adds), sync_adds
    assert any('std::lock_guard lock( g_leaseClientMutex );' in line for line in sync_adds), sync_adds
    assert not any('drm_lease_send_sleep(' in line for line in sync_adds), sync_adds
    print('PASS source: reconnect marks the cached state pending instead of sending inline')

    bsleep = NL.join(bsleep_statement)
    assert bsleep.rstrip().endswith(';'), bsleep
    assert bsleep.count('const bool bSleep') == 1, bsleep
    assert 'const bool bSleep = drm->pConnector &&' in bsleep, bsleep
    assert 'cv_drm_sleep_screens[ drm->pConnector->GetScreenType() ]' in bsleep, bsleep
    assert 'g_sDrmLeaseClientSocket && ( frameInfo->layers.count() == 0 || g_bDrmLeasePanelSleep.load() )' in bsleep, bsleep
    assert not any(
        'g_bDrmLeasePanelSleep' in line[1:]
        for line in bsleep_hunk['lines']
        if line.startswith('-')
    ), '0043 must only add the companion mirror to the sleep expression'
    print('PASS source: drm_prepare ORs the companion mirror only under --drm-lease-client')

    assert any(
        '&cv == &cv_drm_sleep_screens[ gamescope::GAMESCOPE_SCREEN_TYPE_INTERNAL ]' in line
        for line in callback_adds
    ), callback_adds
    assert any('force_repaint();' in line for line in callback_hunk['lines']), callback_hunk
    print('PASS source: only the broker internal callback forwards, external unchanged')

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
        for line in every_add
    ), '0043 must leave the existing bSleep LUT/modeset path alone'
    print('PASS source: the existing single bSleep still drives LUTs and modeset')

    return globals_adds, panel_adds, sync_adds, bsleep_statement


FIXTURE_HEAD = r'''#include <atomic>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <cstdint>
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
static uint64_t g_uLeaseClientGeneration = 0;

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

struct DrmLeaseEvent
{
    DrmLeaseEventType type;
    int32_t touchId;
    uint32_t time;
    float x;
    float y;
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
static int g_transientLeft = 0;
static bool g_permanent = false;

// One-shot hook: disarm before waiting, so a retry or a second sender can never
// be held by the same arm and only the send a test armed is observed.
static std::mutex g_sendHookMutex;
static std::condition_variable g_sendHookCv;
static bool g_sendHookArmed = false;
static bool g_sendHookEntered = false;
static bool g_sendHookRelease = false;

// A transient failure is a benign EAGAIN; a permanent one is EPIPE. Both are
// chosen by the test, so no real socket is stalled.
extern "C" ssize_t __wrap_send( int, const void *pData, size_t nLen, int )
{
    {
        std::unique_lock<std::mutex> lock( g_sendHookMutex );
        if ( g_sendHookArmed )
        {
            g_sendHookArmed = false;
            g_sendHookEntered = true;
            g_sendHookCv.notify_all();
            g_sendHookCv.wait_for( lock, std::chrono::seconds( 2 ), [] { return g_sendHookRelease; } );
        }
    }
    {
        std::lock_guard<std::mutex> lock( g_sentMutex );
        if ( nLen == sizeof( DrmLeaseEvent ) )
            g_lastAttempt = static_cast<const DrmLeaseEvent *>( pData )->type;
        g_sendAttempts++;
        if ( g_permanent )
        {
            errno = EPIPE;
            return -1;
        }
        if ( g_transientLeft > 0 )
        {
            g_transientLeft--;
            errno = EAGAIN;
            return -1;
        }
        if ( nLen == sizeof( DrmLeaseEvent ) )
            g_sent.push_back( static_cast<const DrmLeaseEvent *>( pData )->type );
    }
    return (ssize_t)nLen;
}'''


FIXTURE_TAIL = r'''static void reset_state()
{
    {
        std::lock_guard<std::mutex> lock( g_sentMutex );
        g_sent.clear();
        g_sendAttempts = 0;
        g_lastAttempt = DrmLeaseEventType::Down;
        g_transientLeft = 0;
        g_permanent = false;
    }
    {
        std::lock_guard<std::mutex> lock( g_sendHookMutex );
        g_sendHookArmed = false;
        g_sendHookEntered = false;
        g_sendHookRelease = false;
    }
    g_nLeaseClientFd = -1;
    g_uLeaseClientGeneration = 0;
    g_bLeaseClientPanelSleep = false;
    g_bLeaseClientPanelSleepPending = false;
    g_bDrmLeasePanelSleep.store( false );
    g_shutdownCalls = 0;
    g_repaints = 0;
}

static void connect_client( int nFd )
{
    std::lock_guard<std::mutex> lock( g_leaseClientMutex );
    g_nLeaseClientFd = nFd;
    g_uLeaseClientGeneration++;
}

static void disconnect_client()
{
    std::lock_guard<std::mutex> lock( g_leaseClientMutex );
    g_nLeaseClientFd = -1;
    g_uLeaseClientGeneration++;
}

// One simulated socket-thread poll: flush the pending state if a client is
// present. The production loop decides this from POLLOUT; here the mock send
// outcome makes the same flush deterministic.
static bool flush_once()
{
    const int nFd = g_nLeaseClientFd;
    if ( nFd < 0 )
        return false;
    if ( lease_flush_panel_sleep( nFd ) )
    {
        disconnect_client();
        return true;
    }
    return false;
}

static std::vector<DrmLeaseEventType> sent_now()
{
    std::lock_guard<std::mutex> lock( g_sentMutex );
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
    CHECK( g_bLeaseClientPanelSleepPending );
    CHECK( sent_now().empty() );
    CHECK( g_sendAttempts == 0 );
    CHECK( g_shutdownCalls == 0 );

    drm_lease_send_sleep( false );
    CHECK( !g_bLeaseClientPanelSleep );
    CHECK( g_bLeaseClientPanelSleepPending );
    CHECK( sent_now().empty() );

    std::cout << "PASS no-client state change is cached" << std::endl;
}

static void run_sleep_before_connect()
{
    std::cout << "RUN sleep before connect is replayed" << std::endl;
    reset_state();

    drm_lease_send_sleep( true );
    CHECK( g_bLeaseClientPanelSleepPending );

    connect_client( 7 );
    reconnect_sync();
    CHECK( g_bLeaseClientPanelSleepPending );
    flush_once();
    auto sent = sent_now();
    CHECK( sent.size() == 1 );
    CHECK( sent[0] == DrmLeaseEventType::Sleep );

    disconnect_client();
    CHECK( g_bLeaseClientPanelSleep );
    connect_client( 9 );
    reconnect_sync();
    flush_once();
    sent = sent_now();
    CHECK( sent.size() == 2 );
    CHECK( sent[1] == DrmLeaseEventType::Sleep );

    std::cout << "PASS sleep before connect is replayed" << std::endl;
}

static void run_sync_after_reconnect()
{
    std::cout << "RUN reconnect while awake syncs Wake" << std::endl;
    reset_state();

    connect_client( 7 );
    reconnect_sync();
    flush_once();
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
    connect_client( 7 );
    reconnect_sync();
    flush_once();
    drm_lease_send_sleep( false );

    const auto sent = sent_now();
    CHECK( sent.size() == 2 );
    CHECK( sent[0] == DrmLeaseEventType::Sleep );
    CHECK( sent[1] == DrmLeaseEventType::Wake );
    CHECK( !g_bLeaseClientPanelSleep );

    std::cout << "PASS wake after a reconnect ends in Wake" << std::endl;
}

static void run_transient_fails_twice_then_succeeds()
{
    std::cout << "RUN two would-blocks then success with no new command" << std::endl;
    reset_state();

    connect_client( 7 );
    g_transientLeft = 2;
    drm_lease_send_sleep( true );
    CHECK( g_bLeaseClientPanelSleepPending );
    CHECK( sent_now().empty() );

    flush_once();
    CHECK( g_bLeaseClientPanelSleepPending );
    CHECK( sent_now().empty() );

    flush_once();
    const auto sent = sent_now();
    CHECK( sent.size() == 1 );
    CHECK( sent[0] == DrmLeaseEventType::Sleep );
    CHECK( !g_bLeaseClientPanelSleepPending );
    CHECK( g_bLeaseClientPanelSleep );
    CHECK( g_sendAttempts == 3 );
    CHECK( g_shutdownCalls == 0 );

    std::cout << "PASS two would-blocks then success with no new command" << std::endl;
}

static void run_transient_coalesces_latest()
{
    std::cout << "RUN a pending sleep is replaced by a newer wake" << std::endl;
    reset_state();

    connect_client( 7 );
    g_transientLeft = 2;
    drm_lease_send_sleep( true );
    drm_lease_send_sleep( false );
    CHECK( g_bLeaseClientPanelSleepPending );
    CHECK( !g_bLeaseClientPanelSleep );
    CHECK( g_lastAttempt == DrmLeaseEventType::Wake );

    flush_once();
    const auto sent = sent_now();
    CHECK( sent.size() == 1 );
    CHECK( sent[0] == DrmLeaseEventType::Wake );
    CHECK( !g_bLeaseClientPanelSleepPending );
    CHECK( g_sendAttempts == 3 );

    std::cout << "PASS a pending sleep is replaced by a newer wake" << std::endl;
}

static void run_permanent_error_shuts_down()
{
    std::cout << "RUN a permanent send error shuts down once and retains the state" << std::endl;

    for ( int i = 0; i < 2; i++ )
    {
        reset_state();

        const bool bSleep = i == 0;
        const DrmLeaseEventType expected = bSleep ? DrmLeaseEventType::Sleep : DrmLeaseEventType::Wake;
        connect_client( 7 );
        g_permanent = true;
        drm_lease_send_sleep( bSleep );

        CHECK( g_bLeaseClientPanelSleep == bSleep );
        CHECK( !g_bLeaseClientPanelSleepPending );
        CHECK( g_shutdownCalls == 1 );
        CHECK( g_sendAttempts == 1 );
        CHECK( g_lastAttempt == expected );
        CHECK( sent_now().empty() );

        // The socket thread still owns the fd; once it recycles the client, a
        // reconnect replays the retained state.
        g_permanent = false;
        disconnect_client();
        connect_client( 9 );
        reconnect_sync();
        flush_once();

        const auto sent = sent_now();
        CHECK( sent.size() == 1 );
        CHECK( sent[0] == expected );
        CHECK( g_shutdownCalls == 1 );
    }

    std::cout << "PASS a permanent send error shuts down once and retains the state" << std::endl;
}

static void run_send_sleep_serializes_contention()
{
    std::cout << "RUN a newer command waits for the send in flight" << std::endl;
    reset_state();

    connect_client( 7 );
    {
        std::lock_guard<std::mutex> lock( g_sendHookMutex );
        g_sendHookArmed = true;
    }

    // The hook disarms before it waits, so only the send armed here is held.
    std::thread first( [] { drm_lease_send_sleep( true ); } );
    {
        std::unique_lock<std::mutex> lock( g_sendHookMutex );
        CHECK( g_sendHookCv.wait_for( lock, std::chrono::seconds( 2 ), [] { return g_sendHookEntered; } ) );
    }

    // drm_lease_send_sleep holds g_leaseClientMutex across the send, so this
    // newer Wake must serialize behind the in-flight Sleep rather than
    // replacing the packet that is already on its way.
    std::thread second( [] { drm_lease_send_sleep( false ); } );
    std::this_thread::sleep_for( std::chrono::milliseconds( 100 ) );
    CHECK( g_bLeaseClientPanelSleep );
    CHECK( g_bLeaseClientPanelSleepPending );
    CHECK( sent_now().empty() );

    {
        std::lock_guard<std::mutex> lock( g_sendHookMutex );
        g_sendHookRelease = true;
        g_sendHookCv.notify_all();
    }
    first.join();
    second.join();

    const auto sent = sent_now();
    CHECK( sent.size() == 2 );
    CHECK( sent[0] == DrmLeaseEventType::Sleep );
    CHECK( sent[1] == DrmLeaseEventType::Wake );
    CHECK( !g_bLeaseClientPanelSleep );
    CHECK( !g_bLeaseClientPanelSleepPending );
    CHECK( g_sendAttempts == 2 );
    CHECK( g_shutdownCalls == 0 );

    std::cout << "PASS a newer command serializes behind the in-flight send" << std::endl;
}

static void run_one_shot_hook_disarms()
{
    std::cout << "RUN the one-shot send hook disarms before it waits" << std::endl;
    reset_state();

    connect_client( 7 );
    {
        std::lock_guard<std::mutex> lock( g_sendHookMutex );
        g_sendHookArmed = true;
    }

    std::thread first( [] { drm_lease_send_sleep( true ); } );
    {
        std::unique_lock<std::mutex> lock( g_sendHookMutex );
        CHECK( g_sendHookCv.wait_for( lock, std::chrono::seconds( 2 ), [] { return g_sendHookEntered; } ) );
    }
    {
        std::lock_guard<std::mutex> lock( g_sendHookMutex );
        g_sendHookRelease = true;
        g_sendHookCv.notify_all();
    }
    first.join();

    CHECK( !g_sendHookArmed );
    CHECK( sent_now().size() == 1 );

    drm_lease_send_sleep( false );
    const auto sent = sent_now();
    CHECK( sent.size() == 2 );
    CHECK( sent[0] == DrmLeaseEventType::Sleep );
    CHECK( sent[1] == DrmLeaseEventType::Wake );

    std::cout << "PASS the one-shot send hook disarms before it waits" << std::endl;
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
    run_transient_fails_twice_then_succeeds();
    run_transient_coalesces_latest();
    run_permanent_error_shuts_down();
    run_send_sleep_serializes_contention();
    run_one_shot_hook_disarms();
    run_panel_sleep_mirror();
    run_bsleep_visible_layers();
    run_bsleep_wake_respects_independent();
    run_bsleep_no_client();
    run_bsleep_idle_companion();
    run_bsleep_awake_visible();
    std::cout << "OK all runtime checks passed" << std::endl;
    return 0;
}'''


def build_fixture(globals_adds, panel_adds, sync_adds, bsleep_statement):
    reconnect_fn = NL.join([
        'static void reconnect_sync()',
        '{',
        NL.join(sync_adds),
        '}',
    ])
    bsleep_fn = NL.join([
        'static bool compute_bSleep( drm_t *drm, const FrameInfo_t *frameInfo )',
        '{',
        NL.join(bsleep_statement),
        '\treturn bSleep;',
        '}',
    ])
    blocks = [
        FIXTURE_HEAD,
        NL.join(globals_adds),
        '',
        NL.join(panel_adds),
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
    globals_adds, panel_adds, sync_adds, bsleep_statement = check_source(text)
    fixture = build_fixture(globals_adds, panel_adds, sync_adds, bsleep_statement)

    if shutil.which('g++') is None:
        raise SystemExit('g++ not found; run this test in the gamescope container')

    with tempfile.TemporaryDirectory(prefix='gamescope-lowpower-') as work:
        source = pathlib.Path(work) / 'lease_sleep_fixture.cpp'
        binary = pathlib.Path(work) / 'lease_sleep_fixture'
        source.write_text(fixture, encoding='utf-8')
        build = subprocess.run(
            ['g++', '-std=c++20', '-O0', '-g', '-pthread',
             '-Wl,--wrap=send', '-Wl,--wrap=shutdown',
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
