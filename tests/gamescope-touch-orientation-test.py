#!/usr/bin/env python3
"""Behavior tests for Game Mode rotation of the virtual TB323FU panel touch.

The fixture compiles the production associate/unapply/callback bodies and the
production apply_touchscreen_orientation body from src/wlserver.cpp. Only
libinput/udev, the backend connectors and the seat sinks are stubbed, so the
coordinates asserted here are the ones the rest of wlserver would receive.

The desktop calibration under test is the narrow udev rule matrix
"0 1 0 -1 0 1" (x' = y, y' = 1 - x), applied by libinput before the event
reaches Gamescope.

Invocation:
    GAMESCOPE_SOURCE_DIR=/path/to/patched/gamescope \\
        python3 tests/gamescope-touch-orientation-test.py
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = Path(
    os.environ.get("GAMESCOPE_SOURCE_DIR") or ROOT / "packages/gamescope/source"
)
WLSOURCE = SOURCE_ROOT / "src/wlserver.cpp"
RULE = ROOT / "system_files/usr/lib/udev/rules.d/99-armada-tb323fu-virtual-touch.rules"

PRELUDE = r"""
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iomanip>
#include <iostream>
#include <string>

enum GamescopePanelOrientation
{
    GAMESCOPE_PANEL_ORIENTATION_AUTO = 0,
    GAMESCOPE_PANEL_ORIENTATION_0 = 1,
    GAMESCOPE_PANEL_ORIENTATION_90 = 2,
    GAMESCOPE_PANEL_ORIENTATION_180 = 3,
    GAMESCOPE_PANEL_ORIENTATION_270 = 4
};

namespace gamescope
{
enum GAMESCOPE_SCREEN_TYPE
{
    GAMESCOPE_SCREEN_TYPE_INTERNAL = 0,
    GAMESCOPE_SCREEN_TYPE_EXTERNAL = 1,
    GAMESCOPE_SCREEN_TYPE_AUTO = 2
};

class IBackendConnector
{
public:
    IBackendConnector( const char *name, GamescopePanelOrientation orientation )
        : m_name( name ), eOrientation( orientation ) {}
    const char *GetName() const { return m_name; }
    const char *GetMake() const { return "make"; }
    const char *GetModel() const { return "model"; }
    GamescopePanelOrientation GetCurrentOrientation() const { return eOrientation; }

    GamescopePanelOrientation eOrientation;

private:
    const char *m_name;
};
}

struct wl_list { wl_list *prev = nullptr; wl_list *next = nullptr; };
struct wl_listener
{
    wl_list link;
    void ( *notify )( struct wl_listener *, void * ) = nullptr;
};

#define wl_container_of( ptr, sample, member ) \
    ( __typeof__( sample ) )( (char *)( ptr ) - offsetof( __typeof__( *sample ), member ) )

struct wlr_input_device { int type = 0; };
struct wlr_touch { wlr_input_device base; };

enum class DrmLeaseEventType { Down, Motion, Up };
static void drm_lease_send_touch( DrmLeaseEventType, double, double, int, uint32_t ) {}

struct libinput_device {};

// Minimal udev tree: the event node, the bus/platform parent and a platform root.
struct udev_device
{
    const char *subsystem = nullptr;
    struct udev_device *parent = nullptr;
    const char *pszCalibration = nullptr;
};

static udev_device g_udevEvent;
static udev_device g_udevBus;
static udev_device g_udevRoot;
static libinput_device g_lidev;

static struct libinput_device *wlr_libinput_get_device_handle( struct wlr_input_device * )
{
    return &g_lidev;
}
static struct udev_device *libinput_device_get_udev_device( struct libinput_device * )
{
    return &g_udevEvent;
}
static const char *libinput_device_get_name( struct libinput_device * )
{
    return "NVTCapacitiveTouchScreen";
}
static const char *udev_device_get_subsystem( struct udev_device *dev )
{
    return dev != nullptr ? dev->subsystem : nullptr;
}
static struct udev_device *udev_device_get_parent( struct udev_device *dev )
{
    return dev != nullptr ? dev->parent : nullptr;
}
static const char *udev_device_get_property_value( struct udev_device *dev, const char *key )
{
    if ( dev == nullptr || strcmp( key, "GAMESCOPE_TOUCH_CALIBRATION" ) != 0 )
        return nullptr;
    return dev->pszCalibration;
}
static void udev_device_unref( struct udev_device * ) {}

