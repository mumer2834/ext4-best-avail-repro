# ext4-best-avail-repro

Reproducers for two ext4 mballoc issues in
`ext4_mb_scan_groups_best_avail()`, fixed by the patch series
"[PATCH 0/2] ext4: fix goal trimming in CR_BEST_AVAIL_LEN" on
linux-ext4@vger.kernel.org.

1. Groups below the scan start are searched only after the goal has been
   trimmed to min_order, so smaller free extents are used while larger
   ones are available. Regression from a3ce570a5d6a (v6.17).
2. min_order is one order too high for power-of-two requests, so they
   skip CR_BEST_AVAIL_LEN and fall through to CR_GOAL_LEN_SLOW.
   Present since 7e170922f06b.

## Requirements

- root
- python3
- e2fsprogs (`mkfs.ext4`, `filefrag`, `dumpe2fs`)
- util-linux (`findmnt`)
- tracefs mounted at `/sys/kernel/tracing`

The scripts create a 2 GiB loop image in `/tmp/ext4ba` (or the directory
given as the first argument) and touch nothing else. Keep both scripts in
the same directory; the second imports helpers from the first.

## Usage

```
sudo python3 -I ext4_best_avail_repro.py
sudo python3 -I ext4_best_avail_minorder_repro.py
```

Each takes about a minute.

## Expected results

`ext4_best_avail_repro.py` (issue 1). Same free-space map in every run:
groups 0-7 hold only 1024-block free extents, groups 8-15 only 256-block
ones.

| Run | Unpatched | Patched |
|---|---|---|
| A: start group 0 (control) | 8 x 1024 | 8 x 1024 |
| B: start group 8 | 7 x 1024, 4 x 256 | 8 x 1024 |
| C: start group 0, max_linear_groups=4 | 6 x 1024, 2 x 512, 4 x 256 | 8 x 1024 |

`ext4_best_avail_minorder_repro.py` (issue 2). Every group holds only
512-block free extents.

| Write size | Unpatched | Patched |
|---|---|---|
| 511 blocks | CR_BEST_AVAIL_LEN | CR_BEST_AVAIL_LEN |
| 512 blocks | CR_GOAL_LEN_SLOW | CR_BEST_AVAIL_LEN |

Full output from 7.2.15 before and after the patches is in `output/`.
