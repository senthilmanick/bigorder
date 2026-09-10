# Solution

## Attempts
1. The main solver has a master
2. Needed a visualizer to look at where the robots are getting stuck

### Are there more better mehods
1. Of course yes
2. We could use an AI engine to counter
3. Will attempt next. This alghorithm, was generated based on prompts and finally reviewed

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