struct { void infof( const char *, ... ) {} } wl_log;

struct wlserver_touch
{
    struct wlr_touch *wlr = nullptr;

    struct wl_listener down;
    struct wl_listener up;
    struct wl_listener motion;
    struct wl_listener destroy;

    gamescope::IBackendConnector *connector = nullptr;
    bool bIgnoreWhileLeased = false;
    bool bUndoRightCalibration = false;
};

struct wlr_touch_down_event { double x; double y; int touch_id; uint32_t time_msec; };
struct wlr_touch_up_event { int touch_id; uint32_t time_msec; };
struct wlr_touch_motion_event { double x; double y; int touch_id; uint32_t time_msec; };

static gamescope::IBackendConnector g_internal( "internal", GAMESCOPE_PANEL_ORIENTATION_0 );
static gamescope::IBackendConnector g_external( "external", GAMESCOPE_PANEL_ORIENTATION_0 );

class Backend
{
public:
    gamescope::IBackendConnector *GetConnector( gamescope::GAMESCOPE_SCREEN_TYPE type )
    {
        if ( type == gamescope::GAMESCOPE_SCREEN_TYPE_INTERNAL )
            return bHasInternal ? &g_internal : nullptr;
        if ( type == gamescope::GAMESCOPE_SCREEN_TYPE_EXTERNAL )
            return bHasExternal ? &g_external : nullptr;
        return nullptr;
    }

    bool bHasInternal = true;
    bool bHasExternal = true;
};

static Backend g_backend;
static Backend *GetBackend() { return &g_backend; }

static gamescope::GAMESCOPE_SCREEN_TYPE g_ForcedScreenType = gamescope::GAMESCOPE_SCREEN_TYPE_AUTO;
static bool panelTypeChanged = false;

static double g_last_x = 0.0;
static double g_last_y = 0.0;
static gamescope::IBackendConnector *g_last_connector = nullptr;

static const char *connector_label( gamescope::IBackendConnector *connector )
{
    if ( connector == &g_internal )
        return "internal";
    if ( connector == &g_external )
        return "external";
    return "none";
}

// Sinks standing in for the window-coordinate side of wlserver_touchdown/motion.
void wlserver_touchdown( double x, double y, int touch_id, uint32_t,
                         gamescope::IBackendConnector *connector )
{
    std::cout << "down " << x << " " << y << " " << touch_id << " "
              << connector_label( connector ) << "\n";
    g_last_x = x;
    g_last_y = y;
    g_last_connector = connector;
}

void wlserver_touchmotion( double x, double y, int touch_id, uint32_t, bool,
                           gamescope::IBackendConnector *connector )
{
    std::cout << "motion " << x << " " << y << " " << touch_id << " "
              << connector_label( connector ) << "\n";
    g_last_x = x;
    g_last_y = y;
    g_last_connector = connector;
}

void wlserver_touchup( int touch_id, uint32_t )
{
    std::cout << "up " << touch_id << "\n";
}
"""

MAIN = r"""
// What libinput applies from LIBINPUT_CALIBRATION_MATRIX="0 1 0 -1 0 1".
static void apply_base_right_calibration( double *x, double *y )
{
    double nx = *y;
    double ny = 1.0 - *x;
    *x = nx;
    *y = ny;
}

static void set_orientation( int degrees )
{
    switch ( degrees )
    {
        case 0: g_internal.eOrientation = GAMESCOPE_PANEL_ORIENTATION_0; break;
        case 90: g_internal.eOrientation = GAMESCOPE_PANEL_ORIENTATION_90; break;
        case 180: g_internal.eOrientation = GAMESCOPE_PANEL_ORIENTATION_180; break;
        case 270: g_internal.eOrientation = GAMESCOPE_PANEL_ORIENTATION_270; break;
        default: std::abort();
    }
}

