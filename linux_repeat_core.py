#!/usr/bin/env python3
"""Hold-to-repeat keyboard assistance for MapleStory Worlds on Linux."""
import ctypes as C
from dataclasses import dataclass, field
import fcntl
import os
from pathlib import Path
import random
import select
import signal
import sys
import time

from evdev import InputDevice, UInput, ecodes as E, list_devices
DEVICE_NAME = 'XWayKeyz (virtual) Maple A Assist'
RELAY_PREFIX = 'XWayKeyz (virtual) Maple A Relay: '
TOSHY_DEVICE_NAME = 'XWayKeyz (virtual) Keyboard'
MODIFIERS = {E.KEY_LEFTCTRL, E.KEY_RIGHTCTRL, E.KEY_LEFTALT, E.KEY_RIGHTALT,
             E.KEY_LEFTMETA, E.KEY_RIGHTMETA, E.KEY_LEFTSHIFT, E.KEY_RIGHTSHIFT}
REPEAT_KEYS = {E.KEY_A, E.KEY_ENTER, E.KEY_KPENTER}
HOLD_DELAY = {E.KEY_ENTER: .200, E.KEY_KPENTER: .200}


@dataclass
class RepeatState:
    held: set = field(default_factory=set)
    down: bool = False
    next_press: float = 0.0
    release_at: float = 0.0
    initial_press: bool = False


class GameFocus:
    """Read the XWayland active window; any unknown state fails closed."""
    def __init__(self):
        self.x = C.CDLL('libX11.so.6')
        x = self.x
        x.XOpenDisplay.argtypes = [C.c_char_p]
        x.XOpenDisplay.restype = C.c_void_p
        x.XDefaultRootWindow.argtypes = [C.c_void_p]
        x.XDefaultRootWindow.restype = C.c_ulong
        x.XInternAtom.argtypes = [C.c_void_p, C.c_char_p, C.c_int]
        x.XInternAtom.restype = C.c_ulong
        x.XGetWindowProperty.argtypes = [C.c_void_p, C.c_ulong, C.c_ulong,
            C.c_long, C.c_long, C.c_int, C.c_ulong, C.POINTER(C.c_ulong),
            C.POINTER(C.c_int), C.POINTER(C.c_ulong), C.POINTER(C.c_ulong),
            C.POINTER(C.POINTER(C.c_ubyte))]
        x.XFree.argtypes = [C.c_void_p]
        x.XCloseDisplay.argtypes = [C.c_void_p]
        self.error_handler = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_void_p)(lambda *_: 0)
        x.XSetErrorHandler.argtypes = [C.c_void_p]
        x.XSetErrorHandler(self.error_handler)
        self.display = x.XOpenDisplay(None)
        if not self.display:
            raise RuntimeError('게임 창을 확인할 수 없습니다. DISPLAY 설정을 확인해 주세요.')
        self.root = x.XDefaultRootWindow(self.display)
        self.active_atom = x.XInternAtom(self.display, b'_NET_ACTIVE_WINDOW', 0)
        self.class_atom = x.XInternAtom(self.display, b'WM_CLASS', 0)

    def property(self, window, atom):
        actual, fmt, count, after = C.c_ulong(), C.c_int(), C.c_ulong(), C.c_ulong()
        data = C.POINTER(C.c_ubyte)()
        status = self.x.XGetWindowProperty(self.display, window, atom, 0, 256,
            0, 0, C.byref(actual), C.byref(fmt), C.byref(count),
            C.byref(after), C.byref(data))
        try:
            if status or not data or not count.value:
                return None
            if fmt.value == 32:
                return C.cast(data, C.POINTER(C.c_ulong))[0]
            if fmt.value == 8:
                return C.string_at(data, count.value)
            return None
        finally:
            if data:
                self.x.XFree(data)

    def is_game(self):
        active = self.property(self.root, self.active_atom)
        if not active:
            return False
        classes = self.property(active, self.class_atom)
        return isinstance(classes, bytes) and b'msw.exe' in classes.lower().split(b'\0')

    def close(self):
        if self.display:
            self.x.XCloseDisplay(self.display)
            self.display = None


