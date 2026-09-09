#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import os
import heapq
import sys
import urllib.request
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

WIDTH = 60
HEIGHT = 40
FULFILL_Y = 0
REPLENISH_Y = 39
DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1))


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
    # pallet_id -> (dx,dy), pallet position relative to robot
    docked: dict[int, tuple[int, int]] = field(default_factory=dict)


@dataclass(frozen=True)
class Action:
    timestep: int
    robot_id: int
    verb: str
    x: int
    y: int

    def line(self) -> str:
        return f"{self.timestep} {self.robot_id} {self.verb} {self.x} {self.y}"


@dataclass
class Worklist:
    robots: list[tuple[int, int]]
    capacities: list[int]
    pallets: list[tuple[int, int, int]]
    orders: list[list[int]]


class ValidationError(RuntimeError):
    pass


class LivePipeWriter:
    """JSON-lines event stream for the live visualizer."""
    def __init__(self, path: str):
        self.path = path
        pp = Path(path)
        if pp.exists() and not pp.is_fifo():
            raise ValueError(f"live pipe path exists and is not a FIFO: {path}")
        if not pp.exists():
            os.mkfifo(path)
        print(f"LIVE: waiting for visualizer on {path}", flush=True)
        self.fp = open(path, "w", encoding="utf-8", buffering=1)
        print(f"LIVE: visualizer connected on {path}", flush=True)

    def emit(self, event_type: str, **data) -> None:
        payload = {"type": event_type, **data}
        try:
            self.fp.write(json.dumps(payload, separators=(",", ":")) + "\n")
        except BrokenPipeError:
            print("LIVE: visualizer disconnected; continuing without live stream", flush=True)
            try:
                self.fp.close()
            except Exception:
                pass
            self.fp = None

    def close(self) -> None:
        if self.fp is not None:
            try:
                self.fp.close()
            except Exception:
                pass
            self.fp = None


def read_text(source: str) -> str:
    if source.startswith("http://") or source.startswith("https://"):
        with urllib.request.urlopen(source) as r:  # nosec - user supplied challenge URL
            return r.read().decode("utf-8")
    return Path(source).read_text(encoding="utf-8")


def parse_worklist(text: str) -> Worklist:
    # Format contains no comments in BIG_ORDER.txt. Ignore blank lines and trailing comments
    # to make local experimentation friendlier.
    lines = []
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
            raise ValueError(f"expected one integer on logical line {i}, got: {parts}")
        return int(parts[0])

    nr = take_int()
    robots = []
    for _ in range(nr):
        x, y = map(int, lines[i].split())
        i += 1
        robots.append((x, y))

    ns = take_int()
    capacities = [take_int() for _ in range(ns)]

    np = take_int()
    pallets = []
    for _ in range(np):
        x, y, sku = map(int, lines[i].split())
        i += 1
        pallets.append((x, y, sku))

    no = take_int()
    orders = []
    for _ in range(no):
        if i >= len(lines):
            raise ValueError(f"expected {no} orders but file ended after {len(orders)}")
        order = list(map(int, lines[i].split()))
        i += 1
        orders.append(order)

    if i != len(lines):
        raise ValueError(f"unexpected extra data: {len(lines) - i} logical lines")

    # Structural checks.
    for rid, (x, y) in enumerate(robots):
        if not (0 <= x < WIDTH and 0 <= y < HEIGHT):
            raise ValueError(f"robot {rid} out of bounds at {(x, y)}")
    seen = set(robots)
    if len(seen) != len(robots):
        raise ValueError("two robots share a starting cell")

    pallet_cells = set()
    for pid, (x, y, sku) in enumerate(pallets):
        if not (0 <= x < WIDTH and 0 <= y < HEIGHT):
            raise ValueError(f"pallet {pid} out of bounds at {(x, y)}")
        if not (0 <= sku < ns):
            raise ValueError(f"pallet {pid} has invalid SKU {sku}")
        if (x, y) in seen or (x, y) in pallet_cells:
            raise ValueError(f"starting collision at pallet {pid} cell {(x, y)}")
        pallet_cells.add((x, y))

    for oid, order in enumerate(orders):
        if not order:
            raise ValueError(f"order {oid} is empty")
        bad = [s for s in order if not (0 <= s < ns)]
        if bad:
            raise ValueError(f"order {oid} contains invalid SKU {bad[0]}")

    return Worklist(robots, capacities, pallets, orders)