static void scenario( const std::string &name )
{
    g_udevEvent = udev_device{};
    g_udevBus = udev_device{};
    g_udevRoot = udev_device{};

    g_udevEvent.subsystem = "input";
    g_udevEvent.parent = &g_udevRoot;
    g_udevRoot.subsystem = "platform";

    g_backend.bHasInternal = true;
    g_backend.bHasExternal = true;
    g_internal.eOrientation = GAMESCOPE_PANEL_ORIENTATION_0;

    auto bus = [&]( const char *subsystem ) {
        g_udevBus.subsystem = subsystem;
        g_udevBus.parent = &g_udevRoot;
        g_udevEvent.parent = &g_udevBus;
    };

    if ( name == "virtual" )
        ;
    else if ( name == "virtual-tagged" )
        g_udevEvent.pszCalibration = "right";
    else if ( name == "no-connector-tagged" )
    {
        g_udevEvent.pszCalibration = "right";
        g_backend.bHasInternal = false;
        g_backend.bHasExternal = false;
    }
    else if ( name == "spi" )
        bus( "spi" );
    else if ( name == "spi-tagged" )
    {
        bus( "spi" );
        g_udevEvent.pszCalibration = "right";
    }
    else if ( name == "i2c" )
        bus( "i2c" );
    else if ( name == "usb" )
        bus( "usb" );
    else
        std::abort();
}