class Repeater:
    """Repeat each supported key independently while physically held."""
    def __init__(self, output, focus, clock=time.monotonic):
        self.output, self.focus, self.clock = output, focus, clock
        self.enabled = True
        self.states = {code: RepeatState() for code in REPEAT_KEYS}
        self.last_toggle = -10.0
        self.message = 'A나 Enter를 누르고 있는 동안만 연타해요'

    @property
    def running(self):
        return any(state.held for state in self.states.values())

    @property
    def down(self):
        return any(state.down for state in self.states.values())

    def key(self, code, value):
        self.output.write(E.EV_KEY, code, value)
        self.output.syn()
        self.states[code].down = bool(value)

    def stop(self, message=None, disable=False):
        if disable:
            self.enabled = False
        for code, state in self.states.items():
            state.held.clear()
            state.initial_press = False
            if state.down:
                self.key(code, 0)
        self.message = message or ('A나 Enter를 누르고 있는 동안만 연타해요' if self.enabled
                                   else '보조 기능이 꺼져 있어요')

    def toggle(self):
        self.enabled = not self.enabled
        self.stop()

    def press(self, source, blocked=False, code=E.KEY_A):
        if code not in self.states or not self.enabled or blocked or not self.focus():
            return False
        state = self.states[code]
        if source in state.held:
            return True
        first_source = not state.held
        state.held.add(source)
        if first_source:
            now = self.clock()
            state.next_press = now
            state.initial_press = code in HOLD_DELAY
            if state.initial_press:
                # Preserve the first normal Enter press. Only a sustained hold
                # transitions into repeated taps; short taps keep their key-up.
                state.release_at = now + HOLD_DELAY[code]
                self.key(code, 1)
        self.message = '손을 뗀 키는 바로 멈춰요'
        self.tick()
        return True

    def release(self, source, code=None):
        for key, state in self.states.items():
            if code is not None and key != code:
                continue
            state.held.discard(source)
            if not state.held:
                state.initial_press = False
                if state.down:
                    self.key(key, 0)
        if not self.running:
            self.stop()

    def shortcut(self, code, value):
        if value != 1:
            return
        if code == E.KEY_F8:
            now = self.clock()
            if now - self.last_toggle < .35:
                return
            self.last_toggle = now
            self.toggle()
        elif code == E.KEY_F9:
            self.stop(disable=True)
        elif code == E.KEY_ESC or code in MODIFIERS:
            if self.running:
                self.stop('연타할 키를 떼었다가 다시 눌러 주세요')

    def tick(self):
        if not self.running:
            return
        if not self.enabled or not self.focus():
            self.stop()
            return
        now = self.clock()
        for code, state in self.states.items():
            if not state.held:
                continue
            if state.initial_press:
                if now >= state.release_at:
                    self.key(code, 0)
                    state.initial_press = False
                    # Leave a visible release before the first repeated tap.
                    state.next_press = now + .030
                continue
            if state.down and now >= state.release_at:
                self.key(code, 0)
            if not state.down and now >= state.next_press:
                self.key(code, 1)
                state.release_at = now + .030
                state.next_press = now + random.randint(90, 200) / 1000


class CapturedKeyboard:
    def __init__(self, device, ready_delay):
        self.device = device
        # The relay name is excluded by Toshy's existing virtual-device filter.
        self.relay = UInput.from_device(device, name=RELAY_PREFIX + device.name,
                                       phys='maple-a-assist/' + Path(device.path).name)
        self.ready_at = time.monotonic() + ready_delay
        self.grabbed = False
        self.forwarded = set()
        self.suppressed = set()

    def activate(self):
        if self.grabbed or time.monotonic() < self.ready_at:
            return
        if self.device.active_keys():
            return
        self.device.grab()
        if self.device.active_keys():
            self.device.ungrab()
            return
        # Earlier events already reached the desktop before the grab.
        while True:
            try:
                list(self.device.read())
            except BlockingIOError:
                break
        self.grabbed = True

    def forward(self, event):
        if event.type == E.EV_KEY:
            if event.value == 1:
                self.forwarded.add(event.code)
            elif event.value == 0:
                self.forwarded.discard(event.code)
        if event.type not in (E.EV_SYN, E.EV_KEY, E.EV_REL):
            return
        self.relay.write_event(event)

    def close(self):
        try:
            for code in self.forwarded:
                self.relay.write(E.EV_KEY, code, 0)
            self.relay.syn()
        except OSError:
            pass
        finally:
            self.forwarded.clear()
            try:
                if self.grabbed:
                    self.device.ungrab()
            except OSError:
                pass
            finally:
                self.device.close()
                self.relay.close()