def print_parse_report(w: Worklist) -> None:
    demand = Counter(s for order in w.orders for s in order)
    order_freq = Counter(s for order in w.orders for s in set(order))
    pallet_count = Counter(s for _, _, s in w.pallets)
    initial_stock = {s: pallet_count[s] * w.capacities[s] for s in range(len(w.capacities))}

    print("=== PARSE REPORT ===")
    print("status: OK")
    print(f"grid: {WIDTH}x{HEIGHT}")
    print(f"robots: {len(w.robots)}")
    print("robot_starts:", ", ".join(f"R{i}=({x},{y})" for i, (x, y) in enumerate(w.robots)))
    print(f"skus: {len(w.capacities)}")
    print(f"pallets: {len(w.pallets)}")
    print(f"orders: {len(w.orders)}")
    print(f"total_items_requested: {sum(map(len, w.orders))}")
    sizes = [len(o) for o in w.orders]
    print(f"order_size_min/max/avg: {min(sizes)}/{max(sizes)}/{sum(sizes)/len(sizes):.2f}")
    print("top_10_skus_by_demand:")
    for sku, qty in demand.most_common(10):
        cap = w.capacities[sku]
        pc = pallet_count[sku]
        stock = initial_stock[sku]
        replenishments_lb = max(0, (qty - stock + cap - 1) // cap)
        print(
            f"  sku={sku:2d} demand={qty:4d} orders={order_freq[sku]:4d} "
            f"pallets={pc} capacity={cap} initial_stock={stock} repl_lb={replenishments_lb}"
        )
    missing = [s for s in demand if pallet_count[s] == 0]
    print(f"demanded_skus_without_pallet: {missing if missing else 'none'}")
    print("=== END PARSE REPORT ===")
    print()


class Simulator:
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
        self.pick_count = 0
        self.move_count = 0
        self.dock_count = 0
        self.undock_count = 0

    def occupied(self, ignore_robot: int | None = None, ignore_pallets: set[int] | None = None) -> dict[tuple[int, int], str]:
        ignore_pallets = ignore_pallets or set()
        occ: dict[tuple[int, int], str] = {}
        for r in self.robots:
            if r.id != ignore_robot:
                occ[(r.x, r.y)] = f"robot {r.id}"
        for p in self.pallets:
            if p.id not in ignore_pallets:
                occ[(p.x, p.y)] = f"pallet {p.id}"
        return occ

    def _check_global_collisions(self) -> None:
        cells: dict[tuple[int, int], str] = {}
        for r in self.robots:
            key = (r.x, r.y)
            if key in cells:
                raise ValidationError(f"collision: robot {r.id} and {cells[key]} at {key}")
            cells[key] = f"robot {r.id}"
        for p in self.pallets:
            key = (p.x, p.y)
            if key in cells:
                raise ValidationError(f"collision: pallet {p.id} and {cells[key]} at {key}")
            cells[key] = f"pallet {p.id}"

    def apply(self, a: Action) -> None:
        if not (0 <= a.robot_id < len(self.robots)):
            raise ValidationError(f"invalid robot id {a.robot_id}")
        r = self.robots[a.robot_id]

        if a.verb == "move":
            if abs(a.x - r.x) + abs(a.y - r.y) != 1:
                raise ValidationError(f"move target {(a.x,a.y)} is not adjacent to robot at {(r.x,r.y)}")
            if not (0 <= a.x < WIDTH and 0 <= a.y < HEIGHT):
                raise ValidationError(f"move target {(a.x,a.y)} is out of bounds")

            attached = set(r.docked)
            occ = self.occupied(ignore_robot=r.id, ignore_pallets=attached)
            dx, dy = a.x - r.x, a.y - r.y
            new_cells = [(a.x, a.y, "robot")]
            for pid in attached:
                p = self.pallets[pid]
                nx, ny = p.x + dx, p.y + dy
                if not (0 <= nx < WIDTH and 0 <= ny < HEIGHT):
                    raise ValidationError(f"docked pallet {pid} would move out of bounds to {(nx,ny)}")
                new_cells.append((nx, ny, f"pallet {pid}"))
            coords = [(x, y) for x, y, _ in new_cells]
            if len(coords) != len(set(coords)):
                raise ValidationError("robot/docked pallets overlap after move")
            for x, y, who in new_cells:
                if (x, y) in occ:
                    raise ValidationError(f"{who} would collide with {occ[(x,y)]} at {(x,y)}")

            r.x, r.y = a.x, a.y
            for pid in attached:
                p = self.pallets[pid]
                p.x += dx
                p.y += dy
            self.move_count += 1

        elif a.verb == "pick":
            candidates = [p for p in self.pallets if (p.x, p.y) == (a.x, a.y)]
            if not candidates:
                raise ValidationError(f"no pallet at pick coordinate {(a.x,a.y)}")
            p = candidates[0]
            if abs(p.x - r.x) + abs(p.y - r.y) != 1:
                raise ValidationError(f"pallet {p.id} is not adjacent to robot")
            if p.count <= 0:
                raise ValidationError(f"pallet {p.id} SKU {p.sku} is empty")
            p.count -= 1
            r.storage[p.sku] += 1
            self.pick_count += 1

        elif a.verb == "dock":
            candidates = [p for p in self.pallets if (p.x, p.y) == (a.x, a.y)]
            if not candidates:
                raise ValidationError(f"no pallet at dock coordinate {(a.x,a.y)}")
            p = candidates[0]
            if abs(p.x - r.x) + abs(p.y - r.y) != 1:
                raise ValidationError(f"pallet {p.id} is not adjacent to robot")
            if p.owner is not None:
                raise ValidationError(f"pallet {p.id} is already docked to robot {p.owner}")
            if len(r.docked) >= 4:
                raise ValidationError(f"robot {r.id} already has 4 docked pallets")
            rel = (p.x - r.x, p.y - r.y)
            if rel in r.docked.values():
                raise ValidationError(f"robot {r.id} already has a pallet docked on side {rel}")
            p.owner = r.id
            r.docked[p.id] = rel
            self.dock_count += 1

        elif a.verb == "undock":
            candidates = [p for p in self.pallets if (p.x, p.y) == (a.x, a.y)]
            if not candidates:
                raise ValidationError(f"no pallet at undock coordinate {(a.x,a.y)}")
            p = candidates[0]
            if p.owner != r.id or p.id not in r.docked:
                raise ValidationError(f"pallet {p.id} is not docked to robot {r.id}")
            p.owner = None
            del r.docked[p.id]
            self.undock_count += 1

        elif a.verb == "fulfill":
            if r.y != FULFILL_Y:
                raise ValidationError(f"robot {r.id} must be on y=0 to fulfill, is at {(r.x,r.y)}")
            match = None
            for oid, needed in enumerate(self.order_counters):
                if self.unfulfilled[oid] and needed == r.storage:
                    match = oid
                    break
            if match is None:
                raise ValidationError(f"storage {dict(r.storage)} does not exactly match an unfulfilled order")
            self.unfulfilled[match] = False
            self.fulfilled_count += 1
            r.storage.clear()

        else:
            raise ValidationError(f"unknown action '{a.verb}'")

        # Automatic replenishment occurs at END of timestep after the action.
        if r.y == REPLENISH_Y and r.docked:
            for pid in r.docked:
                p = self.pallets[pid]
                if p.count != p.max_count:
                    self.replenishment_events += 1
                p.count = p.max_count

        self._check_global_collisions()


class BaselineSolver:
    """Conservative one-robot baseline.

    Robot 0 processes orders sequentially. Other robots remain parked as obstacles.
    Empty pallets are replenished one at a time and returned to their original cell.
    This is intentionally score-naive but easy to reason about and validate.
    """

    def __init__(self, w: Worklist):
        self.w = w
        self.live = live
        self.sim = Simulator(w)
        self.actions: list[Action] = []
        self.t = 0
        self.rid = 0
        self.by_sku: dict[int, list[int]] = defaultdict(list)
        for p in self.sim.pallets:
            self.by_sku[p.sku].append(p.id)

    @property
    def r(self) -> Robot:
        return self.sim.robots[self.rid]

    def emit(self, verb: str, x: int, y: int) -> None:
        a = Action(self.t, self.rid, verb, x, y)
        self.sim.apply(a)
        self.actions.append(a)
        self.t += 1

    def _plain_path(self, goal_cells: set[tuple[int, int]]) -> list[tuple[int, int]]:
        start = (self.r.x, self.r.y)
        if start in goal_cells:
            return []
        occ = self.sim.occupied(ignore_robot=self.rid)
        q = deque([start])
        prev = {start: None}
        end = None
        while q:
            cur = q.popleft()
            for dx, dy in DIRS:
                nxt = (cur[0] + dx, cur[1] + dy)
                if not (0 <= nxt[0] < WIDTH and 0 <= nxt[1] < HEIGHT):
                    continue
                if nxt in prev or nxt in occ:
                    continue
                prev[nxt] = cur
                if nxt in goal_cells:
                    end = nxt
                    q.clear()
                    break
                q.append(nxt)
        if end is None:
            raise RuntimeError(f"no path from {start} to any of {sorted(goal_cells)}")
        path = []
        cur = end
        while cur != start:
            path.append(cur)
            cur = prev[cur]
        path.reverse()
        return path

    def _move_plain_to(self, goals: set[tuple[int, int]]) -> None:
        for x, y in self._plain_path(goals):
            self.emit("move", x, y)

    def _adjacent_free_cells(self, p: Pallet, allowed_rel: set[tuple[int, int]] | None = None) -> set[tuple[int, int]]:
        # rel is pallet - robot. So robot cell = pallet - rel.
        occ = self.sim.occupied(ignore_robot=self.rid)
        result = set()
        for rel in DIRS:
            if allowed_rel is not None and rel not in allowed_rel:
                continue
            rx, ry = p.x - rel[0], p.y - rel[1]
            if 0 <= rx < WIDTH and 0 <= ry < HEIGHT and ((rx, ry) == (self.r.x, self.r.y) or (rx, ry) not in occ):
                result.add((rx, ry))
        return result

    def _go_adjacent(self, p: Pallet) -> None:
        goals = self._adjacent_free_cells(p)
        if not goals:
            raise RuntimeError(f"pallet {p.id} at {(p.x,p.y)} has no reachable free adjacent cell")
        self._move_plain_to(goals)

    def _dockable_path_to_replenishment(self, pid: int, target_y: int) -> list[tuple[int, int]]:
        r = self.r
        p = self.sim.pallets[pid]
        rel = r.docked[pid]
        start = (r.x, r.y)
        attached = set(r.docked)
        occ = self.sim.occupied(ignore_robot=r.id, ignore_pallets=attached)

        def valid(rx: int, ry: int) -> bool:
            if not (0 <= rx < WIDTH and 0 <= ry < HEIGHT):
                return False
            pc = (rx + rel[0], ry + rel[1])
            if not (0 <= pc[0] < WIDTH and 0 <= pc[1] < HEIGHT):
                return False
            if (rx, ry) in occ or pc in occ or pc == (rx, ry):
                return False
            return True

        q = deque([start])
        prev = {start: None}
        end = None
        while q:
            cur = q.popleft()
            if cur[1] == target_y:
                end = cur
                break
            for dx, dy in DIRS:
                nxt = (cur[0] + dx, cur[1] + dy)
                if nxt not in prev and valid(*nxt):
                    prev[nxt] = cur
                    q.append(nxt)
        if end is None:
            raise RuntimeError(f"no docked path for pallet {p.id} from {start} to robot y={target_y}")
        path = []
        cur = end
        while cur != start:
            path.append(cur)
            cur = prev[cur]
        path.reverse()
        return path

    def _docked_path_to_exact(self, pid: int, goal: tuple[int, int]) -> list[tuple[int, int]]:
        r = self.r
        rel = r.docked[pid]
        start = (r.x, r.y)
        attached = set(r.docked)
        occ = self.sim.occupied(ignore_robot=r.id, ignore_pallets=attached)

        def valid(rx: int, ry: int) -> bool:
            if not (0 <= rx < WIDTH and 0 <= ry < HEIGHT):
                return False
            pc = (rx + rel[0], ry + rel[1])
            return (
                0 <= pc[0] < WIDTH and 0 <= pc[1] < HEIGHT
                and (rx, ry) not in occ and pc not in occ and pc != (rx, ry)
            )

        q = deque([start])
        prev = {start: None}
        while q:
            cur = q.popleft()
            if cur == goal:
                break
            for dx, dy in DIRS:
                nxt = (cur[0] + dx, cur[1] + dy)
                if nxt not in prev and valid(*nxt):
                    prev[nxt] = cur
                    q.append(nxt)
        if goal not in prev:
            raise RuntimeError(f"no docked path back to {goal} for pallet {pid}")
        path = []
        cur = goal
        while cur != start:
            path.append(cur)
            cur = prev[cur]
        path.reverse()
        return path

    def replenish(self, pid: int) -> None:
        p = self.sim.pallets[pid]
        original = (p.x, p.y)

        # Avoid docking with pallet south of robot, because that footprint cannot put
        # the robot on y=39 without pushing the pallet out of bounds. Prefer east/west/north.
        safe_rels = {(1, 0), (-1, 0), (0, -1)}
        goals = self._adjacent_free_cells(p, safe_rels)
        if not goals:
            raise RuntimeError(f"cannot find safe docking side for pallet {pid}")
        self._move_plain_to(goals)
        rel = (p.x - self.r.x, p.y - self.r.y)
        self.emit("dock", p.x, p.y)

        return_robot_cell = (original[0] - rel[0], original[1] - rel[1])
        for x, y in self._dockable_path_to_replenishment(pid, REPLENISH_Y):
            self.emit("move", x, y)
        # Refill has already happened at end of the move that entered y=39.
        if self.sim.pallets[pid].count != self.sim.pallets[pid].max_count:
            raise RuntimeError(f"internal error: pallet {pid} did not refill")

        for x, y in self._docked_path_to_exact(pid, return_robot_cell):
            self.emit("move", x, y)
        p = self.sim.pallets[pid]
        if (p.x, p.y) != original:
            raise RuntimeError(f"internal error: pallet {pid} returned to {(p.x,p.y)}, expected {original}")
        self.emit("undock", p.x, p.y)

    def _choose_nonempty_pallet(self, sku: int) -> Pallet | None:
        candidates = [self.sim.pallets[pid] for pid in self.by_sku[sku] if self.sim.pallets[pid].count > 0]
        if not candidates:
            return None
        # Cheap nearest approximation. Actual path is BFS once selected.
        return min(candidates, key=lambda p: abs(self.r.x - p.x) + abs(self.r.y - p.y))

    def _choose_replenish_pallet(self, sku: int) -> Pallet:
        ids = self.by_sku.get(sku, [])
        if not ids:
            raise RuntimeError(f"SKU {sku} has no pallet")
        return min(
            (self.sim.pallets[pid] for pid in ids),
            key=lambda p: abs(self.r.x - p.x) + abs(self.r.y - p.y),
        )

    def collect_order(self, order: list[int]) -> None:
        needed = Counter(order)
        # Greedy SKU ordering: repeatedly go to the currently nearest available pallet.
        # This is intentionally simple; optimization comes after correctness.
        while needed:
            choices = []
            for sku in needed:
                p = self._choose_nonempty_pallet(sku)
                if p is not None:
                    d = abs(self.r.x - p.x) + abs(self.r.y - p.y)
                    choices.append((d, sku, p.id))
            if not choices:
                # Every remaining SKU is empty. Replenish the nearest needed SKU pallet.
                repl_choices = []
                for sku in needed:
                    p = self._choose_replenish_pallet(sku)
                    repl_choices.append((abs(self.r.x-p.x)+abs(self.r.y-p.y), sku, p.id))
                _, sku, pid = min(repl_choices)
                self.replenish(pid)
                continue

            _, sku, pid = min(choices)
            p = self.sim.pallets[pid]
            self._go_adjacent(p)
            take = min(needed[sku], p.count)
            for _ in range(take):
                self.emit("pick", p.x, p.y)
            needed[sku] -= take
            if needed[sku] == 0:
                del needed[sku]

    def fulfill(self) -> None:
        goals = {(x, FULFILL_Y) for x in range(WIDTH)}
        self._move_plain_to(goals)
        self.emit("fulfill", 0, 0)

    def solve(self, progress_every: int = 25) -> list[Action]:
        for oid, order in enumerate(self.w.orders):
            self.collect_order(order)
            self.fulfill()
            if progress_every and ((oid + 1) % progress_every == 0 or oid + 1 == len(self.w.orders)):
                print(f"SOLVER progress: {oid+1}/{len(self.w.orders)} orders, timestep={self.t}")
        return self.actions


@dataclass
class RobotJob:
    order_id: int | None = None
    needed: Counter[int] = field(default_factory=Counter)
    planned_orders: deque[int] = field(default_factory=deque)
    target_pid: int | None = None
    target_sku: int | None = None
    repl_pid: int | None = None
    repl_phase: str | None = None
    repl_original: tuple[int, int] | None = None
    repl_return_robot: tuple[int, int] | None = None
    repl_stall_ticks: int = 0
    repl_phase_since: int = 0


class MultiRobotSolver:
    """Concurrent solver with configurable fulfillment/logistics roles with exclusive pallet targeting.

    Fulfillment robots collect one physically fulfillable order at a time, while keeping
    a configurable queue of upcoming orders for locality-aware lookahead. Logistics
    robots do not pick order items: they service replenishment requests generated when
    fulfillment robots need a SKU whose pallets are empty.

    Collision handling remains deliberately conservative: no entity may enter a cell
    occupied by another entity at the start of the current timestep.
    """

    def __init__(self, w: Worklist, logistics_ids: set[int] | None = None, order_lookahead: int = 2, yield_after: int = 8, master_stop_after: int = 24, target_cooldown: int = 40, live: LivePipeWriter | None = None):
        self.w = w
        self.live = live
        self.sim = Simulator(w)
        self.actions: list[Action] = []
        self.t = 0
        self.jobs = [RobotJob() for _ in self.sim.robots]
        self.unassigned: set[int] = set(range(len(w.orders)))
        self.completed_jobs = 0
        self.by_sku: dict[int, list[int]] = defaultdict(list)
        for p in self.sim.pallets:
            self.by_sku[p.sku].append(p.id)

        self.logistics_ids = set(logistics_ids or set())
        all_ids = set(range(len(self.sim.robots)))
        bad = self.logistics_ids - all_ids
        if bad:
            raise ValueError(f"invalid logistics robot ids: {sorted(bad)}")
        self.fulfillment_ids = sorted(all_ids - self.logistics_ids)
        if not self.fulfillment_ids:
            raise ValueError("at least one fulfillment robot is required")
        self.order_lookahead = max(1, int(order_lookahead))
        self.yield_after = max(1, int(yield_after))
        self.master_stop_after = max(self.yield_after, int(master_stop_after))
        self.target_cooldown = max(1, int(target_cooldown))
        self.target_since = [0 for _ in self.sim.robots]
        # (robot_id, pallet_id) -> first timestep at which this pair may be assigned again.
        self.target_pair_cooldown: dict[tuple[int, int], int] = {}
        # Per-robot lack-of-progress counters. A robot can be locally deadlocked even
        # while other robots are still moving, so global-idle detection is insufficient.
        self.blocked_ticks = [0 for _ in self.sim.robots]
        # Central traffic arbitration age. A robot denied an intersection gets
        # increasing priority on later timesteps instead of repeatedly losing.
        self.traffic_wait_ticks = [0 for _ in self.sim.robots]
        # One-tick clearance requests plus persistent right-of-way holds.
        # A lower-priority robot that conflicts with a higher-priority robot must
        # clear the requested cell (if physically occupying it), then remain stopped
        # until the higher-priority robot finishes its CURRENT mission.  This avoids
        # multi-timestep "dancing" where both robots repeatedly try to go around.
        self.clearance_requests: dict[int, tuple[int, tuple[int, int]]] = {}
        # lower_rid -> {priority, cell, token, created, reason}
        self.priority_holds: dict[int, dict[str, object]] = {}
        # Persistent pull-out destination for each yielding robot.  This is deliberately
        # fixed when a conflict is created; recomputing an escape direction every tick
        # is what caused horizontal "dancing" between opposing robots.
        self.yield_targets: dict[int, tuple[int, int]] = {}

        # SKU replenishment requests. A SKU is queued at most once at a time.
        self.replenish_queue: deque[int] = deque()
        self.replenish_requested: set[int] = set()
        self.logistics_sku: dict[int, int | None] = {rid: None for rid in self.logistics_ids}
        # A replenishment pallet can be geometrically unreachable even though it is the
        # nearest by Manhattan distance.  Failed dock targets are temporarily blacklisted
        # so logistics immediately tries another pallet for the same SKU.
        self.repl_pallet_cooldown: dict[tuple[int, int], int] = {}  # (sku,pid) -> retry timestep
        self.repl_dock_stall_limit = 40
        self.repl_phase_stall_limit = 250

        # Resource-wait parking. A robot that still needs items but temporarily has
        # no executable pallet target must not become permanent furniture in an aisle.
        # Targets are selected dynamically from open, roomy cells and are cleared as
        # soon as real work becomes executable again.
        self.resource_park_target: dict[int, tuple[int, int] | None] = {
            rid: None for rid in range(len(self.sim.robots))
        }

    def _emit(self, event_type: str, **data) -> None:
        if self.live is not None and self.live.fp is not None:
            self.live.emit(event_type, **data)

    def _record_action(self, a: Action) -> None:
        self.actions.append(a)
        self._emit("action", t=a.timestep, robot=a.robot_id, verb=a.verb, x=a.x, y=a.y)

    def _priority_rank(self, rid: int) -> int:
        """Smaller rank means higher master traffic priority.

        Active-work policy:
            R4 replenishment > active R0 > R1 > R2 > R3 > active R4 fallback

        A robot that has NO executable work (idle, or an unfinished order with no
        pallet/replenishment target) is traffic furniture, not a priority claimant.
        It is deliberately ranked below every robot that can actually make progress so
        the master can order it out of an aisle.
        """
        job = self.jobs[rid]
        if rid in self.logistics_ids and job.repl_pid is not None:
            return 0

        has_missing = bool(self._missing_for_robot(rid)) if job.order_id is not None else False
        executable_fulfillment = (job.order_id is not None and
                                  (not has_missing or job.target_pid is not None))

        if rid in self.fulfillment_ids and executable_fulfillment:
            return 1 + rid
        if rid in self.logistics_ids and executable_fulfillment:
            return 1 + len(self.sim.robots) + rid

        # Resource-wait and truly idle robots yield to every executable worker.
        return 100 + rid

    def _traffic_priority(self, rid: int) -> tuple[int, int]:
        """Sort key: role-aware master priority, then robot id for determinism."""
        return (self._priority_rank(rid), rid)

    def _higher_priority(self, a: int, b: int) -> bool:
        return self._traffic_priority(a) < self._traffic_priority(b)

    def _dynamic_owner_at(self, cell: tuple[int, int], ignore_rid: int | None = None) -> int | None:
        """Return robot owning a dynamic occupied cell (robot body or docked pallet)."""
        for r in self.sim.robots:
            if r.id == ignore_rid:
                continue
            if (r.x, r.y) == cell:
                return r.id
            for pid in r.docked:
                p = self.sim.pallets[pid]
                if (p.x, p.y) == cell:
                    return r.id
        return None

    def _priority_keepout(self, priority_rid: int, blocked_cell: tuple[int, int]) -> set[tuple[int, int]]:
        """Build the local passage zone ONCE when a hold is installed."""
        pr = self.sim.robots[priority_rid]
        zone: set[tuple[int, int]] = set()
        for dx in range(-2, 3):
            for dy in range(-2, 3):
                if abs(dx) + abs(dy) <= 2:
                    x, y = pr.x + dx, pr.y + dy
                    if 0 <= x < WIDTH and 0 <= y < HEIGHT:
                        zone.add((x, y))
        sx = 0 if blocked_cell[0] == pr.x else (1 if blocked_cell[0] > pr.x else -1)
        sy = 0 if blocked_cell[1] == pr.y else (1 if blocked_cell[1] > pr.y else -1)
        x, y = pr.x, pr.y
        for _ in range(4):
            x += sx
            y += sy
            if 0 <= x < WIDTH and 0 <= y < HEIGHT:
                zone.add((x, y))
                for px, py in ((x+1,y),(x-1,y),(x,y+1),(x,y-1)):
                    if 0 <= px < WIDTH and 0 <= py < HEIGHT:
                        zone.add((px, py))
        return zone

    def _static_anchor_free(self, rid: int, rx: int, ry: int) -> bool:
        """Can this robot footprint occupy anchor (rx,ry), ignoring robot bodies?"""
        r = self.sim.robots[rid]
        attached = set(r.docked)
        rel = [(0, 0)]
        for pid in attached:
            p = self.sim.pallets[pid]
            rel.append((p.x-r.x, p.y-r.y))
        hard: set[tuple[int,int]] = set()
        for p in self.sim.pallets:
            if p.id in attached:
                continue
            hard.add((p.x,p.y))
        for dx,dy in rel:
            c=(rx+dx, ry+dy)
            if not (0 <= c[0] < WIDTH and 0 <= c[1] < HEIGHT):
                return False
            if c in hard:
                return False
        return True

    def _choose_yield_target(self, lower: int, higher: int, zone: set[tuple[int,int]]) -> tuple[int,int] | None:
        """Choose a deterministic pull-out anchor away from the winner's passage.

        Prefer changing ROW for a primarily horizontal encounter, and changing COLUMN
        for a primarily vertical encounter.  The target is fixed for the duration of
        the hold so the loser cannot oscillate between equally-good side steps.
        """
        lr=self.sim.robots[lower]
        hr=self.sim.robots[higher]
        horizontal = abs(lr.x-hr.x) >= abs(lr.y-hr.y)
        best=None
        # Search a modest neighborhood; warehouse aisles are much narrower than this.
        for radius in range(1, 9):
            candidates=[]
            for dx in range(-radius, radius+1):
                rem=radius-abs(dx)
                dys={rem, -rem}
                for dy in dys:
                    x,y=lr.x+dx, lr.y+dy
                    if not (0 <= x < WIDTH and 0 <= y < HEIGHT):
                        continue
                    if not self._static_anchor_free(lower,x,y):
                        continue
                    # Whole future footprint must be outside the frozen passage zone.
                    rr=self.sim.robots[lower]
                    ddx,ddy=x-rr.x,y-rr.y
                    fp={(x,y)}
                    for pid in rr.docked:
                        pp=self.sim.pallets[pid]
                        fp.add((pp.x+ddx,pp.y+ddy))
                    if fp & zone:
                        continue
                    # Never send two yielding robots to the same pull-out anchor.
                    occupied_pullouts = {t for rrid, t in self.yield_targets.items() if rrid != lower}
                    if (x, y) in occupied_pullouts:
                        continue
                    # Keep a real buffer from the priority robot.  Merely leaving the
                    # frozen corridor by one cell is what created the visible crowding.
                    if abs(x - hr.x) + abs(y - hr.y) < 4:
                        continue
                    # For horizontal head-on traffic, strongly prefer a different row;
                    # for vertical traffic, strongly prefer a different column.
                    lateral = abs(y-lr.y) if horizontal else abs(x-lr.x)
                    axial = abs(x-lr.x) if horizontal else abs(y-lr.y)
                    # Deterministic tie break: lower priority IDs favor + side, higher IDs - side.
                    side = (y-lr.y) if horizontal else (x-lr.x)
                    desired_sign = 1 if (lower % 2 == 0) else -1
                    side_penalty = 0 if side == 0 or (1 if side > 0 else -1) == desired_sign else 1
                    score=(0 if lateral>0 else 1, axial, -lateral, side_penalty, y, x)
                    candidates.append((score,(x,y)))
            if candidates:
                candidates.sort()
                return candidates[0][1]
        return best

    def _hold_is_clear(self, rid: int, hold: dict[str, object]) -> bool:
        zone = set(hold.get("zone", ()))
        tgt = self.yield_targets.get(rid)
        r=self.sim.robots[rid]
        return tgt is not None and (r.x,r.y) == tgt and self._own_cells(rid).isdisjoint(zone)

    def _mission_token(self, rid: int) -> tuple:
        """Stable token for the higher-priority robot's current piece of work."""
        j = self.jobs[rid]
        if j.repl_pid is not None:
            return ("repl", j.repl_pid, j.repl_phase)
        if j.target_pid is not None:
            return ("target", j.order_id, j.target_pid, j.target_sku)
        if j.order_id is not None:
            return ("order", j.order_id)
        return ("idle",)

    def _install_priority_hold(self, lower: int, higher: int, cell: tuple[int, int], reason: str) -> None:
        """Give HIGHER persistent right-of-way through the local conflict corridor.

        LOWER clears the corridor and waits until HIGHER has passed the conflict.
        This is deliberately a local traffic lock, not an entire-order lock.

        Priority is role-aware.  Active logistics replenishment outranks every
        fulfillment robot; otherwise R0 > R1 > R2 > R3 > logistics fallback.
        If several robots request the same lower-priority robot, the currently
        highest-priority requester owns the hold.
        """
        # Collapse a chain such as R3->R2 and R2->R0 into R3->R0.
        # Pairwise chains were the source of multi-robot 'dancing': every loser
        # tried to clear a different moving winner.  A crowd gets ONE winner.
        seen = set()
        while higher in self.priority_holds and higher not in seen:
            seen.add(higher)
            higher = int(self.priority_holds[higher]["priority"])

        if not self._higher_priority(higher, lower):
            return
        old = self.priority_holds.get(lower)
        if old is not None and not self._higher_priority(higher, int(old["priority"])):
            return
        zone = self._priority_keepout(higher, cell)
        target = self._choose_yield_target(lower, higher, zone)
        self.priority_holds[lower] = {
            "priority": higher,
            "cell": cell,
            "zone": tuple(sorted(zone)),
            "token": self._mission_token(higher),
            "created": self.t,
            "reason": reason,
        }
        if target is not None:
            self.yield_targets[lower] = target
        self._emit("priority_hold", t=self.t, robot=lower, for_robot=higher,
                   x=cell[0], y=cell[1], reason=reason, yield_target=target)
        print(f"MASTER hold: timestep={self.t} R{lower} YIELD for R{higher} pullout={target}", flush=True)

    def _normalize_crowd_holds(self) -> None:
        """Resolve nearby multi-robot congestion as ONE master-controlled group.

        Pairwise right-of-way is insufficient when 3+ robots crowd the same aisle:
        R3 may yield to R2 while R2 yields to R0, producing a moving chain.  Here
        every connected crowd elects one winner and every other member yields to
        that same winner with a distinct fixed pull-out.
        """
        n = len(self.sim.robots)
        # Nearby robots are in the same crowd. Distance 3 catches the condition
        # before bodies become immediately adjacent.
        adj = {i: set() for i in range(n)}
        for i in range(n):
            ri = self.sim.robots[i]
            for j in range(i + 1, n):
                rj = self.sim.robots[j]
                d = abs(ri.x - rj.x) + abs(ri.y - rj.y)
                if d <= 3:
                    adj[i].add(j)
                    adj[j].add(i)

        seen = set()
        for seed in range(n):
            if seed in seen or not adj[seed]:
                continue
            stack = [seed]
            comp = []
            while stack:
                u = stack.pop()
                if u in seen:
                    continue
                seen.add(u)
                comp.append(u)
                stack.extend(adj[u] - seen)
            if len(comp) < 2:
                continue

            # Only executable robots compete for the token. If none is executable,
            # leave the group alone; resource scheduling will handle them.
            executable = [r for r in comp if self._priority_rank(r) < 100]
            if not executable:
                continue
            winner = min(executable, key=self._traffic_priority)
            wr = self.sim.robots[winner]

            # If the winner itself was yielding to a member of this crowd, remove
            # that contradictory pairwise hold. The crowd has one root winner.
            if winner in self.priority_holds:
                self.priority_holds.pop(winner, None)
                self.yield_targets.pop(winner, None)

            for loser in sorted((r for r in comp if r != winner),
                                key=self._traffic_priority, reverse=True):
                lr = self.sim.robots[loser]
                # Install/refresh only when loser is not already yielding to winner.
                h = self.priority_holds.get(loser)
                if h is not None and int(h.get("priority", -1)) == winner:
                    continue

                # Use the midpoint/loser cell as conflict direction hint.
                cell = (lr.x, lr.y)
                self._install_priority_hold(loser, winner, cell, "crowd-group")
                h = self.priority_holds.get(loser)
                if h is not None:
                    self._emit("crowd_yield", t=self.t, robot=loser,
                               for_robot=winner,
                               yield_target=self.yield_targets.get(loser))

            self._emit("crowd_control", t=self.t, winner=winner,
                       robots=sorted(comp))
            if self.t % 25 == 0:
                print(
                    f"MASTER crowd: timestep={self.t} winner=R{winner} "
                    f"group={sorted(comp)} "
                    f"pullouts={{{', '.join(f'R{r}:{self.yield_targets.get(r)}' for r in comp if r != winner)}}}",
                    flush=True,
                )

    def _release_finished_priority_holds(self) -> None:
        """Release a right-of-way hold after the higher robot has PASSED the conflict.

        A hold is a traffic lock, not an order lock.  The lower-priority robot must
        first clear the reserved corridor.  Once clear, it waits only until the
        higher-priority robot has moved beyond the original conflict cell by a small
        passage distance.  This prevents the old failure mode where R2 could remain
        frozen for thousands of timesteps while R0 completed an entire order.
        """
        PASS_DISTANCE = 2
        MIN_HOLD_TICKS = 1
        for lower, hold in list(self.priority_holds.items()):
            higher = int(hold["priority"])
            cell = tuple(hold["cell"])

            # Dynamic role priority can change (notably R4 entering replenishment).
            if not self._higher_priority(higher, lower):
                reason = "priority-changed"
            elif self._mission_token(higher) != hold["token"]:
                reason = "higher-mission-changed"
            else:
                age = self.t - int(hold.get("created", self.t))
                lower_clear = self._hold_is_clear(lower, hold)
                hr = self.sim.robots[higher]
                passed = abs(hr.x - cell[0]) + abs(hr.y - cell[1]) >= PASS_DISTANCE
                reason = "higher-passed" if age >= MIN_HOLD_TICKS and lower_clear and passed else None

            # A fixed pull-out is advisory, never a reason to freeze the scheduler.
            # If it has not been achieved quickly, drop the persistent hold.  A real
            # immediate conflict will simply recreate a fresh local clearance request.
            if reason is None:
                age = self.t - int(hold.get("created", self.t))
                if age >= 20:
                    reason = "hold-timeout-replan"

            if reason is not None:
                del self.priority_holds[lower]
                self.yield_targets.pop(lower, None)
                self.traffic_wait_ticks[lower] = 0
                self.blocked_ticks[lower] = 0
                self._emit("priority_release", t=self.t, robot=lower, for_robot=higher, reason=reason)
                print(
                    f"MASTER release: timestep={self.t} R{lower} may resume; "
                    f"R{higher} cleared passage reason={reason}",
                    flush=True,
                )

    def _uncleared_yielders_for(self, priority_rid: int) -> list[int]:
        """Lower-priority robots that are still physically clearing this robot's passage.

        This creates a strict two-phase right-of-way handshake:
          1) higher robot STOPS while lower robot moves out of the reserved corridor;
          2) once every yielder is clear, higher robot proceeds while lower robots HOLD.

        Without this handshake the reserved corridor moved forward with the higher robot
        every tick, effectively snowplowing the yielding robot and causing the visual
        'butting heads' behavior.
        """
        out = []
        for lower, hold in self.priority_holds.items():
            if int(hold["priority"]) != priority_rid:
                continue
            if not self._hold_is_clear(lower, hold):
                out.append(lower)
        return sorted(out, key=self._traffic_priority, reverse=True)

    def _request_clearance(self, blocker: int, priority_rid: int, cell: tuple[int, int]) -> None:
        """Ask a lower-priority robot to clear a cell THIS timestep only.

        No persistent corridor/aisle state is created.  The master recomputes the
        constraint from actual robot positions on the next timestep.
        """
        if not self._higher_priority(priority_rid, blocker):
            return
        old = self.clearance_requests.get(blocker)
        # Keep the highest-priority requester according to the current role-aware policy.
        if old is None or self._higher_priority(priority_rid, old[0]):
            self.clearance_requests[blocker] = (priority_rid, cell)
            self._emit("master_clear", t=self.t, robot=blocker, for_robot=priority_rid,
                       x=cell[0], y=cell[1], reason="clear-priority-path")
            print(
                f"MASTER clear: timestep={self.t} R{blocker} move aside for R{priority_rid} cell={cell}",
                flush=True,
            )

    def _clearance_step(self, rid: int, priority_rid: int, blocked_cell: tuple[int, int],
                        start_cells: set[tuple[int, int]],
                        reserved_next: dict[tuple[int, int], int]) -> tuple[int, int] | None:
        """Choose one adjacent move that clears the higher-priority robot.

        This is intentionally local.  A yielding robot never chases a distant pullout
        and never carries a traffic hold into the next timestep.
        """
        r = self.sim.robots[rid]
        h = self.sim.robots[priority_rid]

        # Prefer moving perpendicular to the line between blocker and winner.
        dx = r.x - h.x
        dy = r.y - h.y
        if abs(dx) >= abs(dy):
            prefs = [(0, -1), (0, 1), (1 if dx >= 0 else -1, 0),
                     (-1 if dx >= 0 else 1, 0)]
        else:
            prefs = [(-1, 0), (1, 0), (0, 1 if dy >= 0 else -1),
                     (0, -1 if dy >= 0 else 1)]

        # Deterministic side choice avoids left/right oscillation when both are free.
        if rid % 2:
            prefs[0], prefs[1] = prefs[1], prefs[0]

        candidates = []
        for pref, (sx, sy) in enumerate(prefs):
            nx, ny = r.x + sx, r.y + sy
            if not self._valid_footprint(rid, nx, ny, start_cells):
                continue
            future = self._proposed_move_footprint(rid, nx, ny)
            if any(c in reserved_next for c in future):
                continue

            # Prefer increasing separation from both the winner and contested cell.
            old_sep = abs(r.x-h.x) + abs(r.y-h.y)
            new_sep = abs(nx-h.x) + abs(ny-h.y)
            old_block = abs(r.x-blocked_cell[0]) + abs(r.y-blocked_cell[1])
            new_block = abs(nx-blocked_cell[0]) + abs(ny-blocked_cell[1])
            score = (
                0 if new_sep > old_sep else 1,
                0 if new_block > old_block else 1,
                pref,
            )
            candidates.append((score, (nx, ny)))

        if not candidates:
            return None
        candidates.sort()
        return candidates[0][1]

    def _proposed_move_footprint(self, rid: int, x: int, y: int) -> set[tuple[int, int]]:
        """Robot + docked pallet cells after moving the robot anchor to (x,y)."""
        r = self.sim.robots[rid]
        dx, dy = x - r.x, y - r.y
        cells = {(x, y)}
        for pid in r.docked:
            p = self.sim.pallets[pid]
            cells.add((p.x + dx, p.y + dy))
        return cells

    def _master_wait(self, rid: int, reason: str, blocker: int | None = None) -> None:
        self.traffic_wait_ticks[rid] += 1
        self.blocked_ticks[rid] += 1
        suffix = f" for R{blocker}" if blocker is not None else ""
        # Keep the pipe detailed, but do not flood the solver terminal with every
        # single intersection wait. Show first and periodic waits only.
        if self.traffic_wait_ticks[rid] == 1 or self.traffic_wait_ticks[rid] % 10 == 0:
            print(f"MASTER wait: timestep={self.t} R{rid}{suffix} reason={reason} wait={self.traffic_wait_ticks[rid]}", flush=True)
        self._emit(
            "master_wait", t=self.t, robot=rid, blocker=blocker, reason=reason,
            wait_ticks=self.traffic_wait_ticks[rid],
        )

    def _own_cells(self, rid: int) -> set[tuple[int, int]]:
        r = self.sim.robots[rid]
        return {(r.x, r.y)} | {(self.sim.pallets[pid].x, self.sim.pallets[pid].y) for pid in r.docked}

    def _start_cells(self) -> set[tuple[int, int]]:
        return {(r.x, r.y) for r in self.sim.robots} | {(p.x, p.y) for p in self.sim.pallets}

    def _order_score(self, rid: int, oid: int) -> float:
        r = self.sim.robots[rid]
        order = self.w.orders[oid]
        uniq = set(order)
        nearest = []
        for sku in uniq:
            ds = [abs(r.x - self.sim.pallets[pid].x) + abs(r.y - self.sim.pallets[pid].y)
                  for pid in self.by_sku[sku]]
            nearest.append(min(ds) if ds else 10_000)
        # Bias toward nearby orders; item count is a small tie breaker because picks cost timesteps too.
        return (sum(nearest) / max(1, len(nearest))) + 0.08 * len(order)

    def _fill_order_queue(self, rid: int) -> None:
        """Keep future orders reserved for a robot currently allowed to fulfill.

        Dedicated logistics robots may borrow fulfillment work only while they are not
        replenishing.  They remain lowest traffic priority.
        """
        if rid not in self.fulfillment_ids and rid not in self.logistics_ids:
            return
        if rid in self.logistics_ids and self.jobs[rid].repl_pid is not None:
            return
        job = self.jobs[rid]
        while len(job.planned_orders) < self.order_lookahead and self.unassigned:
            # Score from current robot position; favor orders that share SKUs with already
            # reserved lookahead orders so successive orders tend to reuse the same area.
            candidates = sorted(self.unassigned)[:120]
            reserved_skus = set()
            for oid0 in job.planned_orders:
                reserved_skus.update(self.w.orders[oid0])

            def score(oid: int) -> float:
                base = self._order_score(rid, oid)
                overlap = len(set(self.w.orders[oid]) & reserved_skus)
                return base - 1.5 * overlap

            oid = min(candidates, key=score)
            self.unassigned.remove(oid)
            job.planned_orders.append(oid)

    def _assign_if_idle(self, rid: int) -> None:
        if rid not in self.fulfillment_ids and rid not in self.logistics_ids:
            return
        job = self.jobs[rid]
        # Only urgent stockouts block R4 from borrowing fulfillment work.
        if rid in self.logistics_ids:
            if job.repl_pid is not None:
                return
            urgent_skus, _ = self._urgent_replenishment()
            if any(sku in urgent_skus for sku in self.replenish_queue):
                return
        if job.order_id is not None:
            return
        self._fill_order_queue(rid)
        if job.order_id is not None or not job.planned_orders:
            return
        oid = job.planned_orders.popleft()
        job.order_id = oid
        job.needed = Counter(self.w.orders[oid])
        job.target_pid = None
        job.target_sku = None
        self._fill_order_queue(rid)

    def _request_replenishment(self, sku: int) -> None:
        if sku not in self.replenish_requested:
            self.replenish_requested.add(sku)
            self.replenish_queue.append(sku)
            print(f"SOLVER replenish-request: timestep={self.t} SKU={sku}", flush=True)
            self._emit("replenish_request", t=self.t, sku=sku)

    def _urgent_replenishment(self) -> tuple[set[int], Counter[int]]:
        """Return SKUs that are genuinely blocking active orders."""
        active_missing: Counter[int] = Counter()
        for rid in range(len(self.sim.robots)):
            for sku, n in self._missing_for_robot(rid).items():
                active_missing[sku] += n

        urgent: set[int] = set()
        for sku, qty in active_missing.items():
            if qty <= 0:
                continue
            free_stock = any(
                self.sim.pallets[pid].count > 0
                and self.sim.pallets[pid].owner is None
                for pid in self.by_sku[sku]
            )
            if not free_stock:
                urgent.add(sku)
        return urgent, active_missing

    def _refresh_replenishment_queue(self) -> None:
        """Rebuild replenishment work with urgent stockouts before background empties."""
        urgent_skus, active_missing = self._urgent_replenishment()

        available_empty: set[int] = set()
        for sku, pids in self.by_sku.items():
            if any(
                self.sim.pallets[pid].count == 0
                and self.sim.pallets[pid].owner is None
                and self.repl_pallet_cooldown.get((sku, pid), 0) <= self.t
                for pid in pids
            ):
                available_empty.add(sku)

        in_service = {sku for sku in self.logistics_sku.values() if sku is not None}
        available_empty -= in_service

        urgent = sorted(
            (sku for sku in available_empty if sku in urgent_skus),
            key=lambda sku: (-active_missing.get(sku, 0), sku),
        )
        background = sorted(sku for sku in available_empty if sku not in urgent_skus)

        old = set(self.replenish_requested)
        self.replenish_queue = deque(urgent + background)
        self.replenish_requested = set(urgent + background)

        for sku in urgent:
            if sku not in old:
                self._emit("replenish_request", t=self.t, sku=sku, reason="stockout-urgent")
        for sku in background:
            if sku not in old:
                self._emit("replenish_request", t=self.t, sku=sku, reason="empty-pallet-background")

    def _assign_logistics_if_idle(self, rid: int) -> None:
        if rid not in self.logistics_ids:
            return
        job = self.jobs[rid]
        if job.repl_pid is not None:
            return

        urgent_skus, active_missing = self._urgent_replenishment()

        # If R4 already has a fulfillment order, only a true stockout may preempt it.
        if job.order_id is not None:
            urgent_in_queue = [sku for sku in self.replenish_queue if sku in urgent_skus]
            if not urgent_in_queue:
                return
            urgent_in_queue.sort(key=lambda sku: (-active_missing.get(sku, 0), sku))
            rest = [sku for sku in self.replenish_queue if sku not in urgent_skus]
            self.replenish_queue = deque(urgent_in_queue + rest)

        while self.replenish_queue:
            sku = self.replenish_queue.popleft()

            if job.order_id is not None and sku not in urgent_skus:
                self.replenish_queue.append(sku)
                return

            if not any(
                self.sim.pallets[pid].count == 0
                and self.sim.pallets[pid].owner is None
                and self.repl_pallet_cooldown.get((sku, pid), 0) <= self.t
                for pid in self.by_sku[sku]
            ):
                self.replenish_requested.discard(sku)
                continue

            if self._begin_replenish(rid, sku):
                self.logistics_sku[rid] = sku
                kind = "URGENT" if sku in urgent_skus else "background"
                print(
                    f"SOLVER logistics-assign: timestep={self.t} R{rid} -> "
                    f"SKU={sku} pallet={job.repl_pid} kind={kind}",
                    flush=True,
                )
                self._emit(
                    "logistics_assign", t=self.t, robot=rid, sku=sku,
                    pallet=job.repl_pid, urgency=kind,
                )
                return

            self.replenish_queue.append(sku)
            return

    def _valid_footprint(self, rid: int, rx: int, ry: int, start_cells: set[tuple[int, int]]) -> bool:
        r = self.sim.robots[rid]
        own = self._own_cells(rid)
        if not (0 <= rx < WIDTH and 0 <= ry < HEIGHT):
            return False
        cells = [(rx, ry)]
        dx, dy = rx - r.x, ry - r.y
        for pid in r.docked:
            p = self.sim.pallets[pid]
            cells.append((p.x + dx, p.y + dy))
        if len(cells) != len(set(cells)):
            return False
        occ = self.sim.occupied(ignore_robot=rid, ignore_pallets=set(r.docked))
        for c in cells:
            if not (0 <= c[0] < WIDTH and 0 <= c[1] < HEIGHT):
                return False
            if c in occ:
                return False
            if c in start_cells and c not in own:
                return False
        return True

    def _build_soft_route_reservations(
        self, start_cells: set[tuple[int, int]], depth: int = 10
    ) -> None:
        """Reserve a short prefix of each robot's preferred static route.

        These are SOFT reservations: lower-priority robots may cross them when there
        is no useful alternative, but weighted path planning strongly prefers a
        different aisle.  Actual cell conflicts are still resolved by the strict
        priority layer.
        """
        self.soft_route_reservations: dict[tuple[int, int], set[int]] = defaultdict(set)
        self.soft_route_paths: dict[int, list[tuple[int, int]]] = {}

        for rid in sorted(range(len(self.sim.robots)), key=self._traffic_priority):
            goals = self._joint_movement_goals(rid, start_cells)
            if not goals:
                continue

            dmap, footprint = self._joint_static_dist(rid, goals)
            r = self.sim.robots[rid]
            pos = (r.x, r.y)
            if pos not in dmap:
                continue

            path: list[tuple[int, int]] = []
            cur = pos

            for _ in range(depth):
                cur_d = dmap.get(cur)
                if cur_d is None or cur_d <= 0:
                    break

                choices = []
                for pref, (dx, dy) in enumerate(DIRS):
                    np = (cur[0] + dx, cur[1] + dy)
                    nd = dmap.get(np)
                    if nd is None or nd >= cur_d:
                        continue
                    fp = footprint(np)
                    if fp is None:
                        continue
                    choices.append((nd, pref, np, fp))

                if not choices:
                    break

                choices.sort()
                _, _, nxt, fp = choices[0]
                path.append(nxt)
                for c in fp:
                    self.soft_route_reservations[c].add(rid)
                cur = nxt

            if path:
                self.soft_route_paths[rid] = path

        if self.soft_route_paths:
            self._emit(
                "route_reservations",
                t=self.t,
                paths={
                    str(rid): [list(p) for p in path]
                    for rid, path in self.soft_route_paths.items()
                },
            )

    def _route_reservation_penalty(
        self, rid: int, footprint: Iterable[tuple[int, int]]
    ) -> int:
        """Penalty for occupying a higher-priority robot's reserved future route."""
        reservations = getattr(self, "soft_route_reservations", {})
        penalty = 0
        for c in footprint:
            for owner in reservations.get(c, ()):
                if owner != rid and self._higher_priority(owner, rid):
                    # Large enough to prefer a several-cell detour, but still soft.
                    penalty += 8
        return penalty

    def _next_step(self, rid: int, goals: set[tuple[int, int]], start_cells: set[tuple[int, int]]) -> tuple[int, int] | None:
        """Return one safe move toward a goal.

        Pallets are hard/static obstacles for planning. Other robots (and pallets docked
        to them) are *dynamic* obstacles: they cannot be entered on THIS timestep, but
        they are not treated as permanent walls for the whole BFS. This is essential in
        narrow aisles; otherwise a line of robots can mutually conclude that no route
        exists and deadlock forever.
        """
        r = self.sim.robots[rid]
        start = (r.x, r.y)
        if start in goals:
            return None

        attached = set(r.docked)
        own = self._own_cells(rid)

        # Relative footprint of this robot + its docked pallets.
        rel_cells = [(0, 0)]
        for pid in attached:
            p = self.sim.pallets[pid]
            rel_cells.append((p.x - r.x, p.y - r.y))

        # Hard obstacles are undocked pallets and pallets docked to OTHER robots.
        # Robot cells themselves are deliberately excluded from long-horizon BFS.
        hard = set()
        for p in self.sim.pallets:
            if p.id in attached:
                continue
            # A pallet attached to another robot is dynamic too, but its present cell
            # must still be respected by the immediate-step test below. Ignoring it in
            # the long-horizon search prevents temporary formations becoming walls.
            if p.owner is None:
                hard.add((p.x, p.y))

        def footprint_at(rx: int, ry: int) -> list[tuple[int, int]] | None:
            fp = []
            for ox, oy in rel_cells:
                c = (rx + ox, ry + oy)
                if not (0 <= c[0] < WIDTH and 0 <= c[1] < HEIGHT):
                    return None
                fp.append(c)
            if len(fp) != len(set(fp)):
                return None
            return fp

        def statically_valid(rx: int, ry: int) -> bool:
            fp = footprint_at(rx, ry)
            return fp is not None and not any(c in hard for c in fp)

        # Weighted reverse Dijkstra.  Static travel costs 1 per cell; cells reserved by
        # HIGHER-priority robots carry an additional soft penalty.  Therefore a lower
        # priority robot can deliberately take a slightly longer neighboring aisle
        # instead of tailgating the exact same sequence of cells.
        pq: list[tuple[int, int, int]] = []
        dist: dict[tuple[int, int], int] = {}
        for g in goals:
            if statically_valid(*g):
                dist[g] = 0
                heapq.heappush(pq, (0, g[0], g[1]))

        while pq:
            cur_cost, cx, cy = heapq.heappop(pq)
            cur = (cx, cy)
            if cur_cost != dist.get(cur):
                continue

            for dx, dy in DIRS:
                prev = (cx + dx, cy + dy)
                fp = footprint_at(*prev)
                if fp is None or not statically_valid(*prev):
                    continue

                step_cost = 1 + self._route_reservation_penalty(rid, fp)
                nd = cur_cost + step_cost
                if nd < dist.get(prev, 10**18):
                    dist[prev] = nd
                    heapq.heappush(pq, (nd, prev[0], prev[1]))

        # Pick the best CURRENTLY safe neighbor. start_cells includes every robot and
        # pallet position at the beginning of this timestep, so this forbids swaps and
        # entering a cell another entity is vacating in the same tick.
        static_candidates = []
        for pref, (dx, dy) in enumerate(DIRS):
            nxt = (start[0] + dx, start[1] + dy)
            fp = footprint_at(*nxt)
            if fp is None or nxt not in dist:
                continue
            static_candidates.append((dist[nxt], pref, nxt, fp))

        if not static_candidates:
            return None
        static_candidates.sort()

        # Absolute priority rule. Look first at weighted-cost-decreasing choices.
        # If the best route is occupied by a LOWER-priority robot, do not detour: ask
        # the master to move that robot aside. If occupied by a HIGHER-priority robot,
        # simply wait.
        cur_d = dist.get(start)
        if cur_d is None:
            return None
        for d, pref, nxt, fp in static_candidates:
            if d >= cur_d:
                continue
            blocked_cells = [c for c in fp if c in start_cells and c not in own]
            if not blocked_cells:
                return nxt
            for c in blocked_cells:
                blocker = self._dynamic_owner_at(c, ignore_rid=rid)
                if blocker is not None and self._higher_priority(rid, blocker):
                    self._request_clearance(blocker, rid, c)
                    return None
            # This improving choice is blocked by a higher-priority robot or a static
            # entity represented in start_cells. Try another equally/improving route.
        return None

    def _escape_step(self, rid: int, start_cells: set[tuple[int, int]]) -> tuple[int, int] | None:
        """One collision-safe yield step used only to break a global traffic deadlock.

        Prefer stepping perpendicular to the robot->target direction so a head-on pair
        leaves the aisle instead of simply reversing along it.
        """
        r = self.sim.robots[rid]
        job = self.jobs[rid]
        tx = ty = None
        if job.target_pid is not None:
            p = self.sim.pallets[job.target_pid]
            tx, ty = p.x, p.y
        elif job.repl_pid is not None:
            p = self.sim.pallets[job.repl_pid]
            tx, ty = p.x, p.y

        if tx is not None and ty is not None:
            dx, dy = tx - r.x, ty - r.y
            if abs(dx) >= abs(dy):
                prefs = ((0, -1), (0, 1), (-1, 0), (1, 0))
            else:
                prefs = ((-1, 0), (1, 0), (0, -1), (0, 1))
        else:
            prefs = DIRS

        for dx, dy in prefs:
            nx, ny = r.x + dx, r.y + dy
            if self._valid_footprint(rid, nx, ny, start_cells):
                return (nx, ny)
        return None

    def _adjacent_goals(self, rid: int, p: Pallet, safe_dock: bool = False) -> set[tuple[int, int]]:
        r = self.sim.robots[rid]
        occ = self.sim.occupied(ignore_robot=rid, ignore_pallets=set(r.docked))
        if not safe_dock:
            rels = DIRS
        else:
            # Prefer docking from BELOW: pallet above robot => one-cell-wide vertical
            # replenishment train. Use side docking only if that cell is unavailable.
            below = (p.x, p.y + 1)
            if 0 <= below[0] < WIDTH and 0 <= below[1] < HEIGHT:
                if below == (r.x, r.y) or below not in occ:
                    return {below}
            rels = ((1, 0), (-1, 0))
        result = set()
        for rel in rels:
            cell = (p.x - rel[0], p.y - rel[1])
            if 0 <= cell[0] < WIDTH and 0 <= cell[1] < HEIGHT:
                if cell == (r.x, r.y) or cell not in occ:
                    result.add(cell)
        return result

    def _begin_replenish(self, rid: int, sku: int) -> bool:
        job = self.jobs[rid]
        r = self.sim.robots[rid]
        candidates = [self.sim.pallets[pid] for pid in self.by_sku[sku]
                      if self.sim.pallets[pid].owner is None
                      and self.sim.pallets[pid].count == 0
                      and self.repl_pallet_cooldown.get((sku, pid), 0) <= self.t]
        if not candidates:
            return False
        occ = self.sim.occupied(ignore_robot=rid, ignore_pallets=set(r.docked))
        def score(q):
            below = (q.x, q.y + 1)
            below_ok = (0 <= below[0] < WIDTH and 0 <= below[1] < HEIGHT and
                        (below == (r.x, r.y) or below not in occ))
            return (0 if below_ok else 1, abs(r.x-q.x) + abs(r.y-q.y), q.id)
        p = min(candidates, key=score)
        job.repl_pid = p.id
        job.repl_phase = "dock"
        job.repl_original = (p.x, p.y)
        job.repl_return_robot = None
        job.repl_stall_ticks = 0
        job.repl_phase_since = self.t
        return True

    def _abort_replenishment(self, rid: int, reason: str) -> None:
        job = self.jobs[rid]
        pid = job.repl_pid
        sku = self.logistics_sku.get(rid)
        # Safe automatic abort is only possible before docking.  Once attached, the
        # pallet is part of the robot footprint and must be recovered explicitly.
        if pid is None:
            return
        if job.repl_phase != "dock":
            return
        if sku is None:
            sku = self.sim.pallets[pid].sku
        self.repl_pallet_cooldown[(sku, pid)] = self.t + 120
        print(f"MASTER replenish-abort: timestep={self.t} R{rid} P{pid}/SKU{sku} phase=dock reason={reason}", flush=True)
        self._emit("replenish_abort", t=self.t, robot=rid, pallet=pid, sku=sku, phase="dock", reason=reason)
        job.repl_pid = None
        job.repl_phase = None
        job.repl_original = None
        job.repl_return_robot = None
        job.repl_stall_ticks = 0
        job.repl_phase_since = self.t
        self.logistics_sku[rid] = None
        # Keep the SKU requested and put it at the front so another pallet is selected
        # immediately rather than waiting behind unrelated replenishment work.
        self.replenish_requested.add(sku)
        try:
            self.replenish_queue.remove(sku)
        except ValueError:
            pass
        self.replenish_queue.appendleft(sku)

    def _replenish_action(self, rid: int, start_cells: set[tuple[int, int]]) -> tuple[str, int, int] | None:
        job = self.jobs[rid]
        r = self.sim.robots[rid]
        assert job.repl_pid is not None
        p = self.sim.pallets[job.repl_pid]

        if job.repl_phase == "dock":
            goals = self._adjacent_goals(rid, p, safe_dock=True)
            if (r.x, r.y) in goals and p.owner is None:
                rel = (p.x-r.x, p.y-r.y)
                job.repl_return_robot = (job.repl_original[0]-rel[0], job.repl_original[1]-rel[1])
                job.repl_phase = "bottom"
                job.repl_stall_ticks = 0
                job.repl_phase_since = self.t
                return ("dock", p.x, p.y)
            step = self._next_step(rid, goals, start_cells)
            return ("move", *step) if step else None

        if job.repl_phase == "bottom":
            if r.y == REPLENISH_Y:
                job.repl_phase = "return"
                job.repl_stall_ticks = 0
                job.repl_phase_since = self.t
            else:
                own = self._own_cells(rid)
                goals = {(x, REPLENISH_Y) for x in range(WIDTH)
                         if (x, REPLENISH_Y) not in start_cells or (x, REPLENISH_Y) in own}
                step = self._next_step(rid, goals, start_cells)
                return ("move", *step) if step else None

        if job.repl_phase == "return":
            goal = job.repl_return_robot
            assert goal is not None
            if (r.x, r.y) == goal:
                job.repl_phase = "undock"
                job.repl_stall_ticks = 0
                job.repl_phase_since = self.t
            else:
                step = self._next_step(rid, {goal}, start_cells)
                return ("move", *step) if step else None

        if job.repl_phase == "undock":
            job.repl_phase = None
            job.repl_pid = None
            job.repl_original = None
            job.repl_return_robot = None
            job.repl_stall_ticks = 0
            job.repl_phase_since = self.t
            return ("undock", p.x, p.y)
        return None

    def _missing_for_robot(self, rid: int) -> Counter[int]:
        job = self.jobs[rid]
        r = self.sim.robots[rid]
        missing: Counter[int] = Counter()
        if job.order_id is None:
            return missing
        for sku, qty in job.needed.items():
            have = r.storage[sku]
            if have < qty:
                missing[sku] = qty - have
        return missing

    def _master_drop_target(self, rid: int, reason: str, cooldown: bool = True) -> None:
        job = self.jobs[rid]
        pid = job.target_pid
        if pid is None:
            return
        if cooldown:
            self.target_pair_cooldown[(rid, pid)] = self.t + self.target_cooldown
        print(
            f"MASTER stop: timestep={self.t} R{rid} stop chasing P{pid}/SKU{job.target_sku} reason={reason}",
            flush=True,
        )
        self._emit("master_stop", t=self.t, robot=rid, pallet=pid, sku=job.target_sku, reason=reason)
        job.target_pid = None
        job.target_sku = None
        self.target_since[rid] = self.t
        self.blocked_ticks[rid] = 0

    def _master_allocate_targets(self) -> None:
        """Central authority for fulfillment pallet targets.

        Constraints enforced here:
          * at most one target pallet per robot
          * at most one pursuing robot per pallet
          * target SKU must still be missing from that robot's current order
          * pallet must have stock and not be docked
          * a robot/pallet pair recently revoked by the master is temporarily forbidden

        Robots NEVER select their own pallet in _normal_action().  They only execute
        assignments made here.  This gives one place that can explicitly say STOP.
        """
        # Remove expired cooldown entries.
        self.target_pair_cooldown = {
            k: until for k, until in self.target_pair_cooldown.items() if until > self.t
        }

        # First validate/revoke existing assignments.
        used: set[int] = set()
        for rid in range(len(self.sim.robots)):
            job = self.jobs[rid]
            if job.order_id is None or job.target_pid is None:
                continue
            pid = job.target_pid
            sku = job.target_sku
            missing = self._missing_for_robot(rid)
            p = self.sim.pallets[pid]
            reason = None
            if sku is None or sku not in missing:
                reason = "sku-satisfied"
            elif p.sku != sku or p.count <= 0 or p.owner is not None:
                reason = "target-invalid"
            elif pid in used:
                reason = "duplicate-reservation"
            elif self.blocked_ticks[rid] >= self.master_stop_after:
                reason = f"blocked-{self.blocked_ticks[rid]}"
            if reason is not None:
                self._master_drop_target(rid, reason, cooldown=reason.startswith("blocked") or reason == "duplicate-reservation")
            else:
                used.add(pid)

        # Build candidate edges for robots that need a target.  This is the small
        # centralized assignment problem: robot -> pallet with exclusivity constraints.
        edges: list[tuple[float, int, int, int]] = []
        waiting: list[int] = []
        for rid in range(len(self.sim.robots)):
            job = self.jobs[rid]
            if job.order_id is None or job.target_pid is not None:
                continue
            missing = self._missing_for_robot(rid)
            if not missing:
                continue
            r = self.sim.robots[rid]
            any_stock = False
            for sku, qty in missing.items():
                for pid in self.by_sku[sku]:
                    p = self.sim.pallets[pid]
                    if p.count <= 0 or p.owner is not None:
                        continue
                    any_stock = True
                    if pid in used:
                        continue
                    if self.target_pair_cooldown.get((rid, pid), 0) > self.t:
                        continue
                    d = abs(r.x - p.x) + abs(r.y - p.y)
                    # Prefer larger outstanding quantity very slightly; distance dominates.
                    cost = float(d) - 0.05 * min(qty, 10)
                    edges.append((cost, rid, sku, pid))
            if not any_stock:
                waiting.append(rid)

        # Global greedy matching over ALL robot-pallet edges.  With only 3-5 robots this
        # is deterministic, cheap, and importantly centralized rather than robot-local.
        assigned: set[int] = set()
        for cost, rid, sku, pid in sorted(edges, key=lambda x: (x[0], x[1], x[3])):
            if rid in assigned or self.jobs[rid].target_pid is not None or pid in used:
                continue
            self.jobs[rid].target_pid = pid
            self.jobs[rid].target_sku = sku
            self.target_since[rid] = self.t
            used.add(pid)
            assigned.add(rid)
            print(
                f"MASTER assign: timestep={self.t} R{rid} -> P{pid}/SKU{sku} cost={cost:.2f}",
                flush=True,
            )
            self._emit("master_assign", t=self.t, robot=rid, pallet=pid, sku=sku, cost=cost)

        # No stock at all for a missing SKU: this is a logistics problem, not traffic.
        for rid in waiting:
            missing = self._missing_for_robot(rid)
            if not missing:
                continue
            # Pick a missing SKU for which every pallet is empty/unavailable.
            candidates = []
            for sku, qty in missing.items():
                if not any(self.sim.pallets[pid].count > 0 and self.sim.pallets[pid].owner is None
                           for pid in self.by_sku[sku]):
                    candidates.append((qty, sku))
            if candidates:
                _, sku = max(candidates)
                if self.logistics_ids:
                    self._request_replenishment(sku)
                elif self.jobs[rid].repl_pid is None:
                    self._begin_replenish(rid, sku)

    def _master_recover_unassigned_orders(self) -> None:
        """Enforce the resource-assignment invariant for every unfinished order.

        An active order that still needs items may not be left in a fourth, implicit
        state where it has no pallet target and no replenishment plan.  The master must
        either assign a currently usable pallet or create a replenishment request.

        This recovery deliberately ignores robot/pallet cooldowns when they are the only
        reason progress stopped.  Cooldowns are hints against thrashing, not hard
        constraints allowed to deadlock the warehouse.
        """
        used = {j.target_pid for j in self.jobs if j.target_pid is not None}
        for rid, job in enumerate(self.jobs):
            if job.order_id is None or job.repl_pid is not None or job.target_pid is not None:
                continue
            missing = self._missing_for_robot(rid)
            if not missing:
                continue
            r = self.sim.robots[rid]

            # First find any free, stocked, unreserved pallet for a still-missing SKU.
            candidates = []
            for sku, qty in missing.items():
                for pid in self.by_sku[sku]:
                    p = self.sim.pallets[pid]
                    if p.count <= 0 or p.owner is not None or pid in used:
                        continue
                    d = abs(r.x - p.x) + abs(r.y - p.y)
                    candidates.append((d, -min(qty, 10), sku, pid))
            if candidates:
                _, _, sku, pid = min(candidates)
                job.target_pid = pid
                job.target_sku = sku
                self.resource_park_target[rid] = None
                self.target_since[rid] = self.t
                used.add(pid)
                self.target_pair_cooldown.pop((rid, pid), None)
                print(f"MASTER recover: timestep={self.t} R{rid} -> P{pid}/SKU{sku} reason=no-resource-state", flush=True)
                self._emit('master_recover_assign', t=self.t, robot=rid, pallet=pid, sku=sku)
                self.blocked_ticks[rid] = 0
                continue

            # No free stocked pallet exists for any missing SKU.  Queue one genuinely
            # depleted/unavailable SKU for logistics.  A logistics robot may preempt its
            # fallback fulfillment order to service this request.
            depleted = []
            for sku, qty in missing.items():
                free_stock = any(self.sim.pallets[pid].count > 0 and self.sim.pallets[pid].owner is None
                                 for pid in self.by_sku[sku])
                if not free_stock:
                    depleted.append((qty, sku))
            if depleted:
                _, sku = max(depleted)
                if self.logistics_ids:
                    self._request_replenishment(sku)
                    self._emit('master_wait_resource', t=self.t, robot=rid, sku=sku, reason='await-replenishment')
                elif job.repl_pid is None:
                    self._begin_replenish(rid, sku)

    def _resource_waiting(self, rid: int) -> bool:
        job = self.jobs[rid]
        return (
            job.order_id is not None
            and job.repl_pid is None
            and job.target_pid is None
            and bool(self._missing_for_robot(rid))
        )

    def _static_open_cell(self, x: int, y: int) -> bool:
        if not (1 <= x < WIDTH - 1 and 1 <= y < HEIGHT - 1):
            return False
        # Parking never uses fulfillment/replenishment rows.
        if y in (FULFILL_Y, REPLENISH_Y):
            return False
        # Keep parking away from every undocked pallet and its immediate pick/dock
        # neighborhood. That is the key difference from freezing in an active aisle.
        for p in self.sim.pallets:
            if p.owner is not None:
                continue
            if abs(p.x - x) + abs(p.y - y) <= 1:
                return False
        return True

    def _choose_resource_park(self, rid: int) -> tuple[int, int] | None:
        """Choose a roomy parking bay outside pallet-working lanes.

        Score prefers cells near the outer sides of the warehouse, away from pallets,
        away from other parking targets, and not excessively far from the robot.
        """
        r = self.sim.robots[rid]
        claimed = {
            p for rr, p in self.resource_park_target.items()
            if rr != rid and p is not None
        }
        occupied = self.sim.occupied(ignore_robot=rid)

        candidates = []
        for y in range(2, HEIGHT - 2):
            for x in range(1, WIDTH - 1):
                if (x, y) in claimed or (x, y) in occupied:
                    continue
                if not self._static_open_cell(x, y):
                    continue

                # Require local breathing room: at least three free cardinal neighbors.
                free_nb = 0
                for dx, dy in ((1,0),(-1,0),(0,1),(0,-1)):
                    nx, ny = x + dx, y + dy
                    if not (0 <= nx < WIDTH and 0 <= ny < HEIGHT):
                        continue
                    if (nx, ny) in occupied:
                        continue
                    if any(p.owner is None and (p.x, p.y) == (nx, ny)
                           for p in self.sim.pallets):
                        continue
                    free_nb += 1
                if free_nb < 3:
                    continue

                nearest_pallet = min(
                    (abs(p.x - x) + abs(p.y - y)
                     for p in self.sim.pallets if p.owner is None),
                    default=99,
                )
                side_dist = min(x, WIDTH - 1 - x)
                travel = abs(r.x - x) + abs(r.y - y)

                # Lower score is better. Favor side/open areas and pallet clearance,
                # with travel distance only a secondary consideration.
                score = (
                    side_dist,
                    -min(nearest_pallet, 8),
                    travel,
                    y,
                    x,
                )
                candidates.append((score, (x, y)))

        if not candidates:
            return None
        candidates.sort()
        return candidates[0][1]

    def _resource_park_action(
        self, rid: int, start_cells: set[tuple[int, int]]
    ) -> tuple[str, int, int] | None:
        if not self._resource_waiting(rid):
            self.resource_park_target[rid] = None
            return None

        r = self.sim.robots[rid]
        goal = self.resource_park_target.get(rid)

        # Replan if no target exists or the old parking cell ceased to be suitable.
        if goal is None or not self._static_open_cell(*goal):
            goal = self._choose_resource_park(rid)
            self.resource_park_target[rid] = goal
            if goal is not None:
                print(
                    f"MASTER park: timestep={self.t} R{rid} resource-wait -> {goal}",
                    flush=True,
                )
                self._emit("resource_park_assign", t=self.t, robot=rid,
                           x=goal[0], y=goal[1])

        if goal is None or (r.x, r.y) == goal:
            return None

        step = self._next_step(rid, {goal}, start_cells)
        if step is None:
            # Do not keep a bad parking target forever.
            self.resource_park_target[rid] = None
            return None
        return ("move", step[0], step[1])


    def _joint_movement_goals(
        self, rid: int, start_cells: set[tuple[int, int]]
    ) -> set[tuple[int, int]] | None:
        """Return the current movement goal set without choosing a next step.

        This is used only by the centralized local MAPF planner.  Non-movement
        actions (pick/dock/undock/fulfill) return None and remain handled by the
        normal solver.
        """
        job = self.jobs[rid]
        r = self.sim.robots[rid]

        if job.repl_pid is not None:
            p = self.sim.pallets[job.repl_pid]
            if job.repl_phase == "dock":
                goals = self._adjacent_goals(rid, p, safe_dock=True)
                return None if (r.x, r.y) in goals else goals
            if job.repl_phase == "bottom":
                if r.y == REPLENISH_Y:
                    return None
                return {(x, REPLENISH_Y) for x in range(WIDTH)}
            if job.repl_phase == "return":
                if job.repl_return_robot is None:
                    return None
                goal = {job.repl_return_robot}
                return None if (r.x, r.y) in goal else goal
            return None

        if self._resource_waiting(rid):
            goal = self.resource_park_target.get(rid)
            if goal is None:
                goal = self._choose_resource_park(rid)
                self.resource_park_target[rid] = goal
            if goal is None or (r.x, r.y) == goal:
                return None
            return {goal}

        if job.order_id is None:
            return None

        missing = self._missing_for_robot(rid)
        if not missing:
            if r.y == FULFILL_Y:
                return None
            return {(x, FULFILL_Y) for x in range(WIDTH)}

        if job.target_pid is None or job.target_sku is None:
            return None
        p = self.sim.pallets[job.target_pid]
        if p.owner is not None or p.count <= 0:
            return None
        goals = self._adjacent_goals(rid, p)
        return None if (r.x, r.y) in goals else goals

    def _joint_static_dist(
        self, rid: int, goals: set[tuple[int, int]]
    ) -> tuple[dict[tuple[int, int], int], callable]:
        """Static distance map and footprint function for one robot.

        Other robots are intentionally NOT hard obstacles here.  They are handled
        jointly in the time-expanded search.
        """
        r = self.sim.robots[rid]
        attached = set(r.docked)
        rel = [(0, 0)]
        for pid in attached:
            p = self.sim.pallets[pid]
            rel.append((p.x - r.x, p.y - r.y))

        hard = {
            (p.x, p.y)
            for p in self.sim.pallets
            if p.id not in attached and p.owner is None
        }

        def footprint(pos: tuple[int, int]) -> tuple[tuple[int, int], ...] | None:
            rx, ry = pos
            fp = tuple((rx + ox, ry + oy) for ox, oy in rel)
            if len(fp) != len(set(fp)):
                return None
            for x, y in fp:
                if not (0 <= x < WIDTH and 0 <= y < HEIGHT):
                    return None
                if (x, y) in hard:
                    return None
            return fp

        q = deque()
        dist: dict[tuple[int, int], int] = {}
        for g in goals:
            if footprint(g) is not None:
                dist[g] = 0
                q.append(g)
        while q:
            x, y = q.popleft()
            nd = dist[(x, y)] + 1
            for dx, dy in DIRS:
                p = (x + dx, y + dy)
                if p in dist or footprint(p) is None:
                    continue
                dist[p] = nd
                q.append(p)
        return dist, footprint

    def _strict_priority_clear_plan(
        self, start_cells: set[tuple[int, int]]
    ) -> dict[int, tuple[int, int]]:
        """Give absolute right-of-way to the highest-priority directly blocked robot.

        Unlike the beam planner, this rule is not an optimization preference.  It is
        a hard traffic decision:

            higher-priority robot wants cell C
            lower-priority robot occupies C
                => higher WAITS
                => lower MUST vacate if a safe adjacent cell exists

        The desired cell is computed from the static shortest-path map, intentionally
        ignoring robots.  This lets us see the actual blocker instead of having
        _next_step() return None before the master knows why.
        """
        moving = []
        info = {}

        for rid in range(len(self.sim.robots)):
            goals = self._joint_movement_goals(rid, start_cells)
            if not goals:
                continue

            dmap, footprint = self._joint_static_dist(rid, goals)
            r = self.sim.robots[rid]
            pos = (r.x, r.y)
            curd = dmap.get(pos)
            if curd is None or curd <= 0:
                continue

            # Best strictly distance-decreasing static next steps.
            best = []
            bestd = curd
            for pref, (dx, dy) in enumerate(DIRS):
                np = (pos[0] + dx, pos[1] + dy)
                nd = dmap.get(np)
                if nd is None or nd >= bestd:
                    continue
                fp = footprint(np)
                if fp is None:
                    continue
                if nd < bestd:
                    bestd = nd
                    best = []
                best.append((pref, np, set(fp)))

            if not best:
                continue

            best.sort(key=lambda z: z[0])
            moving.append(rid)
            info[rid] = (dmap, footprint, best)

        if not moving:
            return {}

        # Current dynamic ownership, including docked pallets.
        owner_at = {}
        for r in self.sim.robots:
            for c in self._own_cells(r.id):
                owner_at[c] = r.id

        # Highest priority blocked robot wins the arbitration.
        for high in sorted(moving, key=self._traffic_priority):
            _dmap, _fp, best = info[high]

            blocker = None
            contested = None
            desired = None

            # Use the first best step whose obstruction is a lower-priority robot.
            for _pref, np, fp_cells in best:
                blockers = {
                    owner_at[c]
                    for c in fp_cells
                    if c in owner_at and owner_at[c] != high
                }
                blockers = {
                    b for b in blockers
                    if self._higher_priority(high, b)
                }
                if blockers:
                    blocker = min(blockers, key=self._traffic_priority)
                    contested = next(
                        c for c in fp_cells
                        if owner_at.get(c) == blocker
                    )
                    desired = np
                    break

            if blocker is None:
                continue

            br = self.sim.robots[blocker]
            bpos = (br.x, br.y)

            # Cells occupied by everybody except the blocker.
            other_cells = set()
            for rr in self.sim.robots:
                if rr.id != blocker:
                    other_cells |= self._own_cells(rr.id)

            # Pick one safe adjacent clear move.  It is allowed to increase the
            # blocker's own route distance: clearing the winner is the objective.
            candidates = []
            for pref, (dx, dy) in enumerate(DIRS):
                np = (bpos[0] + dx, bpos[1] + dy)
                if not self._valid_footprint(blocker, np[0], np[1], start_cells):
                    continue

                future = set(self._proposed_move_footprint(blocker, np[0], np[1]))

                # Do not remain in or move into the winner's contested footprint.
                high_future = set(self._proposed_move_footprint(high, desired[0], desired[1]))
                if future & high_future:
                    continue
                if future & other_cells:
                    continue

                # Prefer increasing separation from contested cell and from winner.
                old_cont = abs(bpos[0] - contested[0]) + abs(bpos[1] - contested[1])
                new_cont = abs(np[0] - contested[0]) + abs(np[1] - contested[1])
                hr = self.sim.robots[high]
                old_sep = abs(bpos[0] - hr.x) + abs(bpos[1] - hr.y)
                new_sep = abs(np[0] - hr.x) + abs(np[1] - hr.y)

                score = (
                    0 if new_cont > old_cont else 1,
                    0 if new_sep > old_sep else 1,
                    pref,
                )
                candidates.append((score, np))

            if not candidates:
                # No one-step escape. Let the normal joint MAPF attempt a multi-step
                # resolution; importantly we do NOT invert priority here.
                self._emit(
                    "strict_priority_blocked",
                    t=self.t, winner=high, blocker=blocker,
                    cell=list(contested),
                )
                continue

            candidates.sort()
            clear_to = candidates[0][1]

            print(
                f"MASTER PRIORITY: timestep={self.t} R{high} blocked by R{blocker} "
                f"at {contested}; R{blocker} CLEAR -> {clear_to}; R{high} WAIT",
                flush=True,
            )
            self._emit(
                "strict_priority_clear",
                t=self.t,
                winner=high,
                blocker=blocker,
                contested=list(contested),
                clear_to=list(clear_to),
            )

            # Explicitly command both members of the conflict.
            return {
                high: (self.sim.robots[high].x, self.sim.robots[high].y),
                blocker: clear_to,
            }

        return {}

    def _joint_local_plan(
        self, start_cells: set[tuple[int, int]], horizon: int = 6, beam_width: int = 120
    ) -> dict[int, tuple[int, int]]:
        """Small centralized MAPF search for robots that are already close.

        The previous traffic patches were reactive.  This planner reasons several
        timesteps ahead.  If R0's best route is occupied by R2, a branch where R2
        temporarily moves away scores better because it removes R0's blockage and
        lets R0 advance on the following simulated step.

        Only nearby moving robots are included, so the search stays tiny (normally
        2-3 agents).  We execute only the first step and replan next timestep.
        """
        goals: dict[int, set[tuple[int, int]]] = {}
        distmaps = {}
        footprints = {}
        movers = []
        for rid in range(len(self.sim.robots)):
            g = self._joint_movement_goals(rid, start_cells)
            if not g:
                continue
            dmap, fp = self._joint_static_dist(rid, g)
            pos = (self.sim.robots[rid].x, self.sim.robots[rid].y)
            if pos not in dmap:
                continue
            goals[rid] = g
            distmaps[rid] = dmap
            footprints[rid] = fp
            movers.append(rid)

        if len(movers) < 2:
            return {}

        # Connected proximity components.  We intervene only where robots are already
        # close enough to create a real traffic interaction.
        adj = {rid: set() for rid in movers}
        for i, a in enumerate(movers):
            ra = self.sim.robots[a]
            for b in movers[i + 1:]:
                rb = self.sim.robots[b]
                if abs(ra.x - rb.x) + abs(ra.y - rb.y) <= 2:
                    adj[a].add(b)
                    adj[b].add(a)

        components = []
        seen = set()
        for seed in movers:
            if seed in seen or not adj[seed]:
                continue
            stack = [seed]
            comp = []
            while stack:
                u = stack.pop()
                if u in seen:
                    continue
                seen.add(u)
                comp.append(u)
                stack.extend(adj[u] - seen)
            if len(comp) >= 2:
                components.append(comp)

        if not components:
            return {}

        result: dict[int, tuple[int, int]] = {}
        all_current_fp = {
            rid: set(self._own_cells(rid)) for rid in range(len(self.sim.robots))
        }

        for comp in components:
            comp = sorted(comp, key=self._traffic_priority)
            fixed = set()
            for rid in range(len(self.sim.robots)):
                if rid not in comp:
                    fixed |= all_current_fp[rid]

            start_pos = tuple(
                (self.sim.robots[rid].x, self.sim.robots[rid].y) for rid in comp
            )

            # Cache candidate neighbors for a robot/position.  Keep WAIT plus all
            # statically valid neighbors; component size is normally 2-3.
            cand_cache = {}
            def candidates(rid: int, pos: tuple[int, int]):
                key = (rid, pos)
                if key in cand_cache:
                    return cand_cache[key]
                dmap = distmaps[rid]
                vals = [pos]  # WAIT is always available in the search model.
                neigh = []
                for pref, (dx, dy) in enumerate(DIRS):
                    np = (pos[0] + dx, pos[1] + dy)
                    if np not in dmap:
                        continue
                    if footprints[rid](np) is None:
                        continue
                    neigh.append((dmap[np], pref, np))
                neigh.sort()
                # Include every direction for 2-agent conflicts; cap to 4 moves naturally.
                vals.extend(v[2] for v in neigh)
                cand_cache[key] = vals
                return vals

            def state_score(state):
                # Lexicographic role priority.  For each robot, being immediately
                # blocked on its best static step is penalized BEFORE raw distance.
                occupied_by = {}
                fps = {}
                for j, rid in enumerate(comp):
                    fp = set(footprints[rid](state[j]) or ())
                    fps[rid] = fp
                    for c in fp:
                        occupied_by[c] = rid

                score = []
                for j, rid in enumerate(comp):
                    pos = state[j]
                    dmap = distmaps[rid]
                    curd = dmap.get(pos, 10**6)
                    blocked = 0
                    if curd > 0:
                        bestd = curd
                        bestfps = []
                        for dx, dy in DIRS:
                            np = (pos[0] + dx, pos[1] + dy)
                            nd = dmap.get(np)
                            if nd is None or nd >= bestd:
                                continue
                            fp = footprints[rid](np)
                            if fp is None:
                                continue
                            bestd = nd
                            bestfps = [set(fp)]
                        if bestfps:
                            for fp in bestfps:
                                if any(
                                    c in occupied_by and occupied_by[c] != rid
                                    for c in fp
                                ):
                                    blocked = 1
                                    break
                    reservation_cost = self._route_reservation_penalty(
                        rid, footprints[rid](pos) or ()
                    )
                    score.extend((blocked, reservation_cost, curd))
                # Mild total-distance tie breaker.
                score.append(sum(distmaps[rid].get(state[j], 10**6)
                                 for j, rid in enumerate(comp)))
                return tuple(score)

            # Beam entries: (state, first_state).
            beam = [(start_pos, None)]
            for depth in range(horizon):
                nxt_entries = {}
                for state, first_state in beam:
                    choices = [candidates(rid, state[j]) for j, rid in enumerate(comp)]

                    # Recursive Cartesian product with early collision pruning.
                    partial = [None] * len(comp)
                    future_fps = [None] * len(comp)

                    def rec(k):
                        if k == len(comp):
                            ns = tuple(partial)
                            fs = ns if first_state is None else first_state
                            old = nxt_entries.get(ns)
                            if old is None:
                                nxt_entries[ns] = fs
                            return
                        rid = comp[k]
                        cur_fp_other = set()
                        for m, orid in enumerate(comp):
                            if m != k:
                                cur_fp_other |= set(footprints[orid](state[m]) or ())
                        for np in choices[k]:
                            fp = set(footprints[rid](np) or ())
                            if not fp or fp & fixed:
                                continue
                            # Conservative challenge rule: cannot enter any other
                            # entity's cell from the start of this simulated timestep.
                            if fp & cur_fp_other:
                                continue
                            # Nor can two future footprints overlap.
                            if any(fp & future_fps[m] for m in range(k)
                                   if future_fps[m] is not None):
                                continue
                            partial[k] = np
                            future_fps[k] = fp
                            rec(k + 1)
                            future_fps[k] = None

                    rec(0)

                ranked = sorted(
                    ((state_score(s), s, fs) for s, fs in nxt_entries.items()),
                    key=lambda x: x[0],
                )
                beam = [(s, fs) for _, s, fs in ranked[:beam_width]]
                if not beam:
                    break

            if not beam:
                continue
            best_state, first_state = min(beam, key=lambda z: state_score(z[0]))
            if first_state is None:
                continue

            changed = False
            for j, rid in enumerate(comp):
                result[rid] = first_state[j]
                if first_state[j] != start_pos[j]:
                    changed = True

            if changed:
                print(
                    f"MASTER joint-plan: timestep={self.t} robots={comp} "
                    f"first={{{', '.join(f'R{rid}:{result[rid]}' for rid in comp)}}}",
                    flush=True,
                )
                self._emit(
                    "joint_plan", t=self.t, robots=comp,
                    moves={str(rid): list(result[rid]) for rid in comp},
                )

        return result

    def _normal_action(self, rid: int, start_cells: set[tuple[int, int]]) -> tuple[str, int, int] | None:
        job = self.jobs[rid]
        r = self.sim.robots[rid]
        if job.order_id is None:
            return None

        missing = self._missing_for_robot(rid)
        if not missing:
            if job.target_pid is not None:
                self._master_drop_target(rid, "order-ready", cooldown=False)
            if r.y == FULFILL_Y:
                return ("fulfill", 0, 0)
            own = self._own_cells(rid)
            goals = {(x, FULFILL_Y) for x in range(WIDTH)
                     if (x, FULFILL_Y) not in start_cells or (x, FULFILL_Y) in own}
            step = self._next_step(rid, goals, start_cells)
            return ("move", *step) if step else None

        # IMPORTANT: no local target selection here.  The master owns target_pid/sku.
        pid = job.target_pid
        sku = job.target_sku
        if pid is None or sku is None:
            return None
        p = self.sim.pallets[pid]
        if sku not in missing or p.sku != sku or p.count <= 0 or p.owner is not None:
            # Master will revoke/reassign at the beginning of the next timestep.
            return None
        if abs(r.x-p.x) + abs(r.y-p.y) == 1:
            return ("pick", p.x, p.y)
        goals = self._adjacent_goals(rid, p)
        step = self._next_step(rid, goals, start_cells)
        return ("move", *step) if step else None

    def _idle_logistics_action(self, rid: int, start_cells: set[tuple[int, int]]) -> tuple[str, int, int] | None:
        """Park idle logistics robots near the replenishment side, out of pick aisles."""
        if rid not in self.logistics_ids:
            return None
        r = self.sim.robots[rid]
        # Spread multiple logistics robots across row 38. Keep them off y=39 so a
        # replenishing robot can still use the actual refill row freely.
        lids = sorted(self.logistics_ids)
        slot = lids.index(rid)
        if len(lids) == 1:
            px = WIDTH // 2
        else:
            px = 4 + round(slot * (WIDTH - 9) / (len(lids) - 1))
        goal = (px, REPLENISH_Y - 1)
        if (r.x, r.y) == goal:
            return None
        step = self._next_step(rid, {goal}, start_cells)
        return ("move", *step) if step else None

    def solve(self, progress_every: int = 25, heartbeat_every: int = 250) -> list[Action]:
        total = len(self.w.orders)
        stagnant = 0
        last_completion_t = 0
        last_completed_jobs = 0
        max_ticks = 2_000_000
        print(
            f"SOLVER start: {len(self.sim.robots)} robots, {total} orders; "
            f"fulfillment={self.fulfillment_ids} logistics={sorted(self.logistics_ids)} "
            f"lookahead={self.order_lookahead} yield_after={self.yield_after} "
            f"master_stop_after={self.master_stop_after} target_cooldown={self.target_cooldown} "
            f"priority=R4(replenish)>R0>R1>R2>R3>R4(fulfillment) resource-parking+urgent-replenishment+strict-priority+soft-route-reservation+joint-local-mapf",
            flush=True,
        )
        self._emit("start", t=self.t, robots=len(self.sim.robots), orders=total,
                   fulfillment=self.fulfillment_ids, logistics=sorted(self.logistics_ids),
                   lookahead=self.order_lookahead)
        while self.completed_jobs < total:
            # Keep logistics work-conserving: continuously discover every empty pallet.
            # Active-order shortages are prioritized; background empties are still done.
            self._refresh_replenishment_queue()
            # Dedicated logistics robots get first chance at replenishment.
            for rid in sorted(self.logistics_ids):
                self._assign_logistics_if_idle(rid)
            # Then assign fulfillment.  Idle logistics robots borrow fulfillment work
            # when their replenishment queue is empty, so R4 is never wasted.
            for rid in range(len(self.sim.robots)):
                self._assign_if_idle(rid)

            # One centralized authority decides who is allowed to chase which pallet.
            self._master_allocate_targets()
            # Hard resource invariant: an unfinished order may never drift around with
            # no target and no replenishment plan.  Recover immediately rather than
            # waiting for the 5000-tick livelock watchdog.
            self._master_recover_unassigned_orders()
            # A recovery call may have queued replenishment while R4 is currently doing
            # fallback fulfillment. Allow logistics to preempt that collection now.
            for _rid in sorted(self.logistics_ids):
                self._assign_logistics_if_idle(_rid)
            # Persistent right-of-way holds survive across timesteps, but only for the
            # local crossing. A lower-priority robot resumes once the higher-priority
            # IMPORTANT: proximity alone is not a traffic conflict.  The previous
            # crowd normalizer created holds for robots that merely happened to be
            # within three cells of one another, which could freeze an entire aisle.
            # Right-of-way is now created only by an ACTUAL attempted footprint/path
            # conflict in _next_step() or the same-timestep reservation layer.
            # Traffic state is deliberately memoryless.  Stale persistent holds from
            # earlier policies are not allowed to influence this timestep.
            self.priority_holds.clear()
            self.yield_targets.clear()

            start_cells = self._start_cells()
            did_any = False
            self.clearance_requests.clear()
            # Role-aware master right-of-way:
            # R4 replenishment > R0 > R1 > R2 > R3 > R4 fulfillment fallback.
            # Evaluate higher-priority robots first so their reservations and clearance
            # requests dominate lower-priority proposals.
            order = sorted(range(len(self.sim.robots)), key=self._traffic_priority)
            reserved_next: dict[tuple[int, int], int] = {}

            # Build short future route reservations before traffic arbitration.
            # Higher-priority routes are SOFT obstacles to lower-priority planning:
            # take another aisle when practical, but never declare the route impossible.
            self._build_soft_route_reservations(start_cells, depth=10)

            # HARD right-of-way first.  If a higher-priority robot's static shortest
            # path is directly occupied by a lower-priority robot, the lower robot must
            # clear and the winner waits.  Only when there is no such direct conflict do
            # we use the softer short-horizon joint MAPF optimizer.
            joint_plan = self._strict_priority_clear_plan(start_cells)
            if not joint_plan:
                joint_plan = self._joint_local_plan(start_cells)

            for rid in order:
                if rid in joint_plan:
                    nx, ny = joint_plan[rid]
                    rj = self.sim.robots[rid]
                    if (nx, ny) == (rj.x, rj.y):
                        self._master_wait(rid, "joint-plan-wait")
                        continue
                    a = Action(self.t, rid, "move", nx, ny)
                    try:
                        self.sim.apply(a)
                    except ValidationError:
                        self._master_wait(rid, "joint-plan-rejected")
                        continue
                    self._record_action(a)
                    did_any = True
                    self.blocked_ticks[rid] = 0
                    self.traffic_wait_ticks[rid] = 0
                    for c in self._own_cells(rid):
                        reserved_next[c] = rid
                    self._emit("joint_move", t=self.t, robot=rid, x=nx, y=ny)
                    continue
                # No persistent traffic hold: every timestep is solved from the
                # current positions and current one-step reservations only.
                # A higher-priority robot may already have requested that this robot
                # vacate a cell this tick. Clearing that path overrides its own work.
                if rid in self.clearance_requests:
                    priority_rid, blocked_cell = self.clearance_requests[rid]
                    step = self._clearance_step(rid, priority_rid, blocked_cell, start_cells, reserved_next)
                    if step is None:
                        self._master_wait(rid, "cannot-clear-priority-path", priority_rid)
                        continue
                    a = Action(self.t, rid, "move", step[0], step[1])
                    try:
                        self.sim.apply(a)
                    except ValidationError:
                        self._master_wait(rid, "cannot-clear-priority-path", priority_rid)
                        continue
                    self._record_action(a)
                    did_any = True
                    self.blocked_ticks[rid] = 0
                    self.traffic_wait_ticks[rid] = 0
                    for c in self._own_cells(rid):
                        reserved_next[c] = rid
                    self._emit("master_clear_move", t=self.t, robot=rid, for_robot=priority_rid,
                               x=step[0], y=step[1])
                    continue
                job = self.jobs[rid]
                job_backup = copy.deepcopy(job)
                if job.repl_pid is not None:
                    self.resource_park_target[rid] = None
                    spec = self._replenish_action(rid, start_cells)
                elif self._resource_waiting(rid):
                    # Resource wait is an ACTIVE PARKING command, not a frozen state.
                    # It remains lowest traffic priority via _priority_rank().
                    spec = self._resource_park_action(rid, start_cells)
                elif job.order_id is not None:
                    self.resource_park_target[rid] = None
                    spec = self._normal_action(rid, start_cells)
                elif rid in self.logistics_ids:
                    spec = self._idle_logistics_action(rid, start_cells)
                else:
                    spec = None
                # Parking is useful movement, but an idle logistics robot should not
                # accumulate blocked/yield state merely because its parking route is busy.
                active = (job.order_id is not None or job.repl_pid is not None)
                if spec is None:
                    if active:
                        # Replenishment has its own watchdog.  A dock target that cannot
                        # produce any action is not a traffic problem: abandon that pallet
                        # and select another empty pallet for the same SKU.
                        if job.repl_pid is not None:
                            job.repl_stall_ticks += 1
                            limit = self.repl_dock_stall_limit if job.repl_phase == "dock" else self.repl_phase_stall_limit
                            if job.repl_stall_ticks >= limit:
                                if job.repl_phase == "dock":
                                    self._abort_replenishment(rid, f"no-progress-{job.repl_stall_ticks}")
                                    self.blocked_ticks[rid] = 0
                                    continue
                                else:
                                    # Replenishment has top traffic priority. If its travel
                                    # stalls, discard stale passage holds involving logistics
                                    # and rebuild right-of-way instead of silently idling.
                                    stale = [lr for lr, h in self.priority_holds.items()
                                             if lr == rid or int(h.get("priority", -1)) == rid]
                                    for lr in stale:
                                        self.priority_holds.pop(lr, None)
                                    self._emit("replenish_stalled", t=self.t, robot=rid, pallet=job.repl_pid,
                                               phase=job.repl_phase, stall=job.repl_stall_ticks,
                                               recovery="clear-priority-holds")
                                    job.repl_stall_ticks = 0
                        self.blocked_ticks[rid] += 1
                        # If the order still needs stock but has no pallet assignment,
                        # this is a RESOURCE WAIT, not a traffic deadlock.  Do not let
                        # local escape logic make the robot bang into walls/pallets.
                        resource_wait = self._resource_waiting(rid)
                        if resource_wait:
                            # Parking may itself be temporarily blocked. Do not interpret
                            # this as route livelock and do not let the robot wander.
                            self.blocked_ticks[rid] = 0
                            if self.t % 50 == 0:
                                self._emit(
                                    'master_wait_resource', t=self.t, robot=rid,
                                    reason='parked-or-parking-await-master',
                                    park=self.resource_park_target.get(rid),
                                )
                            continue
                        # LOCAL deadlock recovery: do not wait for the entire warehouse
                        # to become idle. After a few blocked ticks, this robot alone
                        # may take one collision-safe yield step.
                        if self.blocked_ticks[rid] >= self.yield_after:
                            # A higher-priority robot never detours around a lower robot
                            # that it has already ordered to yield. It waits for clearance.
                            has_lower_yielding = any(int(h["priority"]) == rid for h in self.priority_holds.values())
                            step = None if has_lower_yielding else self._escape_step(rid, start_cells)
                            if step is not None:
                                a = Action(self.t, rid, "move", step[0], step[1])
                                try:
                                    self.sim.apply(a)
                                except ValidationError:
                                    pass
                                else:
                                    self._record_action(a)
                                    did_any = True
                                    self.blocked_ticks[rid] = 0
                                    self._emit("yield", t=self.t, robot=rid, x=step[0], y=step[1], kind="local")
                                    if heartbeat_every:
                                        print(
                                            f"SOLVER local-yield: timestep={self.t} robot={rid} -> {step}",
                                            flush=True,
                                        )
                    else:
                        self.blocked_ticks[rid] = 0
                        self.traffic_wait_ticks[rid] = 0
                    continue
                verb, x, y = spec

                # Predict same-timestep traffic conflicts before executing anything.
                # The official move is one grid edge, so the relevant crossing cases are
                # (1) two moving footprints selecting the same future cell and
                # (2) an edge swap.  _next_step already forbids entering another entity's
                # start cell; this reservation layer handles shared empty intersections
                # explicitly and makes the losing robot WAIT instead of colliding/reacting.
                if verb == "move":
                    future = self._proposed_move_footprint(rid, x, y)
                    blockers = sorted({reserved_next[c] for c in future if c in reserved_next})
                    if blockers:
                        self.jobs[rid] = job_backup
                        higher = min(blockers, key=self._traffic_priority)
                        # Same-timestep reservation conflict: lower priority simply
                        # waits.  Do not create any state that survives this timestep.
                        self._master_wait(rid, "yield-to-higher-priority", higher)
                        continue

                a = Action(self.t, rid, verb, x, y)
                try:
                    self.sim.apply(a)
                except ValidationError as e:
                    # Proposal helpers may advance a replenishment phase; roll it back.
                    self.jobs[rid] = job_backup
                    if active:
                        if self.jobs[rid].repl_pid is not None:
                            self.jobs[rid].repl_stall_ticks += 1
                            if (self.jobs[rid].repl_phase == "dock" and
                                    self.jobs[rid].repl_stall_ticks >= self.repl_dock_stall_limit):
                                self._abort_replenishment(rid, f"rejected-{self.jobs[rid].repl_stall_ticks}")
                                self.blocked_ticks[rid] = 0
                                continue
                        self._master_wait(rid, "occupied-or-footprint-conflict")
                    continue
                self._record_action(a)
                did_any = True
                self.blocked_ticks[rid] = 0
                self.traffic_wait_ticks[rid] = 0
                if job.repl_pid is not None:
                    job.repl_stall_ticks = 0
                if verb == "move":
                    for c in self._own_cells(rid):
                        reserved_next[c] = rid
                if verb == "undock" and rid in self.logistics_ids:
                    sku_done = self.logistics_sku.get(rid)
                    if sku_done is not None:
                        self.replenish_requested.discard(sku_done)
                        self.logistics_sku[rid] = None
                        print(f"SOLVER logistics-done: timestep={self.t} R{rid} SKU={sku_done}", flush=True)
                        self._emit("logistics_done", t=self.t, robot=rid, sku=sku_done)
                if verb == "fulfill":
                    job.order_id = None
                    job.needed.clear()
                    job.target_pid = None
                    job.target_sku = None
                    self.target_since[rid] = self.t
                    self.completed_jobs += 1
                    if progress_every and (self.completed_jobs % progress_every == 0 or self.completed_jobs == total):
                        print(f"SOLVER progress: {self.completed_jobs}/{total} orders, timestep={self.t}", flush=True)
                        self._emit("progress", t=self.t, fulfilled=self.completed_jobs, total=total)

            # If nobody could act, break a possible head-on aisle deadlock with one
            # controlled sideways yield. This is intentionally rare; ordinary motion
            # remains strictly distance-decreasing and therefore cannot oscillate.
            if not did_any and stagnant >= 2:
                active_ids = [rid for rid, j in enumerate(self.jobs)
                              if j.order_id is not None or j.repl_pid is not None]
                if active_ids:
                    # Absolute priority: if a rare global escape is needed, make the
                    # LOWEST-priority active robot yield first. Never rotate priority.
                    active_ids.sort(key=self._traffic_priority, reverse=True)
                    for rid in active_ids:
                        step = self._escape_step(rid, start_cells)
                        if step is None:
                            continue
                        a = Action(self.t, rid, "move", step[0], step[1])
                        try:
                            self.sim.apply(a)
                        except ValidationError:
                            continue
                        self._record_action(a)
                        did_any = True
                        self._emit("yield", t=self.t, robot=rid, x=step[0], y=step[1], kind="global")
                        if heartbeat_every:
                            print(f"SOLVER yield: timestep={self.t} robot={rid} -> {step}", flush=True)
                        break

            self._emit("tick", t=self.t, fulfilled=self.completed_jobs, total=total)
            self.t += 1
            if self.completed_jobs != last_completed_jobs:
                last_completed_jobs = self.completed_jobs
                last_completion_t = self.t
            if heartbeat_every and self.t % heartbeat_every == 0:
                active = sum(j.order_id is not None for j in self.jobs)
                repl = sum(j.repl_pid is not None for j in self.jobs)
                print(
                    f"SOLVER heartbeat: timestep={self.t} fulfilled={self.completed_jobs}/{total} "
                    f"active_fulfillment={active} logistics_busy={repl} repl_queue={len(self.replenish_queue)} "
                    f"urgent_repl={len(self._urgent_replenishment()[0])} "
                    f"actions={len(self.actions)} blocked={self.blocked_ticks} traffic_wait={self.traffic_wait_ticks} "
                    f"priority_holds={{{', '.join(f'R{k}->R{int(v["priority"])}' for k,v in self.priority_holds.items())}}} "
                    f"park={self.resource_park_target}",
                    flush=True,
                )
                self._emit("heartbeat", t=self.t, fulfilled=self.completed_jobs, total=total,
                           active_fulfillment=active, logistics_busy=repl,
                           repl_queue=len(self.replenish_queue), actions=len(self.actions),
                           blocked=list(self.blocked_ticks))
            # Activity is not the same as progress. Abort with useful diagnostics if
            # robots keep moving/picking but no order is fulfilled for too long.
            if self.t - last_completion_t > 5000:
                states = []
                for rr in self.sim.robots:
                    jj = self.jobs[rr.id]
                    missing_n = sum(self._missing_for_robot(rr.id).values())
                    storage_n = sum(rr.storage.values())
                    hold_for = int(self.priority_holds[rr.id]["priority"]) if rr.id in self.priority_holds else None
                    states.append((rr.id, (rr.x, rr.y), jj.order_id, jj.target_pid, jj.target_sku, jj.repl_phase, self.blocked_ticks[rr.id], missing_n, storage_n, hold_for, self.logistics_sku.get(rr.id), jj.repl_pid, jj.repl_stall_ticks))
                raise RuntimeError(
                    f"scheduler livelock: no fulfillment for {self.t-last_completion_t} timesteps; "
                    f"fulfilled={self.completed_jobs}/{total}; states={states}"
                )
            stagnant = 0 if did_any else stagnant + 1
            if stagnant > 500:
                states = [(r.id, (r.x,r.y), self.jobs[r.id].order_id,
                           self.jobs[r.id].target_pid, self.jobs[r.id].target_sku,
                           self.jobs[r.id].repl_phase, self.blocked_ticks[r.id]) for r in self.sim.robots]
                raise RuntimeError(f"scheduler deadlock after 500 idle timesteps: {states}")
            if self.t > max_ticks:
                raise RuntimeError(f"scheduler exceeded {max_ticks} timesteps")
        self._emit("done", t=self.t, fulfilled=self.completed_jobs, total=total, actions=len(self.actions))
        return self.actions


def write_actions(path: str, actions: Iterable[Action]) -> None:
    Path(path).write_text("\n".join(a.line() for a in actions) + "\n", encoding="utf-8")


def parse_actions(text: str) -> list[Action]:
    actions = []
    seen = set()
    last_t = -1
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 5:
            raise ValidationError(f"solution line {lineno}: expected 5 fields, got {len(parts)}")
        ts, rid, verb, x, y = parts
        a = Action(int(ts), int(rid), verb, int(x), int(y))
        if a.timestep < last_t:
            raise ValidationError(f"solution line {lineno}: timesteps decreased from {last_t} to {a.timestep}")
        key = (a.timestep, a.robot_id)
        if key in seen:
            raise ValidationError(f"solution line {lineno}: duplicate timestep/robot pair {key}")
        seen.add(key)
        last_t = a.timestep
        actions.append(a)
    return actions


def validate_solution(w: Worklist, actions: list[Action], require_all_orders: bool = True) -> tuple[bool, str, Simulator]:
    sim = Simulator(w)
    by_t: dict[int, list[Action]] = defaultdict(list)
    for a in actions:
        by_t[a.timestep].append(a)

    try:
        for t in sorted(by_t):
            batch = by_t[t]
            # Capture occupancy at the beginning of the timestep. The generated solver
            # uses the conservative rule that movers cannot enter another entity's
            # start-of-timestep cell, even if that entity also moves away.
            start_cells = {(r.x, r.y) for r in sim.robots} | {(p.x, p.y) for p in sim.pallets}
            own_start: dict[int, set[tuple[int, int]]] = {}
            for r in sim.robots:
                own_start[r.id] = {(r.x, r.y)} | {(sim.pallets[pid].x, sim.pallets[pid].y) for pid in r.docked}

            for a in batch:
                if a.verb == "move":
                    r = sim.robots[a.robot_id]
                    dx, dy = a.x-r.x, a.y-r.y
                    target_cells = {(a.x, a.y)}
                    for pid in r.docked:
                        p = sim.pallets[pid]
                        target_cells.add((p.x+dx, p.y+dy))
                    forbidden = start_cells - own_start[a.robot_id]
                    hit = target_cells & forbidden
                    if hit:
                        raise ValidationError(
                            f"timestep={t} robot={a.robot_id}: conservative simultaneous collision at {sorted(hit)[0]}"
                        )
                try:
                    sim.apply(a)
                except Exception as e:
                    raise ValidationError(
                        f"timestep={a.timestep} robot={a.robot_id} action='{a.verb} {a.x} {a.y}': {e}"
                    ) from e

        if require_all_orders and sim.fulfilled_count != len(w.orders):
            raise ValidationError(f"only fulfilled {sim.fulfilled_count}/{len(w.orders)} orders")
        return True, "OK", sim
    except Exception as e:
        return False, str(e), sim


def print_validator_report(ok: bool, message: str, sim: Simulator, actions: list[Action]) -> None:
    final_t = max((a.timestep for a in actions), default=-1)
    print()
    print("=== VALIDATOR REPORT ===")
    print(f"status: {'PASS' if ok else 'FAIL'}")
    print(f"message: {message}")
    print(f"actions: {len(actions)}")
    print(f"final_timestep: {final_t}")
    print(f"fulfilled_orders: {sim.fulfilled_count}/{len(sim.w.orders)}")
    print(f"moves: {sim.move_count}")
    print(f"picks: {sim.pick_count}")
    print(f"docks: {sim.dock_count}")
    print(f"undocks: {sim.undock_count}")
    print(f"replenishment_events: {sim.replenishment_events}")
    per_robot = Counter(a.robot_id for a in actions)
    print("robot_action_counts:", ", ".join(f"R{r.id}={per_robot[r.id]}" for r in sim.robots))
    print("robot_final_positions:", ", ".join(f"R{r.id}=({r.x},{r.y})" for r in sim.robots))
    nonempty = [(r.id, dict(r.storage)) for r in sim.robots if r.storage]
    print(f"robots_with_remaining_storage: {nonempty if nonempty else 'none'}")
    print("=== END VALIDATOR REPORT ===")


def main() -> int:
    ap = argparse.ArgumentParser(description="BIG_ORDER concurrent 5-robot solver and validator")
    ap.add_argument(
        "worklist",
        nargs="?",
        default="BIG_ORDER.txt",
        help="path or https URL to BIG_ORDER.txt",
    )
    ap.add_argument("-o", "--output", default="solution.txt", help="output solution path")
    ap.add_argument("--validate-only", metavar="SOLUTION", help="validate an existing solution instead of solving")
    ap.add_argument("--progress-every", type=int, default=25, help="print solver progress every N orders (0 disables)")
    ap.add_argument("--heartbeat-every", type=int, default=250, help="print a heartbeat every N timesteps (0 disables)")
    ap.add_argument("--logistics-robots", type=int, default=1,
                    help="number of highest-numbered robots dedicated to logistics/replenishment (default: 1)")
    ap.add_argument("--logistics-ids", default=None,
                    help="comma-separated explicit logistics robot ids, e.g. 3,4; overrides --logistics-robots")
    ap.add_argument("--order-lookahead", type=int, default=2,
                    help="orders reserved/planned per fulfillment robot (default: 2; physical storage still fulfills one exact order at a time)")
    ap.add_argument("--yield-after", type=int, default=8,
                    help="blocked timesteps before an individual robot makes a collision-safe yield move (default: 8)")
    ap.add_argument("--master-stop-after", type=int, default=24,
                    help="blocked timesteps before MASTER revokes a robot's pallet target (default: 24)")
    ap.add_argument("--target-cooldown", type=int, default=40,
                    help="timesteps before MASTER may reassign the same robot/pallet pair after revocation (default: 40)")
    ap.add_argument("--live-pipe", default=None, metavar="FIFO",
                    help="stream solver/master events as JSONL to this named pipe for live visualization")
    args = ap.parse_args()

    try:
        text = read_text(args.worklist)
        w = parse_worklist(text)
        print_parse_report(w)
    except Exception as e:
        print("=== PARSE REPORT ===")
        print("status: FAIL")
        print(f"message: {e}")
        print("=== END PARSE REPORT ===")
        return 2

    if args.validate_only:
        try:
            actions = parse_actions(Path(args.validate_only).read_text(encoding="utf-8"))
        except Exception as e:
            sim = Simulator(w)
            print_validator_report(False, f"solution parse error: {e}", sim, [])
            return 3
    else:
        try:
            nrobots = len(w.robots)
            if args.logistics_ids is not None:
                logistics_ids = {int(x.strip()) for x in args.logistics_ids.split(",") if x.strip()}
            else:
                nlog = max(0, min(args.logistics_robots, nrobots - 1))
                logistics_ids = set(range(nrobots - nlog, nrobots)) if nlog else set()
            live = LivePipeWriter(args.live_pipe) if args.live_pipe else None
            solver = MultiRobotSolver(
                w, logistics_ids=logistics_ids, order_lookahead=args.order_lookahead,
                yield_after=args.yield_after, master_stop_after=args.master_stop_after,
                target_cooldown=args.target_cooldown, live=live,
            )
            try:
                actions = solver.solve(args.progress_every, args.heartbeat_every)
            except Exception as e:
                if live is not None:
                    live.emit("error", t=solver.t, message=str(e))
                raise
            finally:
                if live is not None:
                    live.close()
            write_actions(args.output, actions)
            print(f"\nWROTE: {args.output} ({len(actions)} actions)")
        except Exception as e:
            print("\nSOLVER FAILED:")
            print(e)
            return 4

    ok, message, sim = validate_solution(w, actions)
    print_validator_report(ok, message, sim, actions)
    return 0 if ok else 5


if __name__ == "__main__":
    raise SystemExit(main())