int main( int argc, char **argv )
{
    std::cout << std::setprecision( 17 );

    wlserver_touch touch;
    wlr_touch device;
    touch.wlr = &device;

    for ( int i = 1; i < argc; i++ )
    {
        const std::string op = argv[i];
        if ( op == "scenario" )
            scenario( argv[++i] );
        else if ( op == "orientation" )
            set_orientation( std::atoi( argv[++i] ) );
        else if ( op == "down" || op == "motion" )
        {
            double x = std::atof( argv[++i] );
            double y = std::atof( argv[++i] );
            const int id = std::atoi( argv[++i] );
            apply_base_right_calibration( &x, &y );
            if ( op == "down" )
            {
                wlr_touch_down_event event{ x, y, id, 1u };
                wlserver_handle_touch_down( &touch.down, &event );
            }
            else
            {
                wlr_touch_motion_event event{ x, y, id, 1u };
                wlserver_handle_touch_motion( &touch.motion, &event );
            }
        }
        else if ( op == "up" )
        {
            wlr_touch_up_event event{ std::atoi( argv[++i] ), 1u };
            wlserver_handle_touch_up( &touch.up, &event );
        }
        else if ( op == "apply" )
        {
            // Mirrors the production "if (connector)" gate in the touch sinks,
            // which are stubbed above, then runs the production rotation body.
            double x = g_last_x;
            double y = g_last_y;
            if ( g_last_connector != nullptr )
                apply_touchscreen_orientation( g_last_connector->GetCurrentOrientation(), &x, &y );
            std::cout << "applied " << x << " " << y << "\n";
        }
        else if ( op == "state" )
        {
            std::cout << "state " << connector_label( touch.connector ) << " "
                      << ( touch.bUndoRightCalibration ? 1 : 0 ) << "\n";
        }
        else
            std::abort();
    }
    return 0;
}
"""

# libinput calibration matrix from the udev rule: x' = y, y' = 1 - x.
CALIBRATION_MATRIX = ((0.0, 1.0, 0.0), (-1.0, 0.0, 1.0))


def calibrate(x, y):
    return (
        CALIBRATION_MATRIX[0][0] * x + CALIBRATION_MATRIX[0][1] * y + CALIBRATION_MATRIX[0][2],
        CALIBRATION_MATRIX[1][0] * x + CALIBRATION_MATRIX[1][1] * y + CALIBRATION_MATRIX[1][2],
    )


def forward(orientation, x, y):
    """The production apply_touchscreen_orientation tables, x/y in output space."""
    if orientation == 0:
        return (x, y)
    if orientation == 90:
        return (1.0 - y, x)
    if orientation == 180:
        return (1.0 - x, 1.0 - y)
    if orientation == 270:
        return (y, 1.0 - x)
    raise AssertionError(orientation)


# Raw corners and centre through calibration, the undo and the rotation.
CORNERS = {
    (0.0, 0.0): {
        0: (0.0, 0.0),
        90: (1.0, 0.0),
        180: (1.0, 1.0),
        270: (0.0, 1.0),
    },
    (1.0, 0.0): {
        0: (1.0, 0.0),
        90: (1.0, 1.0),
        180: (0.0, 1.0),
        270: (0.0, 0.0),
    },
    (0.0, 1.0): {
        0: (0.0, 1.0),
        90: (0.0, 0.0),
        180: (1.0, 0.0),
        270: (1.0, 1.0),
    },
    (1.0, 1.0): {
        0: (1.0, 1.0),
        90: (0.0, 1.0),
        180: (0.0, 0.0),
        270: (1.0, 0.0),
    },
    (0.5, 0.5): {
        0: (0.5, 0.5),
        90: (0.5, 0.5),
        180: (0.5, 0.5),
        270: (0.5, 0.5),
    },
}


def extracted_region(source, start_marker, end_marker, description):
    start = source.find(start_marker)
    end = source.find(end_marker, start) if start >= 0 else -1
    if start < 0 or end < 0:
        raise AssertionError(f"could not locate the {description} bodies in wlserver.cpp")
    return source[start:end]


class TouchOrientationNativeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not WLSOURCE.is_file():
            raise AssertionError(
                f"missing {WLSOURCE}; set GAMESCOPE_SOURCE_DIR to a fully "
                "patched Gamescope tree"
            )
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        compiler = shutil.which("c++") or shutil.which("g++")
        if compiler is None:
            raise AssertionError("no C++ compiler available for the fixture")
        source = WLSOURCE.read_text(encoding="utf-8")
        dispatch = extracted_region(
            source,
            "static void wlserver_touch_associate_connector",
            "struct wlserver_keyboard {",
            "touch associate/callback",
        )
        orientation = extracted_region(
            source,
            "static void apply_touchscreen_orientation",
            "void wlserver_gesture_flush()",
            "touchscreen orientation",
        )
        fixture = Path(cls.directory.name) / "fixture.cpp"
        fixture.write_text(
            PRELUDE + "\n" + orientation + "\n" + dispatch + "\n" + MAIN,
            encoding="utf-8",
        )
        cls.executable = Path(cls.directory.name) / "fixture"
        result = subprocess.run(
            [compiler, "-std=c++17", "-O0", "-o", str(cls.executable), str(fixture)],
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            raise AssertionError("fixture compile failed:\n" + result.stderr)

    def run_fixture(self, *operations):
        result = subprocess.run(
            [str(self.executable), *operations],
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return [line for line in result.stdout.splitlines() if line]

    def coordinates(self, output, kind):
        for line in output:
            parts = line.split()
            if parts and parts[0] == kind:
                return float(parts[1]), float(parts[2])
        self.fail(f"no {kind} line in {output}")

    def connector(self, output, kind):
        for line in output:
            parts = line.split()
            if parts and parts[0] == kind:
                return parts[4]
        self.fail(f"no {kind} line in {output}")

    def state(self, output):
        for line in output:
            parts = line.split()
            if parts and parts[0] == "state":
                return parts[1], parts[2] == "1"
        self.fail(f"no state line in {output}")

    def assert_point(self, actual, expected, message=None):
        self.assertAlmostEqual(actual[0], expected[0], places=9, msg=message)
        self.assertAlmostEqual(actual[1], expected[1], places=9, msg=message)

    def test_tagged_virtual_associates_internal_and_undoes_once(self):
        output = self.run_fixture(
            "scenario", "virtual-tagged",
            "orientation", "0",
            "down", "0.25", "0.5", "1",
            "state",
        )
        self.assertEqual(self.state(output), ("internal", True), output)
        self.assertEqual(self.connector(output, "down"), "internal", output)
        # Calibrated (0.5, 0.75) undone exactly once; a second undo would differ.
        self.assert_point(self.coordinates(output, "down"), (0.25, 0.5), output)

    def test_untagged_virtual_is_left_alone(self):
        output = self.run_fixture(
            "scenario", "virtual",
            "orientation", "0",
            "down", "0.25", "0.5", "1",
            "state",
            "apply",
        )
        self.assertEqual(self.state(output), ("none", False), output)
        self.assertEqual(self.connector(output, "down"), "none", output)
        self.assert_point(self.coordinates(output, "down"), calibrate(0.25, 0.5), output)
        self.assert_point(self.coordinates(output, "applied"), calibrate(0.25, 0.5), output)

    def test_real_buses_keep_their_existing_association(self):
        cases = (
            ("spi", "internal"),
            ("i2c", "internal"),
            ("spi-tagged", "internal"),
            ("usb", "external"),
        )
        for scenario, expected_connector in cases:
            with self.subTest(scenario=scenario):
                output = self.run_fixture(
                    "scenario", scenario,
                    "orientation", "90",
                    "down", "0.25", "0.5", "1",
                    "state",
                    "apply",
                )
                # A tag on a real bus must not enable the undo either.
                self.assertEqual(self.state(output), (expected_connector, False), output)
                calibrated = calibrate(0.25, 0.5)
                self.assert_point(self.coordinates(output, "down"), calibrated, output)
                self.assert_point(
                    self.coordinates(output, "applied"), forward(90, *calibrated), output
                )

    def test_missing_connector_leaves_coordinates_alone(self):
        output = self.run_fixture(
            "scenario", "no-connector-tagged",
            "orientation", "90",
            "down", "0.25", "0.5", "1",
            "state",
            "apply",
        )
        self.assertEqual(self.state(output), ("none", False), output)
        calibrated = calibrate(0.25, 0.5)
        self.assert_point(self.coordinates(output, "down"), calibrated, output)
        self.assert_point(self.coordinates(output, "applied"), calibrated, output)

    def test_tagged_corners_and_centre_for_every_orientation(self):
        for orientation in (0, 90, 180, 270):
            for (raw_x, raw_y), expected in CORNERS.items():
                with self.subTest(orientation=orientation, raw=(raw_x, raw_y)):
                    output = self.run_fixture(
                        "scenario", "virtual-tagged",
                        "orientation", str(orientation),
                        "down", str(raw_x), str(raw_y), "1",
                        "apply",
                    )
                    self.assert_point(self.coordinates(output, "down"), (raw_x, raw_y), output)
                    self.assert_point(
                        self.coordinates(output, "applied"), expected[orientation], output
                    )

    def test_default_right_matches_the_calibrated_mapping(self):
        for raw in ((0.0, 0.0), (0.13, 0.87), (0.5, 0.5), (1.0, 1.0)):
            with self.subTest(raw=raw):
                output = self.run_fixture(
                    "scenario", "virtual-tagged",
                    "orientation", "270",
                    "down", str(raw[0]), str(raw[1]), "1",
                    "apply",
                )
                self.assert_point(
                    self.coordinates(output, "applied"), calibrate(*raw), output
                )

    def test_down_and_motion_both_use_the_correction(self):
        output = self.run_fixture(
            "scenario", "virtual-tagged",
            "orientation", "0",
            "down", "0.1", "0.2", "1",
            "motion", "0.3", "0.4", "1",
            "apply",
        )
        self.assert_point(self.coordinates(output, "down"), (0.1, 0.2), output)
        self.assert_point(self.coordinates(output, "motion"), (0.3, 0.4), output)
        self.assert_point(self.coordinates(output, "applied"), (0.3, 0.4), output)

    def test_centre_touch_stays_native(self):
        for orientation in (0, 90, 180, 270):
            with self.subTest(orientation=orientation):
                output = self.run_fixture(
                    "scenario", "virtual-tagged",
                    "orientation", str(orientation),
                    "down", "0.5", "0.5", "1",
                    "up", "1",
                    "apply",
                )
                self.assertEqual(self.connector(output, "down"), "internal", output)
                self.assert_point(self.coordinates(output, "down"), (0.5, 0.5), output)
                self.assert_point(self.coordinates(output, "applied"), (0.5, 0.5), output)
                self.assertIn("up 1", output, output)

    def test_rule_tags_only_the_virtual_touch(self):
        text = RULE.read_text(encoding="utf-8")
        tag = 'ENV{GAMESCOPE_TOUCH_CALIBRATION}="right"'
        self.assertEqual(text.count(tag), 1, text)
        line = next(entry for entry in text.splitlines() if tag in entry)
        # The tag rides on the rule that already pins the calibration, and the
        # narrow predicates that keep every other device out are untouched.
        for fragment in (
            'ENV{LIBINPUT_CALIBRATION_MATRIX}="0 1 0 -1 0 1"',
            'PROGRAM=="/usr/bin/grep -azFxq lenovo,baldur /sys/firmware/devicetree/base/compatible"',
        ):
            self.assertIn(fragment, line, text)
        for fragment in (
            'DEVPATH!="/devices/virtual/input/input*/event*"',
            'ATTRS{name}!="NVTCapacitiveTouchScreen"',
            'ATTRS{id/bustype}!="001c"',
        ):
            self.assertIn(fragment, text, text)


if __name__ == "__main__":
    unittest.main()