class Keyboards:
    def __init__(self, engine, paths=None, ready_delay=.8):
        self.engine = engine
        self.devices = {}
        self.paths = paths
        self.ready_delay = ready_delay
        self.refresh()

    @property
    def ready(self):
        return any(item.grabbed for item in self.devices.values())

    def remove(self, path):
        item = self.devices.pop(path)
        self.engine.release(path)
        item.close()

    def refresh(self):
        paths = set(list_devices() if self.paths is None else self.paths)
        for path in list(self.devices):
            if path not in paths:
                self.remove(path)
        # Toshy must own physical keyboards before this relay receives input.
        # Wait for its output even during login, when that device is not ready.
        # Otherwise startup order can bypass all Toshy shortcuts until logout.
        toshy_configured = (Path.home() / '.config/toshy/toshy_config.py').is_file()
        for path, item in list(self.devices.items()):
            if toshy_configured and item.device.name != TOSHY_DEVICE_NAME:
                self.remove(path)
        for path in paths - self.devices.keys():
            dev = None
            try:
                dev = InputDevice(path)
                if toshy_configured and dev.name != TOSHY_DEVICE_NAME:
                    dev.close()
                    continue
                caps = dev.capabilities().get(E.EV_KEY, [])
                if (dev.name == DEVICE_NAME or dev.name.startswith(RELAY_PREFIX)
                        or E.KEY_A not in caps or E.KEY_F8 not in caps):
                    dev.close()
                    continue
                if dev.active_keys():
                    dev.close()
                    continue
                # Already-grabbed physical devices belong to Toshy. Capture its
                # final virtual keyboard instead; do not change its settings.
                dev.grab()
                dev.ungrab()
                self.devices[path] = CapturedKeyboard(dev, self.ready_delay)
            except OSError:
                if dev is not None:
                    dev.close()

    def modifiers_held(self):
        return any(MODIFIERS.intersection(item.forwarded)
                   for item in self.devices.values())

    def handle(self, item, event):
        path = item.device.path
        if event.type == E.EV_SYN and event.code == E.SYN_DROPPED:
            self.engine.stop('키보드 연결을 다시 확인하고 있어요', disable=True)
            raise OSError('키보드 입력 동기화가 중단되었습니다')
        if event.type == E.EV_KEY:
            code, value = event.code, event.value
            if code in REPEAT_KEYS:
                if value == 0 and code in item.suppressed:
                    item.suppressed.discard(code)
                    self.engine.release(path, code=code)
                    return
                if code in item.suppressed:
                    return  # Ignore the keyboard's own auto-repeat events.
                if value == 1 and self.engine.press(path, self.modifiers_held(), code=code):
                    item.suppressed.add(code)
                    return
            self.engine.shortcut(code, value)
        item.forward(event)

    def process(self):
        for path, item in list(self.devices.items()):
            try:
                item.activate()
            except OSError:
                self.remove(path)
        readers = {item.device.fd: item for item in self.devices.values() if item.grabbed}
        if not readers:
            return
        ready, _, _ = select.select(list(readers), [], [], 0)
        for fd in ready:
            item = readers[fd]
            try:
                for event in item.device.read():
                    self.handle(item, event)
            except BlockingIOError:
                pass
            except OSError:
                self.remove(item.device.path)

    def close(self):
        for path in list(self.devices):
            self.remove(path)

