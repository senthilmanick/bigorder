#!/usr/bin/env python3
"""Matplotlib replay visualizer for The Big Order warehouse challenge.

Usage:
    python bigorder_visualizer.py BIG_ORDER.txt solution.txt

Controls:
    Space       Play / pause
    Right       Step one timestep
    + or =      Faster
    -           Slower
    Home        Restart from timestep 0
    G           Toggle grid
    L           Toggle robot trails
    Q / Esc     Quit

Notes:
- This is a visualization/replay tool, not the authoritative validator.
- It follows the same basic action semantics as the solver produced in this chat.
- y=0 is shown at the top, matching the warehouse coordinate system.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import threading
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
from matplotlib.patches import Circle, Rectangle

WIDTH = 60
HEIGHT = 40
FULFILL_Y = 0
REPLENISH_Y = 39


@dataclass
class Pallet:
    id: int
    x: int
    y: int
    sku: int
    count: int
    max_count: int
    owner: int | None = None


@dataclass
class Robot:
    id: int
    x: int
    y: int
    storage: Counter[int] = field(default_factory=Counter)
    docked: dict[int, tuple[int, int]] = field(default_factory=dict)
    pick_history: list[tuple[int, int, int, int]] = field(default_factory=list)  # (timestep, pallet_id, sku, remaining)


@dataclass(frozen=True)
class Action:
    timestep: int
    robot_id: int
    verb: str
    x: int
    y: int


@dataclass
class Worklist:
    robots: list[tuple[int, int]]
    capacities: list[int]
    pallets: list[tuple[int, int, int]]
    orders: list[list[int]]


def read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def parse_worklist(text: str) -> Worklist:
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(line)

    i = 0

    def take_int() -> int:
        nonlocal i
        if i >= len(lines):
            raise ValueError("unexpected EOF")
        parts = lines[i].split()
        i += 1
        if len(parts) != 1:
            raise ValueError(f"expected one integer at logical line {i}")
        return int(parts[0])

    nr = take_int()
    robots: list[tuple[int, int]] = []
    for _ in range(nr):
        x, y = map(int, lines[i].split())
        i += 1
        robots.append((x, y))

    ns = take_int()
    capacities = [take_int() for _ in range(ns)]

    npallets = take_int()
    pallets: list[tuple[int, int, int]] = []
    for _ in range(npallets):
        x, y, sku = map(int, lines[i].split())
        i += 1
        pallets.append((x, y, sku))

    norders = take_int()
    orders: list[list[int]] = []
    for _ in range(norders):
        orders.append(list(map(int, lines[i].split())))
        i += 1

    return Worklist(robots, capacities, pallets, orders)


def parse_actions(text: str) -> list[Action]:
    actions: list[Action] = []
    seen: set[tuple[int, int]] = set()
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 5:
            raise ValueError(f"solution line {lineno}: expected 5 fields")
        ts, rid, verb, x, y = parts
        a = Action(int(ts), int(rid), verb, int(x), int(y))
        key = (a.timestep, a.robot_id)
        if key in seen:
            raise ValueError(f"solution line {lineno}: duplicate {key}")
        seen.add(key)
        actions.append(a)
    actions.sort(key=lambda a: (a.timestep, a.robot_id))
    return actions


class ReplaySimulator:
    def __init__(self, w: Worklist):
        self.w = w
        self.robots = [Robot(i, x, y) for i, (x, y) in enumerate(w.robots)]
        self.pallets = [
            Pallet(i, x, y, sku, w.capacities[sku], w.capacities[sku])
            for i, (x, y, sku) in enumerate(w.pallets)
        ]
        self.unfulfilled = [True] * len(w.orders)
        self.order_counters = [Counter(o) for o in w.orders]
        self.fulfilled_count = 0
        self.replenishment_events = 0
        self.current_timestep = -1
        self.last_actions: list[Action] = []

    def reset(self) -> None:
        self.__init__(self.w)

    def pallet_at(self, x: int, y: int) -> Pallet | None:
        for p in self.pallets:
            if p.x == x and p.y == y:
                return p
        return None

    def apply_timestep(self, actions: Iterable[Action]) -> None:
        batch = list(actions)
        if not batch:
            return
        self.current_timestep = batch[0].timestep
        self.last_actions = batch

        # Generated solutions avoid ambiguous simultaneous interactions.  Apply
        # one action per robot, then do replenishment once at end of timestep.
        for a in batch:
            r = self.robots[a.robot_id]

            if a.verb == "move":
                dx, dy = a.x - r.x, a.y - r.y
                r.x, r.y = a.x, a.y
                for pid in list(r.docked):
                    p = self.pallets[pid]
                    p.x += dx
                    p.y += dy

            elif a.verb == "pick":
                p = self.pallet_at(a.x, a.y)
                if p is not None and p.count > 0:
                    p.count -= 1
                    r.storage[p.sku] += 1
                    r.pick_history.append((self.current_timestep, p.id, p.sku, p.count))

            elif a.verb == "dock":
                p = self.pallet_at(a.x, a.y)
                if p is not None and p.owner is None:
                    p.owner = r.id
                    r.docked[p.id] = (p.x - r.x, p.y - r.y)

            elif a.verb == "undock":
                p = self.pallet_at(a.x, a.y)
                if p is not None and p.owner == r.id:
                    p.owner = None
                    r.docked.pop(p.id, None)

            elif a.verb == "fulfill":
                # Match exactly as challenge rules require.
                for oid, needed in enumerate(self.order_counters):
                    if self.unfulfilled[oid] and needed == r.storage:
                        self.unfulfilled[oid] = False
                        self.fulfilled_count += 1
                        r.storage.clear()
                        break

        # End-of-timestep automatic replenishment.
        for r in self.robots:
            if r.y == REPLENISH_Y and r.docked:
                for pid in r.docked:
                    p = self.pallets[pid]
                    if p.count != p.max_count:
                        self.replenishment_events += 1
                    p.count = p.max_count


class WarehouseVisualizer:
    ROBOT_COLORS = ["tab:blue", "tab:orange", "tab:green", "tab:red", "tab:purple"]

    def __init__(
        self,
        w: Worklist,
        actions: list[Action] | None = None,
        interval_ms: int = 80,
        skip: int = 1,
        trail_length: int = 80,
        start_timestep: int = 0,
        live_pipe: str | None = None,
    ):
        self.w = w
        self.live_pipe = live_pipe
        self.live_mode = live_pipe is not None
        self.actions = actions or []
        self.by_t: dict[int, list[Action]] = defaultdict(list)
        for a in self.actions:
            self.by_t[a.timestep].append(a)

        self.max_timestep = max(self.by_t, default=0)
        self.sim = ReplaySimulator(w)
        self.interval_ms = max(1, interval_ms)
        self.skip = max(1, skip)
        self.playing = True
        self.show_grid = True
        self.show_trails = True
        self.trail_length = max(1, trail_length)
        self.trails = [deque(maxlen=self.trail_length) for _ in w.robots]
        self.next_timestep = 0

        self.live_queue: queue.Queue[dict] = queue.Queue()
        self.live_connected = False
        self.live_done = False
        self.live_error: str | None = None
        self.live_pending: dict[int, list[Action]] = defaultdict(list)
        self.master_targets: dict[int, dict] = {}
        self.master_waiting: dict[int, dict] = {}
        # Persistent right-of-way holds streamed by the master. lower_rid -> event.
        self.priority_holds: dict[int, dict] = {}
        self.master_log: deque[str] = deque(maxlen=8)
        self.live_roles: dict[str, list[int]] = {}

        self.fig = plt.figure(figsize=(17, 9))
        gs = self.fig.add_gridspec(1, 2, width_ratios=[4.7, 1.3], wspace=0.04)
        self.ax = self.fig.add_subplot(gs[0, 0])
        self.side_ax = self.fig.add_subplot(gs[0, 1])
        try:
            self.fig.canvas.manager.set_window_title("The Big Order - Live Solver" if self.live_mode else "The Big Order - Warehouse Replay")
        except Exception:
            pass

        self.expanded_robot: int | None = None
        self.sidebar_artists = []
        self._configure_axes()
        self._configure_sidebar()

        # One rectangle per pallet is fast enough for 240 pallets and avoids
        # recreating artists each animation frame.
        self.pallet_patches: list[Rectangle] = []
        self.pallet_texts = []
        for p in self.sim.pallets:
            rect = Rectangle((p.x - 0.42, p.y - 0.42), 0.84, 0.84,
                             facecolor="tan", edgecolor="saddlebrown", linewidth=0.7, zorder=2)
            self.ax.add_patch(rect)
            self.pallet_patches.append(rect)
            txt = self.ax.text(p.x, p.y, str(p.sku), ha="center", va="center",
                               fontsize=5.5, color="black", zorder=3)
            self.pallet_texts.append(txt)

        self.robot_patches: list[Circle] = []
        self.robot_texts = []
        self.robot_sku_texts = []
        self.trail_lines = []
        for rid, r in enumerate(self.sim.robots):
            color = self.ROBOT_COLORS[rid % len(self.ROBOT_COLORS)]
            circle = Circle((r.x, r.y), 0.38, facecolor=color, edgecolor="black",
                            linewidth=1.0, zorder=6)
            self.ax.add_patch(circle)
            self.robot_patches.append(circle)
            self.robot_texts.append(
                self.ax.text(r.x, r.y, f"R{rid}", ha="center", va="center",
                             fontsize=7, color="white", fontweight="bold", zorder=7)
            )
            self.robot_sku_texts.append(
                self.ax.text(r.x, r.y, "", visible=False)
            )
            line, = self.ax.plot([], [], ":", linewidth=1.2, color=color, alpha=0.7, zorder=1)
            self.trail_lines.append(line)
            self.trails[rid].append((r.x, r.y))

        self.status_text = self.fig.text(
            0.01, 0.015, "", ha="left", va="bottom", fontsize=10, family="monospace"
        )
        self.help_text = self.fig.text(
            0.99, 0.015,
            "SPACE play/pause   → step   +/- speed   HOME restart   G grid   L trails   Q quit",
            ha="right", va="bottom", fontsize=8,
        )

        self.fig.canvas.mpl_connect("key_press_event", self._on_key)
        self.fig.canvas.mpl_connect("button_press_event", self._on_click)

        if start_timestep > 0 and not self.live_mode:
            self._replay_to(min(start_timestep, self.max_timestep))

        if self.live_mode:
            self.playing = True
            self._start_live_reader()

        self._draw_state()
        self.anim = FuncAnimation(
            self.fig,
            self._animate,
            interval=self.interval_ms,
            blit=False,
            cache_frame_data=False,
        )

    def _configure_axes(self) -> None:
        self.ax.set_title("The Big Order — Live Solver" if self.live_mode else "The Big Order — Warehouse Replay", fontsize=14)
        self.ax.set_xlim(-0.5, WIDTH - 0.5)
        self.ax.set_ylim(HEIGHT - 0.5, -0.5)  # y=0 at top
        self.ax.set_aspect("equal", adjustable="box")
        self.ax.set_xticks(range(0, WIDTH, 5))
        self.ax.set_yticks(range(0, HEIGHT, 5))
        self.ax.set_xticks([x - 0.5 for x in range(WIDTH + 1)], minor=True)
        self.ax.set_yticks([y - 0.5 for y in range(HEIGHT + 1)], minor=True)
        self.ax.grid(which="minor", linewidth=0.25, alpha=0.35)
        self.ax.grid(which="major", linewidth=0.5, alpha=0.4)
        self.ax.tick_params(which="minor", length=0)
        self.ax.set_xlabel("x")
        self.ax.set_ylabel("y")

        # Fulfillment and replenishment rows.
        self.ax.axhspan(-0.5, 0.5, alpha=0.18, color="tab:green", zorder=0)
        self.ax.axhspan(REPLENISH_Y - 0.5, REPLENISH_Y + 0.5,
                        alpha=0.18, color="tab:blue", zorder=0)
        self.ax.text(WIDTH - 0.8, 0, "FULFILL", ha="right", va="center",
                     fontsize=8, fontweight="bold", zorder=1)
        self.ax.text(WIDTH - 0.8, REPLENISH_Y, "REPLENISH", ha="right", va="center",
                     fontsize=8, fontweight="bold", zorder=1)

    def _configure_sidebar(self) -> None:
        self.side_ax.set_xlim(0, 1)
        self.side_ax.set_ylim(0, 1)
        self.side_ax.axis("off")
        self.side_ax.set_title("Robot / Pallet Activity", fontsize=12, fontweight="bold", pad=10)

    def _draw_sidebar(self) -> None:
        # Redraw only this small axes; keeps the warehouse view stable.
        self.side_ax.clear()
        self._configure_sidebar()
        self.sidebar_hitboxes = []

        y = 0.965
        if self.live_mode:
            state = "connected" if self.live_connected else "waiting for solver"
            if self.live_done:
                state = "solver finished"
            if self.live_error:
                state = "solver error"
            self.side_ax.text(0.02, y, f"LIVE: {state}", fontsize=9, fontweight="bold", va="top")
            y -= 0.035
        self.side_ax.text(0.02, y, "Master target + last pick", fontsize=9, color="dimgray", va="top")
        y -= 0.055

        for rid, r in enumerate(self.sim.robots):
            color = self.ROBOT_COLORS[rid % len(self.ROBOT_COLORS)]
            history = list(reversed(r.pick_history))
            latest = history[0] if history else None
            current_pallet = self.sim.pallets[latest[1]] if latest else None

            # Header is clickable to expand/collapse details.
            header_y = y
            arrow = "▼" if self.expanded_robot == rid else "▶"
            self.side_ax.text(0.02, y, f"{arrow} R{rid}  P{rid}", fontsize=10.5, fontweight="bold",
                              color=color, va="top")
            if latest:
                t, pid, sku, remaining = latest
                self.side_ax.text(0.24, y, f"SKU {sku}  P#{pid}", fontsize=10.5,
                                  fontweight="bold", va="top")
                self.side_ax.text(0.24, y-0.026, f"last pick t={t}   pallet left={remaining}",
                                  fontsize=8.3, color="dimgray", va="top")
            else:
                self.side_ax.text(0.24, y, "no picks yet", fontsize=9.5, color="dimgray", va="top")

            mt = self.master_targets.get(rid)
            mw = self.master_waiting.get(rid)
            ph = self.priority_holds.get(rid)
            if ph is not None:
                higher = ph.get("for_robot")
                target_text = f"MASTER → HOLD for R{higher} until mission complete"
                self.side_ax.text(0.24, y-0.050, target_text, fontsize=8.1, fontweight="bold", va="top")
                y_extra = 0.022
            elif mw and int(mw.get("t", -1)) == self.sim.current_timestep:
                blocker = mw.get("blocker")
                who = f" for R{blocker}" if blocker is not None else ""
                target_text = f"MASTER → WAIT{who}: {mw.get('reason')}"
                self.side_ax.text(0.24, y-0.050, target_text, fontsize=8.1, fontweight="bold", va="top")
                y_extra = 0.022
            elif mt:
                if mt.get("kind") == "logistics":
                    target_text = f"MASTER → replenish SKU {mt.get('sku')} P#{mt.get('pallet')}"
                else:
                    target_text = f"MASTER → SKU {mt.get('sku')} P#{mt.get('pallet')}"
                self.side_ax.text(0.24, y-0.050, target_text, fontsize=8.1, fontweight="bold", va="top")
                y_extra = 0.022
            else:
                self.side_ax.text(0.24, y-0.050, "MASTER → wait", fontsize=8.1, color="dimgray", va="top")
                y_extra = 0.022

            self.sidebar_hitboxes.append((rid, header_y-0.070, header_y+0.015))
            y -= 0.07 + y_extra

            if self.expanded_robot == rid:
                # Docked pallets first.
                if r.docked:
                    docked_desc = ", ".join(
                        f"SKU {self.sim.pallets[pid].sku} (P#{pid})" for pid in sorted(r.docked)
                    )
                else:
                    docked_desc = "none"
                self.side_ax.text(0.08, y, f"Docked: {docked_desc}", fontsize=8.7, va="top")
                y -= 0.035

                inv = ", ".join(f"SKU {sku} × {cnt}" for sku, cnt in sorted(r.storage.items()) if cnt) or "empty"
                self.side_ax.text(0.08, y, f"Picked storage: {inv}", fontsize=8.7, va="top", wrap=True)
                y -= 0.045

                self.side_ax.text(0.08, y, "Recent picks:", fontsize=8.7, fontweight="bold", va="top")
                y -= 0.03
                if history:
                    for t, pid, sku, remaining in history[:8]:
                        self.side_ax.text(0.11, y, f"t={t:<6} SKU {sku:<3}  P#{pid:<3}  left={remaining}",
                                          fontsize=8.1, family="monospace", va="top")
                        y -= 0.027
                else:
                    self.side_ax.text(0.11, y, "—", fontsize=8.3, va="top")
                    y -= 0.027
                y -= 0.02

            # subtle separator
            self.side_ax.plot([0.02, 0.98], [y+0.005, y+0.005], color="0.85", linewidth=0.8)
            y -= 0.025

            if y < 0.04:
                self.side_ax.text(0.02, 0.02, "Expand fewer robots to see more history",
                                  fontsize=8, color="dimgray", va="bottom")
                break

    def _on_click(self, event) -> None:
        if event.inaxes is not self.side_ax or event.ydata is None:
            return
        for rid, y0, y1 in getattr(self, "sidebar_hitboxes", []):
            if y0 <= event.ydata <= y1:
                self.expanded_robot = None if self.expanded_robot == rid else rid
                self._draw_sidebar()
                self.fig.canvas.draw_idle()
                return

    def _start_live_reader(self) -> None:
        assert self.live_pipe is not None
        path = self.live_pipe
        pp = Path(path)
        if pp.exists() and not pp.is_fifo():
            raise ValueError(f"live pipe path exists and is not a FIFO: {path}")
        if not pp.exists():
            os.mkfifo(path)
        print(f"LIVE VISUALIZER: waiting for solver on {path}", flush=True)

        def reader() -> None:
            try:
                with open(path, "r", encoding="utf-8") as fp:
                    self.live_queue.put({"type": "connected"})
                    for raw in fp:
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            self.live_queue.put(json.loads(raw))
                        except json.JSONDecodeError as e:
                            self.live_queue.put({"type": "stream_error", "message": f"bad JSON: {e}"})
                self.live_queue.put({"type": "eof"})
            except Exception as e:
                self.live_queue.put({"type": "stream_error", "message": str(e)})

        threading.Thread(target=reader, name="bigorder-live-pipe", daemon=True).start()

    def _handle_live_event(self, ev: dict) -> bool:
        typ = ev.get("type")
        if typ == "connected":
            self.live_connected = True
            return False
        if typ == "action":
            a = Action(int(ev["t"]), int(ev["robot"]), str(ev["verb"]), int(ev["x"]), int(ev["y"]))
            self.live_pending[a.timestep].append(a)
            self.actions.append(a)
            self.max_timestep = max(self.max_timestep, a.timestep)
            return False
        if typ == "tick":
            t = int(ev.get("t", self.sim.current_timestep + 1))
            batch = self.live_pending.pop(t, [])
            if batch:
                batch.sort(key=lambda a: a.robot_id)
                self.sim.apply_timestep(batch)
            else:
                self.sim.current_timestep = t
                self.sim.last_actions = []
            for rid, r in enumerate(self.sim.robots):
                self.trails[rid].append((r.x, r.y))
            self.max_timestep = max(self.max_timestep, t)
            self.next_timestep = t + 1
            return True
        if typ == "master_assign":
            rid = int(ev["robot"])
            self.master_targets[rid] = {"kind": "pick", "sku": ev.get("sku"), "pallet": ev.get("pallet"), "t": ev.get("t")}
            self.master_log.appendleft(f"t={ev.get('t')} R{rid} → P{ev.get('pallet')}/SKU{ev.get('sku')}")
            return False
        if typ == "master_stop":
            rid = int(ev["robot"])
            self.master_targets.pop(rid, None)
            self.master_log.appendleft(f"t={ev.get('t')} STOP R{rid} P{ev.get('pallet')} ({ev.get('reason')})")
            return False
        if typ == "priority_hold":
            rid = int(ev["robot"])
            self.priority_holds[rid] = dict(ev)
            self.master_log.appendleft(
                f"t={ev.get('t')} HOLD R{rid} for R{ev.get('for_robot')} until mission complete"
            )
            return False
        if typ == "priority_release":
            rid = int(ev["robot"])
            higher = ev.get("for_robot")
            self.priority_holds.pop(rid, None)
            self.master_waiting.pop(rid, None)
            self.master_log.appendleft(
                f"t={ev.get('t')} RELEASE R{rid}; R{higher} mission complete"
            )
            return False
        if typ == "master_clear":
            rid = int(ev["robot"])
            self.master_waiting[rid] = {**dict(ev), "reason": "MOVE ASIDE", "blocker": ev.get("for_robot")}
            self.master_log.appendleft(
                f"t={ev.get('t')} CLEAR R{rid} for R{ev.get('for_robot')} cell=({ev.get('x')},{ev.get('y')})"
            )
            return False
        if typ == "master_clear_move":
            rid = int(ev["robot"])
            self.master_waiting[rid] = {**dict(ev), "reason": "MOVE ASIDE", "blocker": ev.get("for_robot")}
            self.master_log.appendleft(
                f"t={ev.get('t')} R{rid} moves aside for R{ev.get('for_robot')} → ({ev.get('x')},{ev.get('y')})"
            )
            return False
        if typ == "master_wait":
            rid = int(ev["robot"])
            self.master_waiting[rid] = dict(ev)
            blocker = ev.get("blocker")
            who = f" for R{blocker}" if blocker is not None else ""
            self.master_log.appendleft(f"t={ev.get('t')} WAIT R{rid}{who} ({ev.get('reason')})")
            return False
        if typ == "logistics_assign":
            rid = int(ev["robot"])
            self.master_targets[rid] = {"kind": "logistics", "sku": ev.get("sku"), "pallet": ev.get("pallet"), "t": ev.get("t")}
            self.master_log.appendleft(f"t={ev.get('t')} R{rid} replenish SKU{ev.get('sku')}")
            return False
        if typ == "logistics_done":
            rid = int(ev["robot"])
            self.master_targets.pop(rid, None)
            self.master_log.appendleft(f"t={ev.get('t')} R{rid} replenish done SKU{ev.get('sku')}")
            return False
        if typ == "replenish_request":
            self.master_log.appendleft(f"t={ev.get('t')} request replenish SKU{ev.get('sku')}")
            return False
        if typ == "start":
            self.live_roles = {"fulfillment": list(ev.get("fulfillment", [])), "logistics": list(ev.get("logistics", []))}
            self.master_log.appendleft(f"solver started: F={self.live_roles['fulfillment']} L={self.live_roles['logistics']}")
            return False
        if typ == "done":
            self.live_done = True
            self.master_log.appendleft(f"DONE fulfilled={ev.get('fulfilled')}/{ev.get('total')}")
            return False
        if typ in ("error", "stream_error"):
            self.live_error = str(ev.get("message", "unknown error"))
            self.live_done = True
            self.master_log.appendleft("ERROR: " + self.live_error[:60])
            return False
        if typ == "eof":
            self.live_done = True
            return False
        return False

    def _drain_live(self) -> None:
        ticks = 0
        seen = 0
        while ticks < self.skip and seen < 20000:
            try:
                ev = self.live_queue.get_nowait()
            except queue.Empty:
                break
            seen += 1
            if self._handle_live_event(ev):
                ticks += 1

    def _replay_to(self, target: int) -> None:
        self.sim.reset()
        self.trails = [deque(maxlen=self.trail_length) for _ in self.w.robots]
        for rid, r in enumerate(self.sim.robots):
            self.trails[rid].append((r.x, r.y))
        for t in range(0, target + 1):
            batch = self.by_t.get(t)
            if batch:
                self.sim.apply_timestep(batch)
                for rid, r in enumerate(self.sim.robots):
                    self.trails[rid].append((r.x, r.y))
        self.next_timestep = target + 1

    def _step_once(self) -> None:
        if self.next_timestep > self.max_timestep:
            self.playing = False
            return
        batch = self.by_t.get(self.next_timestep, [])
        if batch:
            self.sim.apply_timestep(batch)
        else:
            self.sim.current_timestep = self.next_timestep
            self.sim.last_actions = []
        for rid, r in enumerate(self.sim.robots):
            self.trails[rid].append((r.x, r.y))
        self.next_timestep += 1

    def _animate(self, _frame):
        if self.live_mode:
            if self.playing:
                self._drain_live()
                self._draw_state()
            return []
        if self.playing:
            for _ in range(self.skip):
                self._step_once()
                if not self.playing:
                    break
            self._draw_state()
        return []

    def _draw_state(self) -> None:
        # Pallets.
        for p, rect, txt in zip(self.sim.pallets, self.pallet_patches, self.pallet_texts):
            rect.set_xy((p.x - 0.42, p.y - 0.42))
            txt.set_position((p.x, p.y))

            ratio = 0.0 if p.max_count == 0 else p.count / p.max_count
            if p.owner is not None:
                rect.set_facecolor("gold")
                rect.set_edgecolor(self.ROBOT_COLORS[p.owner % len(self.ROBOT_COLORS)])
                rect.set_linewidth(2.0)
                # A docked pallet moves with its robot. Make its SKU unmistakable.
                txt.set_text(f"SKU\n{p.sku}")
                txt.set_fontsize(8)
                txt.set_fontweight("bold")
                txt.set_color("black")
                txt.set_zorder(9)
            elif p.count == 0:
                txt.set_text(str(p.sku))
                txt.set_fontsize(5.5)
                txt.set_fontweight("normal")
                txt.set_color("black")
                txt.set_zorder(3)
                rect.set_facecolor("lightgray")
                rect.set_edgecolor("dimgray")
                rect.set_linewidth(0.8)
            else:
                txt.set_text(str(p.sku))
                txt.set_fontsize(5.5)
                txt.set_fontweight("normal")
                txt.set_color("black")
                txt.set_zorder(3)
                # Keep the palette simple but make low-stock pallets visibly paler.
                rect.set_facecolor("tan" if ratio > 0.2 else "wheat")
                rect.set_edgecolor("saddlebrown")
                rect.set_linewidth(0.7)

        # Robots and trails.
        for rid, r in enumerate(self.sim.robots):
            self.robot_patches[rid].center = (r.x, r.y)
            self.robot_texts[rid].set_position((r.x, r.y))
            self.robot_sku_texts[rid].set_visible(False)
            points = list(self.trails[rid])
            if self.show_trails and len(points) >= 2:
                xs, ys = zip(*points)
                self.trail_lines[rid].set_data(xs, ys)
                self.trail_lines[rid].set_visible(True)
            else:
                self.trail_lines[rid].set_visible(False)

        action_str = " | ".join(
            f"R{a.robot_id}:{a.verb}({a.x},{a.y})" for a in self.sim.last_actions
        ) or "wait"
        storage_str = "  ".join(
            f"R{r.id}[{sum(r.storage.values())}]" for r in self.sim.robots
        )
        held_parts = []
        for r in self.sim.robots:
            d = ",".join(f"SKU{self.sim.pallets[pid].sku}" for pid in sorted(r.docked)) or "-"
            inv = ",".join(f"SKU{sku}x{cnt}" for sku, cnt in sorted(r.storage.items()) if cnt) or "-"
            held_parts.append(f"R{r.id}: docked={d} picked={inv}")
        held_str = "  ".join(held_parts)
        docked = sum(len(r.docked) for r in self.sim.robots)
        t_label = f"{max(self.sim.current_timestep, 0):6d}" if self.live_mode else f"{max(self.sim.current_timestep, 0):6d}/{self.max_timestep}"
        live_state = "   LIVE" if self.live_mode and not self.live_done else ("   DONE" if self.live_mode else "")
        self.status_text.set_text(
            f"t={t_label}{live_state}   "
            f"fulfilled={self.sim.fulfilled_count}/{len(self.w.orders)}   "
            f"docked={docked}   replenishments={self.sim.replenishment_events}   "
            f"speed={self.skip}x   {'PLAY' if self.playing else 'PAUSE'}\n"
            f"storage items: {storage_str}\n"
            f"holding: {held_str}\n"
            f"actions: {action_str}"
        )
        self._draw_sidebar()
        self.fig.canvas.draw_idle()

    def _set_interval(self) -> None:
        # Matplotlib backends differ slightly; event_source is the portable API.
        try:
            self.anim.event_source.interval = self.interval_ms
        except Exception:
            pass

    def _on_key(self, event) -> None:
        key = (event.key or "").lower()
        if key == " ":
            self.playing = not self.playing
        elif key == "right":
            self.playing = False
            if self.live_mode:
                old_skip = self.skip
                self.skip = 1
                self._drain_live()
                self.skip = old_skip
            else:
                self._step_once()
        elif key in ("+", "="):
            if self.skip < 1024:
                self.skip *= 2
        elif key == "-":
            self.skip = max(1, self.skip // 2)
        elif key == "home":
            if not self.live_mode:
                self.playing = False
                self._replay_to(0)
        elif key == "g":
            self.show_grid = not self.show_grid
            self.ax.grid(self.show_grid, which="minor")
            self.ax.grid(self.show_grid, which="major")
        elif key == "l":
            self.show_trails = not self.show_trails
        elif key in ("q", "escape"):
            plt.close(self.fig)
            return
        self._draw_state()

    def show(self) -> None:
        plt.subplots_adjust(left=0.04, right=0.985, top=0.94, bottom=0.12)
        plt.show()


def print_summary(w: Worklist, actions: list[Action]) -> None:
    max_t = max((a.timestep for a in actions), default=0)
    counts = Counter(a.verb for a in actions)
    print("=== VISUALIZER INPUT ===")
    print(f"grid: {WIDTH}x{HEIGHT}")
    print(f"robots: {len(w.robots)}")
    print(f"pallets: {len(w.pallets)}")
    print(f"orders: {len(w.orders)}")
    print(f"actions: {len(actions)}")
    print(f"final_timestep: {max_t}")
    print("action_counts:", " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print("========================")


def main() -> int:
    ap = argparse.ArgumentParser(description="Animate Big Order robots and pallets using Matplotlib")
    ap.add_argument("worklist", help="BIG_ORDER.txt")
    ap.add_argument("solution", nargs="?", help="solution.txt for replay mode")
    ap.add_argument("--live-pipe", metavar="FIFO", help="consume live JSONL events from solver named pipe")
    ap.add_argument("--interval", type=int, default=60, help="milliseconds between animation frames")
    ap.add_argument("--skip", type=int, default=10, help="timesteps per animation frame")
    ap.add_argument("--trail", type=int, default=80, help="robot trail length")
    ap.add_argument("--start", type=int, default=0, help="initial replay timestep (replay only)")
    args = ap.parse_args()

    if not args.live_pipe and not args.solution:
        ap.error("provide solution.txt for replay, or --live-pipe FIFO for live mode")
    if args.live_pipe and args.solution:
        ap.error("use either solution.txt replay mode or --live-pipe, not both")

    w = parse_worklist(read_text(args.worklist))
    if args.live_pipe:
        print("=== LIVE VISUALIZER ===")
        print(f"grid: {WIDTH}x{HEIGHT}  robots: {len(w.robots)}  pallets: {len(w.pallets)}  orders: {len(w.orders)}")
        print(f"pipe: {args.live_pipe}")
        print("=======================")
        viz = WarehouseVisualizer(w, [], interval_ms=args.interval, skip=args.skip,
                                  trail_length=args.trail, live_pipe=args.live_pipe)
    else:
        actions = parse_actions(read_text(args.solution))
        print_summary(w, actions)
        viz = WarehouseVisualizer(w, actions, interval_ms=args.interval, skip=args.skip,
                                  trail_length=args.trail, start_timestep=args.start)
    viz.show()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
