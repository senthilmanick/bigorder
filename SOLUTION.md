# Solution

## Attempts
1. The main solver has a master
2. Needed a visualizer to look at where the robots are getting stuck

### Are there better methods
1. Of course yes
2. We could use an AI engine to counter
3. Will attempt next. This algorithm, was generated based on prompts and finally reviewed

## How to run
1. git clone this repo
2. Setup
```
uv init
uv sync
```
3. Start the visualizer
```
uv run bigorder_visualizer.py \
BIG_ORDER.txt \
    --live-pipe /tmp/bigorder.pipe \
    --skip 10
```
4. Start the solver
```
uv run bigorder_solver.py BIG_ORDER.txt \
    --live-pipe /tmp/bigorder.pipe \
    --logistics-robots 1 \
    --order-lookahead 2 \
    --yield-after 8 \
    --master-stop-after 24 \
    --target-cooldown 40 \
    --heartbeat-every 100 \
    --progress-every 10
```

# Big Order Solver Architecture

## Overview

The solver is a **centralized hierarchical controller**.

It is not five independent robots each running their own shortest-path
planner. The master assigns work, decides traffic priority, reserves
short pieces of future routes, resolves local conflicts, and then allows
each robot to execute at most one action per timestep.

``` text
                         +-----------------------------+
                         |       MASTER SCHEDULER      |
                         +--------------+--------------+
                                        |
             +--------------------------+--------------------------+
             |                          |                          |
             v                          v                          v
      ORDER / RESOURCE            REPLENISHMENT              TRAFFIC MASTER
         SCHEDULER                  SCHEDULER
             |                          |                          |
      assign orders              urgent/background          fixed priority
      assign pallets             replenish queue            route reservations
      reserve pallets            R4 logistics               conflict resolution
             |                          |                          |
             +--------------------------+--------------------------+
                                        |
                                        v
                         +-----------------------------+
                         |       MOTION PLANNING       |
                         |                             |
                         | weighted shortest path      |
                         | + soft reservations         |
                         | + local joint MAPF          |
                         +--------------+--------------+
                                        |
                                        v
                         +-----------------------------+
                         |     ACTION / SIMULATOR      |
                         | move/pick/dock/undock/      |
                         | fulfill                     |
                         +-----------------------------+
```

------------------------------------------------------------------------

## 1. Order Scheduler

Each fulfillment robot owns **one physical order at a time**.

For example:

``` text
R0 -> Order 100
R1 -> Order 101
R2 -> Order 102
R3 -> Order 103
R4 -> fallback Order 104
```

There is lookahead for future orders, but the robot does **not** mix
multiple orders in its storage.

This is important because fulfillment requires:

``` text
robot.storage == exact order
```

The lookahead is therefore a planning optimization, not physical
batching.

------------------------------------------------------------------------

## 2. Central Pallet Assignment

Robots do not independently decide:

``` text
"I need SKU 17, I'll go to pallet 42."
```

The master makes that decision.

``` text
                 MASTER

SKU17 needed by R0 --+
SKU17 needed by R2 --+--> pallet assignment
SKU51 needed by R1 --+

                     P42 -> R0
                     P87 -> R2
                     P91 -> R1
```

A pallet is exclusively targeted by one robot. This prevents two or
three robots from chasing the same pallet.

An active fulfillment robot normally follows:

``` text
order
  |
  v
missing SKU
  |
  v
target pallet
  |
  v
movement goal
```

If no usable resource exists, the robot becomes a **resource waiter**
and is moved out of the working aisles.

------------------------------------------------------------------------

## 3. Resource-Wait Parking

Suppose R2 needs SKU 7 but cannot currently obtain a pallet.

Instead of leaving R2 in an aisle:

``` text
             aisle
               |
               |
              R2   <-- BAD: permanent obstacle
               |
               |
```

the master gives it a parking target near an outside edge:

``` text
R2 ---------------------------> PARK
                                x=1
```

As soon as a usable pallet becomes available, the master assigns a real
target and the parking target is discarded.

------------------------------------------------------------------------

## 4. Replenishment Architecture

R4 has a dual role:

``` text
                 R4
                  |
          +-------+--------+
          |                |
          v                v
    replenishment      fulfillment
       primary          fallback
```

Replenishment work is divided into two categories.

### Urgent replenishment

