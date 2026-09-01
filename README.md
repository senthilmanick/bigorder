# atoms not electrons

Congratulations, you have inherited your great uncle's warehouse. The Big Order is due and the Client is breathing down your neck. Fulfill it as fast as possible by telling your robot fleet what to do. Good luck!

## The Challenge

You command **5 robots** in a **60x40 grid** warehouse. Pallets carry items of one SKU each and have **finite stock**. Your mission: fulfill **1,000 orders** by collecting the right items, delivering each order to the **fulfillment row at the top** (`y=0`), and refilling depleted pallets at the **replenishment row at the bottom** (`y=39`).

**Your score** = total timesteps to complete ALL orders. Lower is better.

## Key Concepts

| Term | Meaning |
|------|---------|
| **Pallet** | A container holding items of a single SKU. Has a finite `count` (currently held) and `maxCount` (capacity). All pallets sharing a SKU share the same capacity. |
| **Order** | A list of SKUs to collect and deliver together (e.g., "2x SKU 1, 1x SKU 3, 1x SKU 7") |
| **Storage** | Robot's internal inventory where picked items are held |
| **Fulfill** | Deliver items at the top row (`y=0`) when your storage EXACTLY matches an unfulfilled order |
| **Replenish** | Restore a docked pallet to capacity by parking your robot on the bottom row (`y=39`) |

## The Grid

- **Dimensions**: 60 cells wide x 40 cells tall, indexed (x,y) from (0,0) at top-left
- **Fulfillment zone**: top row, `y=0` — robots deliver orders here
- **Replenishment zone**: bottom row, `y=39` — pallets refill here
- Pallets start somewhere between rows `y=1` and `y=38` (never inside a zone)

## Robot Actions

Each robot executes AT MOST ONE action per timestep:

| Action | Usage | Effect |
|--------|-------|--------|
| `move` | `move <x> <y>` | Move to an adjacent empty cell |
| `pick` | `pick <x> <y>` | Pick 1 item from an adjacent pallet (decrements its `count`; fails if empty) |
| `dock` | `dock <x> <y>` | Attach to an adjacent pallet (see Docking below) |
| `undock` | `undock <x> <y>` | Detach from a docked pallet |
| `fulfill` | `fulfill <x> <y>` | Deliver order at the top row (coords ignored) |

## Docking Explained

**Why dock?** Normally, robots must be adjacent to a pallet to pick from it. By docking, you attach a pallet to your robot — they move together as a unit. This is also how you bring a depleted pallet to the replenishment row.

**Use cases:**
- Move a pallet closer to the fulfillment zone
- Carry a frequently-needed pallet with you instead of returning to it
- Drag a depleted pallet to `y=39` to refill it

**How it works:**
- A docked pallet moves WITH the robot (same direction, same timestep)
- Docked pallets still occupy grid cells and can collide with other entities
- Robots can dock up to 4 pallets (one on each side)
- You can still pick from docked pallets (yours or others')

## Replenishment

Pallets have finite stock. Refilling is **automatic and free of an action**: at the end of any timestep where a robot is on `y=39` with one or more pallets docked, those docked pallets are restored to full capacity (`count = maxCount`). The cost is just the timestep your robot spends sitting in the replenishment row.

- The **robot** triggers the refill, not the pallet — the pallet itself doesn't have to be on `y=39`, only its owner robot.
- Picks earlier in the same timestep happen before the refill, so a docked pallet picked from at `y=39` is refilled afterwards in the same timestep.
- Your robot can take any other action during a refill timestep.
- A pallet that isn't docked to anyone won't refill, even if a robot stands next to it on `y=39`.

## Rules

1. **Movement**: Adjacent cells only (not diagonal). Target must be empty.
2. **Picking**: Pallet must have at least 1 item. Multiple robots can pick from the same pallet in the same timestep, but the combined picks may not exceed the current `count`.
3. **Fulfillment**: Robot must be on `y=0`. Storage must EXACTLY match an unfulfilled order — no more, no less.
4. **Collision**: No two entities can occupy the same cell.

## Submission Format

Text file with one action per line:
```
<timestep> <robot_id> <action> <x> <y>
```

Example:
```
0 0 move 1 0
0 1 pick 10 5
1 0 move 2 0
1 0 pick 3 0
2 0 move 5 0
3 0 fulfill 0 0
```

Requirements:
- Lines must be in increasing timestep order
- No duplicate (timestep, robot_id) pairs
- Robots with no action at a timestep simply wait

## Worklist Format (BIG_ORDER.txt)

```
<num_robots>
<x> <y>              # Robot starting positions
...
<num_skus>
<capacity>           # Pallet capacity for SKU 0
<capacity>           # Pallet capacity for SKU 1
...
<num_pallets>
<x> <y> <sku>        # Pallet positions and SKU types
...
<num_orders>
<sku1> <sku2> ...    # Space-separated SKUs per order
...
```

Pallet `count` is initialized to its SKU's declared capacity. SKU ids are 0-indexed.

## The Big Order

- **5** robots
- **100** unique SKU types
- **240** pallets, distributed so most SKUs have multiple pallets:
  - 5 SKUs × 1 pallet
  - 50 SKUs × 2 pallets
  - 45 SKUs × 3 pallets
- **Per-SKU capacity** drawn uniformly at random from `[80, 300]` boxes; all pallets sharing a SKU share that capacity
- **1,000** orders (30–100 items each)
- **SKU distribution**: power law (Zipf-like) — some SKUs are "high runners" appearing much more frequently in orders. Total demand for each SKU comfortably exceeds what one pallet holds, so replenishment is a real game mechanic.

## How to Participate

1. **Download** `BIG_ORDER.txt` — the warehouse state and orders
2. **Write a solver** that outputs robot commands. AI agent use strongly recommended.
3. **Use the Testbench** — upload your solution to visualize and debug before submitting
4. **Submit to leaderboard** — requires GitHub login

## Testbench

The testbench lets you validate and debug your solution before submitting.

1. Click **TESTBENCH** on the homepage
2. Drop your `.txt` solution file
3. The simulation runs in-browser — scrub the timeline to step through each timestep
4. Inspect robot positions, pallet contents, and order fulfillment
5. When ready, click **Submit to Leaderboard** (requires GitHub login)

All validation happens locally. Nothing touches the server until you explicitly submit.

## Tips

- Plan efficient paths — minimize robot travel time
- Consider which robots are closest to which pallets
- Docking can reduce repeated trips to distant pallets, and is the only way to refill a pallet
- Multiple robots can work in parallel
- Batch orders by shared SKUs
- High-runner SKUs benefit from being parked near the fulfillment row; replenishment trips amortize across many orders
- Start simple with a single-robot no-docking naive solution, then optimize

---

*AI use encouraged. Don't spam. Play fair.*

*Created with love at [Tutor Intelligence](https://tutorintelligence.com), building generally capable robot workers for American industry. If you enjoy this puzzle, please [consider joining our team](https://jobs.lever.co/tutorintelligence)!*
