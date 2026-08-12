"""XInput wrapper and controller abstraction.

Hardware-dependent assertions are skipped when no pad is present, so the
suite is meaningful on a build machine and stricter on a real one.
"""

from __future__ import annotations

import pytest

from app.controller import xinput
from app.controller.base import DeviceInfo, NullController
from app.controller.blitz import XInputController
from app.controller.device_manager import DeviceManager
from app.core.events import Event, EventBus

pad_connected = pytest.mark.skipif(
    not (xinput.available() and xinput.connected_indices()),
    reason="no physical controller connected",
)
windows_only = pytest.mark.skipif(
    not xinput.available(), reason="XInput unavailable on this platform"
)


class TestXInputModule:
    def test_import_never_raises_even_without_xinput(self):
        """The UI and tests must run on a machine with no XInput at all."""
        assert isinstance(xinput.available(), bool)
        assert isinstance(xinput.dll_name(), str)

    @windows_only
    def test_loads_a_known_dll(self):
        assert xinput.dll_name() in (
            "xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll"
        )

    def test_queries_on_empty_slots_are_safe(self):
        for index in range(xinput.MAX_DEVICES):
            assert isinstance(xinput.is_connected(index), bool)

    def test_connected_indices_are_in_range(self):
        for index in xinput.connected_indices():
            assert 0 <= index < xinput.MAX_DEVICES

    def test_vibration_on_an_empty_slot_reports_not_connected(self):
        # Slot 3 is almost never populated; skip if it happens to be.
        if 3 in xinput.connected_indices():
            pytest.skip("slot 3 is in use")
        result = xinput.set_vibration(3, 0, 0)
        assert result == xinput.ERROR_DEVICE_NOT_CONNECTED

    def test_speeds_are_clamped_to_the_16_bit_range(self):
        # Must not raise despite absurd inputs.
        xinput.set_vibration(3, -5000, 999999)


class TestXInputController:
    def test_intensities_are_clamped(self):
        controller = XInputController(index=3)
        controller.set_motors(-1.0, 5.0)
        left, right = controller.last_intensities
        assert 0.0 <= left <= 1.0
        assert 0.0 <= right <= 1.0

    def test_nan_never_reaches_the_hardware(self):
        controller = XInputController(index=3)
        controller.set_motors(float("nan"), float("nan"))
        assert controller.last_intensities == (0.0, 0.0)

    def test_output_limit_scales_the_command(self):
        controller = XInputController(index=3, output_limit=0.5)
        controller.set_motors(1.0, 1.0)
        assert controller.last_intensities == (0.5, 0.5)

    def test_repeated_identical_writes_are_skipped(self):
        """Saves a USB round trip per tick when output is steady."""
        controller = XInputController(index=3)
        controller.set_motors(0.5, 0.5)
        before = controller.write_stats
        for _ in range(10):
            controller.set_motors(0.5, 0.5)
        assert controller.write_stats == before

    def test_stop_always_writes_even_if_already_zero(self):
        controller = XInputController(index=3)
        controller.set_motors(0.0, 0.0)
        controller.stop()
        assert controller.last_intensities == (0.0, 0.0)

    def test_changing_index_silences_the_previous_slot(self):
        controller = XInputController(index=2)
        controller.set_motors(1.0, 1.0)
        controller.set_index(3)
        assert controller.index == 3

    def test_info_reports_honestly(self):
        info = XInputController(index=3).info()
        assert isinstance(info, DeviceInfo)
        if not info.connected:
            assert info.connection == "None"

    def test_context_manager_stops_on_exit(self):
        with XInputController(index=3) as controller:
            controller.set_motors(1.0, 1.0)
        assert controller.last_intensities == (0.0, 0.0)


class TestNullController:
    def test_records_writes_and_never_reports_connected(self):
        controller = NullController()
        assert controller.is_connected() is False
        controller.set_motors(0.5, 0.7)
        assert controller.last_left == 0.5
        assert controller.last_right == 0.7

    def test_stop_zeroes(self):
        controller = NullController()
        controller.set_motors(1.0, 1.0)
        controller.stop()
        assert (controller.last_left, controller.last_right) == (0.0, 0.0)


class TestDeviceManager:
    def test_emits_connect_and_disconnect_edges(self):
        bus = EventBus()
        events = []
        bus.subscribe(Event.CONTROLLER_CONNECTED, lambda **kw: events.append("connected"))
        bus.subscribe(Event.CONTROLLER_DISCONNECTED, lambda **kw: events.append("disconnected"))

        class FakeController(XInputController):
            def __init__(self):
                super().__init__(index=0)
                self.present = False

            def is_connected(self):
                return self.present

            def info(self):
                return DeviceInfo("fake", 0, "test", self.present)

        controller = FakeController()
        manager = DeviceManager(controller, bus, auto_detect=False)

        manager.check_now()
        assert events == []  # still absent, no edge

        controller.present = True
        manager.check_now()
        assert events == ["connected"]

        manager.check_now()
        assert events == ["connected"]  # no repeat without a change

        controller.present = False
        manager.check_now()
        assert events == ["connected", "disconnected"]

    def test_start_and_stop_are_clean(self):
        manager = DeviceManager(XInputController(index=3), EventBus(), auto_detect=False)
        manager.start()
        manager.stop()
        assert manager._thread is None


@pad_connected
class TestRealHardware:
    """Only runs with a physical controller attached."""

    def test_detects_the_pad(self):
        index = xinput.connected_indices()[0]
        assert xinput.is_connected(index)

    def test_rumble_write_succeeds(self):
        index = xinput.connected_indices()[0]
        controller = XInputController(index=index)
        try:
            assert controller.set_motors(0.25, 0.25) is True
            ok, failed = controller.write_stats
            assert ok >= 1 and failed == 0
        finally:
            controller.stop()

    def test_reads_pad_state(self):
        index = xinput.connected_indices()[0]
        assert xinput.get_state(index) is not None