An active order needs a SKU and there is no usable stocked pallet for
that SKU.

### Background replenishment

A pallet is empty, but no current order is blocked by it.

The intended scheduling priority is:

``` text
URGENT replenishment
        >
normal fulfillment
        >
BACKGROUND replenishment
```

This prevents background warehouse maintenance from continually stealing
R4 away from useful fulfillment.

### Replenishment state machine

``` text
      select empty pallet
              |
              v
            DOCK
              |
              v
           BOTTOM
       move toward y=39
              |
              v
           REFILL
      automatic at y=39
              |
              v
           RETURN
              |
              v
          original
          location
              |
              v
           UNDOCK
```

The diagnostic state can therefore contain:

``` text
repl_phase='dock'
repl_phase='bottom'
repl_phase='return'
```

------------------------------------------------------------------------

## 5. Fixed Traffic Priority

Traffic priority is deliberately separate from order scheduling.

The current hierarchy is fixed:

``` text
R4 while replenishing
        >
R0
        >
R1
        >
R2
        >
R3
        >
R4 fulfillment
        >
parked/resource-wait robots
```

The important property is that the priority is **deterministic**.

Right-of-way does not change because a robot picked another item or
became closer to completing its order.

------------------------------------------------------------------------

## 6. Hard Direct-Conflict Arbitration

Suppose R0 wants a cell currently occupied by R2:

``` text
R0 ---> [R2]
```

and:

``` text
priority(R0) > priority(R2)
```

The master explicitly commands:

``` text
R0 = WAIT
R2 = CLEAR
```

For example:

``` text
Before:

       R0
       |
       v
       R2
       |
       v

One timestep:

       R0       WAIT

       .
      R2 --->   CLEAR
```

The next timestep is then replanned from the actual positions.

This is the **hard priority layer**.

------------------------------------------------------------------------

## 7. Why Route Reservation Is Needed

Collision priority alone does not prevent two robots from selecting the
same route.

For example:

``` text
t=0

R0  ---->
R2  ---->


t=5

       R0
       R2
```

R2 can move aside when R0 blocks it, but without another mechanism it
may immediately return to the same shortest-path centerline.

The reason is that both robots independently see the same warehouse
geometry and therefore discover the same shortest route.

``` text
R0: xxxxxxxxxxxxxxxx
R2: xxxxxxxxxxxxxxxx
```

Hard priority resolves the collision, but it does not encourage route
diversity.

This architecture therefore adds **soft future-route
reservations**.

------------------------------------------------------------------------

## 8. Soft Future-Route Reservations

Before movement, the master calculates approximately the next **10
cells** of each higher-priority robot's preferred route.

Example:

``` text
R0 current
   |
   v

   A
   A
   A
   A
   A
   A
   A
   A
   A
   A

A = R0 soft reservation
```

These cells are not physically occupied.

They mean:

> R0 is likely to use this area shortly.

A lower-priority robot sees those cells as **expensive**, rather than
forbidden.

If R2 has two possible routes:

``` text
                 R0 reserved
                    |
                    |
                    |
R2 -----------+-----+------ TARGET
              |
              |
              +------------ TARGET
                alternate
```

R2 should prefer the alternate route when the detour is reasonable.

------------------------------------------------------------------------

## 9. Weighted Dijkstra

This is the core routing change in this solver.

One could use path cost approximately:

``` text
cost(cell) = 1
```

Now, for a lower-priority robot:

``` text
normal cell                   = 1
higher-priority reserved cell = 1 + reservation penalty
```

The current reservation penalty is **8**.

For example, suppose a shared route has 10 travel steps and 4 reserved
cells:

``` text
cost = 10 + 4 * 8
     = 42
```

while an alternate route has 14 ordinary steps:

``` text
cost = 14
```

The planner chooses the 14-step alternate route.

The routing objective is therefore closer to:

``` text
travel distance + traffic interference
```

rather than simply:

``` text
travel distance
```

------------------------------------------------------------------------

## 10. Why Reservations Are Soft

Higher-priority future routes are deliberately **not hard obstacles**.

Consider a warehouse section with only one usable passage:

``` text
###########
R0 --->   #
######### #
R2 --->   #
###########
```

R2 must eventually be allowed to use the same passage.

A hard reservation could incorrectly produce:

