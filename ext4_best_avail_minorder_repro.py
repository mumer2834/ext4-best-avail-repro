#!/usr/bin/env python3
"""
Reproducer: off-by-one in ext4_mb_scan_groups_best_avail() min_order:

    if (1 << min_order < ac->ac_o_ex.fe_len)
            min_order = fls(ac->ac_o_ex.fe_len);

fls(512) == 10, so for an original request of exactly 2^k blocks the trimmed
goal is never allowed to go down to 2^k even though 2^k fully satisfies the
request (order_base_2()/fls(len - 1) is the intended bound).  A request of
512 blocks therefore skips CR_BEST_AVAIL_LEN and falls into the linear
CR_GOAL_LEN_SLOW scan, while a 511-block request is served by
CR_BEST_AVAIL_LEN from the avg-fragment xarray.

Layout: 16 groups, every group has only 512-block free chunks.  Each victim
write is one O_DIRECT write at a 2 MiB-aligned offset into a file with
i_size = 64 MiB, so the goal is normalized to 2048 blocks:
  run 512: writes of 512 blocks (2 MiB)
  run 511: writes of 511 blocks (2 MiB - 4 KiB)
Reported: which criteria satisfied each allocation (tracepoint) and the
groups-considered counters from /proc/fs/ext4/<dev>/mb_stats.

Run as root:  python3 -I ext4_best_avail_minorder_repro.py [workdir]
"""
import ctypes, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ext4_best_avail_repro as r  # noqa: E402  (shared helpers)

NWRITES = 12


def odirect_write(path, nblocks_each):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_DIRECT)
    os.ftruncate(fd, 64 << 20)
    n = nblocks_each * r.BS
    raw = ctypes.create_string_buffer(n + 4096)
    addr = (ctypes.addressof(raw) + 4095) & ~4095
    mv = memoryview((ctypes.c_char * n).from_address(addr))
    for i in range(NWRITES):
        os.pwrite(fd, mv, i * (2 << 20))
    os.fsync(fd)
    os.close(fd)


def run(nblocks):
    r.sh(f"umount {r.MNT} 2>/dev/null", check=False)
    os.makedirs(r.MNT, exist_ok=True)
    r.sh(f"rm -f {r.IMG}; truncate -s {r.NGROUPS * r.BPG * r.BS} {r.IMG}")
    r.sh(f"mkfs.ext4 -q -F -b {r.BS} -g {r.BPG} -m 0 -O ^flex_bg,^resize_inode "
         f"-E lazy_itable_init=0 {r.IMG}")
    r.sh(f"mount -o loop {r.IMG} {r.MNT}")
    dev = os.path.basename(r.sh(f"findmnt -n -o SOURCE {r.MNT}").strip())
    sysd = f"/sys/fs/ext4/{dev}"
    r.wr(f"{sysd}/reserved_clusters", 0)
    victim = os.path.join(r.MNT, "victim")
    open(victim, "w").close()
    vino = os.stat(victim).st_ino
    spare = os.path.join(r.MNT, "spare")
    fd = os.open(spare, os.O_CREAT | os.O_RDWR)
    r.libc.fallocate(fd, 0, 0, 128 * r.BS)
    os.close(fd)
    fills = r.fill_fs()
    os.unlink(spare)
    r.sh("sync")
    r.punch_region(fills, set(range(r.NGROUPS)), 512)
    r.sh("sync")
    r.wr(f"{sysd}/mb_stats", 1)
    r.wr(f"{r.TRACE}/trace", "")
    r.wr(f"{r.TRACE}/events/ext4/ext4_mballoc_alloc/enable", 1)
    odirect_write(victim, nblocks)
    r.wr(f"{r.TRACE}/events/ext4/ext4_mballoc_alloc/enable", 0)
    crs = re.findall(rf"inode {vino} orig .*? cr (\S+) flags \S*STREAM_ALLOC",
                     open(f"{r.TRACE}/trace").read())
    stats = open(f"/proc/fs/ext4/{dev}/mb_stats").read()
    slow = re.search(r"cr_goal_slow_stats:\s*\n\s*hits:\s*(\d+)\s*\n\s*groups_considered:\s*(\d+)",
                     stats)
    ba = re.search(r"cr_best_avail_stats:\s*\n\s*hits:\s*(\d+)\s*\n\s*groups_considered:\s*(\d+)",
                   stats)
    r.sh(f"umount {r.MNT}")
    hist = {c: crs.count(c) for c in sorted(set(crs))}
    print(f"[{nblocks}-block writes] {len(crs)} allocations, criteria: {hist}")
    if ba and slow:
        print(f"    mb_stats: best_avail hits={ba[1]} groups_considered={ba[2]}; "
              f"goal_slow hits={slow[1]} groups_considered={slow[2]}")
    else:
        print("    mb_stats:\n" + stats)
    return hist


def main():
    if os.geteuid():
        sys.exit("run as root")
    os.makedirs(r.WORK, exist_ok=True)
    print(f"kernel {os.uname().release}; all groups hold only 512-block free chunks")
    run(511)
    run(512)


if __name__ == "__main__":
    main()