``` text
R2: NO PATH
```

A soft reservation instead means:

``` text
Use another path if reasonably possible.
Otherwise this path is still legal.
```

This avoids recreating the earlier corridor-lock behavior.

------------------------------------------------------------------------

## 11. Local Joint Multi-Agent Path Finding (MAPF)

When robots become physically close, the solver does not rely only on
their individual weighted paths.

It creates a small local group, normally:

``` text
{R0, R2}
```

or:

``` text
{R0, R2, R4}
```

and performs a short-horizon joint search, currently approximately six
timesteps.

``` text
                 current state

                  R0   R2

                       |
           +-----------+------------+
           v           v            v
        possibility  possibility  possibility
           |           |            |
           v           v            v
         t+1          t+1          t+1
           |
          ...
           |
          t+6
```

The planner considers combinations such as:

``` text
R0 move, R2 move
R0 move, R2 wait
R0 wait, R2 move
R0 move, R2 temporarily move away
...
```

Combinations that produce footprint collisions are rejected.

Only the **first action** of the selected plan is executed. The complete
situation is replanned on the next timestep.

This is a receding-horizon local MAPF strategy rather than committing
robots to long multi-agent trajectories.

------------------------------------------------------------------------

## 12. Traffic Mechanisms and Their Responsibilities

The traffic system contains several layers because each solves a
different problem.

``` text
SOFT ROUTE RESERVATION
        |
        | prevents robots from choosing
        | the same future route
        v

WEIGHTED PATH PLANNING
        |
        | chooses an alternate aisle
        | when worthwhile
        v

LOCAL JOINT MAPF
        |
        | handles nearby multi-robot
        | motion interaction
        v

HARD PRIORITY CLEARANCE
        |
        | final authority when one robot
        | directly blocks another
        v

       ACTION
```

In simpler terms:

> **Route reservation:** "Don't follow me if you don't have to."

> **Joint planner:** "Let's coordinate our next few moves."

> **Hard priority:** "You're physically in my way. You move."

------------------------------------------------------------------------

## 13. One Complete Solver Timestep

The master loop is approximately:

``` text
BEGIN TIMESTEP

1. Examine pallet inventory

2. Build urgent/background replenishment queue

3. Assign R4 logistics work if appropriate

4. Assign orders to idle robots

5. Determine missing SKUs

6. Assign exclusive target pallets

7. Detect robots with unavailable resources

8. Give those robots parking targets

9. Determine movement goals

10. Sort robots by fixed traffic priority

11. Compute higher-priority preferred paths

12. Reserve approximately 10 future cells

13. Run hard direct-conflict arbitration

14. For nearby robots:
       run local joint MAPF

15. For remaining robots:
       run weighted shortest-path planning

16. Reserve proposed next footprints

17. Execute at most one action per robot:
       move
       pick
       dock
       undock
       fulfill

18. Update pallet, robot, and order state

END TIMESTEP

repeat
```

------------------------------------------------------------------------

## 14. Overall Architecture

``` text
                        BIG_ORDER
                            |
                            v
                 +--------------------+
                 |   MASTER SCHEDULER |
                 +---------+----------+
                           |
          +----------------+----------------+
          v                v                v
     ORDER MASTER     RESOURCE MASTER   LOGISTICS MASTER
          |                |                |
      order/RID        SKU/pallet       urgent/background
      lookahead        assignment       replenishment
          |                |                |
          +----------------+----------------+
                           |
                           v
                  MOVEMENT GOALS
                           |
                           v
                 FIXED PRIORITY ORDER
                           |
                           v
              FUTURE ROUTE RESERVATIONS
                    (~10 cells)
                           |
                           v
                  WEIGHTED DIJKSTRA
                distance + traffic cost
                           |
                           v
                   LOCAL JOINT MAPF
                    (~6-step horizon)
                           |
                           v
                HARD PRIORITY CLEARANCE
                           |
                           v
                   FOOTPRINT SAFETY
                           |
                           v
              move / pick / dock / undock
                       / fulfill
                           |
                           v
                       SIMULATOR
                           |
                           +------> next timestep
```

------------------------------------------------------------------------

## Architectural Principle

> **Centralize resource ownership and right-of-way, use soft costs to
> prevent traffic before it happens, and reserve hard priority
> intervention for actual conflicts.**

