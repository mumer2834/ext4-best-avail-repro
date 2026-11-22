#!/usr/bin/env python3
"""
Reproducer: ext4 CR_BEST_AVAIL_LEN over-trims the goal length because it
trims all the way down over groups [start, ngroups) before it looks at
groups [0, start).  Regression from commit a3ce570a ("ext4: implement
linear-like traversal across order xarrays", v6.17); still present in
mainline as of 2026-11-21.

ext4_mb_scan_groups_best_avail() is documented as "find the highest free
fragment order we have and proactively trim the goal request length to that
order".  Since a3ce570a it instead does

    for each pass in ([start, N), [0, start)):
        for i = order .. min_order:            # trim 2048 -> 1024 -> 512 -> 256
            scan avg_fragment_size xarray for 1<<i in this pass's range

so a 1024-block fragment in a group below `start` loses to a 256-block one in
a group above it.  For stream allocations (every file > 16 blocks) `start` is
the global goal s_mb_last_groups[ino % nr] (where the last stream allocation
landed), and the 4-group linear prescan advances it further, so start != 0 is
the normal case.

Layout (4k blocks, 32768 blocks/group, 16 groups, no flex_bg, -m 0), built by
filling the fs completely and punching holes:
  groups 0..7  : only 1024-block free chunks   (avg-fragment order 9)
  groups 8..15 : only  256-block free chunks   (avg-fragment order 7)
No free extent >= 2048 anywhere, so for an 8 MiB normalized goal (2048 blocks)
CR_POWER2_ALIGNED and CR_GOAL_LEN_FAST fail and CR_BEST_AVAIL_LEN decides.
Correct result: trim 2048 -> 1024 and use groups 0..7.

Runs (identical free-space map; only the global goal / linear prescan differ):
  A) global goal = group 0, mb_max_linear_groups=0   -> control
  B) global goal = group 8, mb_max_linear_groups=0   -> isolated bug
  C) global goal = group 0, mb_max_linear_groups=4 (default on rotational)
The victim file is written with 64 KiB O_DIRECT writes, i_size preset to
64 MiB so every allocation is normalized to an 8 MiB goal.

Run as root:  python3 -I ext4_best_avail_repro.py [workdir]
"""
import ctypes, os, re, subprocess, sys

WORK = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "/tmp/ext4ba")
IMG, MNT = os.path.join(WORK, "fs.img"), os.path.join(WORK, "mnt")
BS, BPG, NGROUPS = 4096, 32768, 16
LOW_CHUNK, HIGH_CHUNK = 1024, 256
FILE_MB, WRITE_KB = 32, 64
TRACE = "/sys/kernel/tracing"

libc = ctypes.CDLL("libc.so.6", use_errno=True)
libc.fallocate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_long, ctypes.c_long]
PUNCH = 0x01 | 0x02  # FALLOC_FL_KEEP_SIZE | FALLOC_FL_PUNCH_HOLE
LOW, HIGH = set(range(NGROUPS // 2)), set(range(NGROUPS // 2, NGROUPS))


def sh(cmd, check=True):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode:
        sys.exit(f"FAILED: {cmd}\n{r.stdout}{r.stderr}")
    return r.stdout


def wr(path, val):
    with open(path, "w") as f:
        f.write(str(val))


def extents(path):
    out = []
    for line in sh(f"filefrag -v {path}").splitlines():
        m = re.match(r"\s*\d+:\s+(\d+)\.\.\s*(\d+):\s+(\d+)\.\.\s*(\d+):\s+(\d+)", line)
        if m:
            out.append((int(m[1]), int(m[3]), int(m[5])))
    return out


def fill_fs():
    """Allocate every free block; returns list of (path, extents)."""
    files, i = [], 0
    while True:
        free = os.statvfs(MNT).f_bfree
        if free == 0:
            break
        p = os.path.join(MNT, f"fill{i}")
        fd = os.open(p, os.O_CREAT | os.O_RDWR)
        n = free
        while n > 0 and libc.fallocate(fd, 0, 0, n * BS):
            n = n - 8 if n > 8 else n - 1
        os.fsync(fd)
        os.close(fd)
        i += 1
        if n <= 0 or i > 200:
            break
        files.append((p, extents(p)))
    return files


def punch_region(fills, groups, chunk):
    n = 0
    for path, ext in fills:
        fd = os.open(path, os.O_RDWR)
        for lo, phys, ln in ext:
            end = phys + ln
            first = (phys + chunk - 1) // chunk * chunk
            for s in range(first, end - chunk + 1, chunk):
                g, off = divmod(s, BPG)
                if g in groups and (off // chunk) % 2 == 0 and (s + chunk - 1) // BPG == g:
                    if libc.fallocate(fd, PUNCH, (lo + s - phys) * BS, chunk * BS):
                        sys.exit(f"punch failed errno {ctypes.get_errno()}")
                    n += 1
        os.fsync(fd)
        os.close(fd)
    return n


def group_summary(dev):
    rows = []
    for line in open(f"/proc/fs/ext4/{dev}/mb_groups"):
        m = re.match(r"#(\d+)\s*:\s*(\d+)\s+(\d+)\s+(\d+)\s+\[(.*)\]", line)
        if m:
            g, free, frags = int(m[1]), int(m[2]), int(m[3])
            c = [int(v) for v in m[5].split()]
            rows.append((g, free, frags, free // frags if frags else 0, c))
    return rows


def write_direct(path, total_kb, isize_mb=0):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_DIRECT)
    if isize_mb:
        os.ftruncate(fd, isize_mb << 20)
    raw = ctypes.create_string_buffer(WRITE_KB * 1024 + 4096)
    addr = (ctypes.addressof(raw) + 4095) & ~4095
    mv = memoryview((ctypes.c_char * (WRITE_KB * 1024)).from_address(addr))
    for i in range(max(1, total_kb // WRITE_KB)):
        os.pwrite(fd, mv, i * WRITE_KB * 1024)
    os.fsync(fd)
    os.close(fd)


def run(label, prime_groups, linear, show_map=False):
    sh(f"umount {MNT} 2>/dev/null", check=False)
    os.makedirs(MNT, exist_ok=True)
    sh(f"rm -f {IMG}; truncate -s {NGROUPS * BPG * BS} {IMG}")
    sh(f"mkfs.ext4 -q -F -b {BS} -g {BPG} -m 0 -O ^flex_bg,^resize_inode "
       f"-E lazy_itable_init=0 {IMG}")
    sh(f"mount -o loop {IMG} {MNT}")
    dev = os.path.basename(sh(f"findmnt -n -o SOURCE {MNT}").strip())
    sysd = f"/sys/fs/ext4/{dev}"
    wr(f"{sysd}/mb_max_linear_groups", 0)   # keep priming deterministic
    wr(f"{sysd}/reserved_clusters", 0)      # let the fill take every block
    nr_goals = min(os.cpu_count(), (NGROUPS + 3) // 4)

    victim = os.path.join(MNT, "victim")
    open(victim, "w").close()               # inode now, blocks later
    vino = os.stat(victim).st_ino
    spare = os.path.join(MNT, "spare")      # room for extent-tree splits
    sfd = os.open(spare, os.O_CREAT | os.O_RDWR)
    libc.fallocate(sfd, 0, 0, 128 * BS)
    os.close(sfd)
    fills = fill_fs()
    left = os.statvfs(MNT).f_bfree
    os.unlink(spare)
    sh("sync")

    # 1) free space only in the priming region; stream-allocate 1 MiB files
    #    there until every s_mb_last_groups[] slot points into that region.
    chunk = LOW_CHUNK if prime_groups is LOW else HIGH_CHUNK
    punch_region(fills, prime_groups, chunk)
    primed, i = {}, 0
    while len(primed) < nr_goals and i < 32:
        p = os.path.join(MNT, f"prime{i}")
        write_direct(p, 1024)
        primed.setdefault(os.stat(p).st_ino % nr_goals, extents(p)[-1][1] // BPG)
        i += 1
    # 2) free the other region -> same final free-space shape in every run
    other = HIGH if prime_groups is LOW else LOW
    punch_region(fills, other, HIGH_CHUNK if other is HIGH else LOW_CHUNK)
    sh("sync")
    wr(f"{sysd}/mb_max_linear_groups", linear)

    rows = group_summary(dev)
    if show_map:
        print(f"free-space map (blocks left unfilled before punching: {left}):")
        print("  group   free  frags   avg  free-extent orders present")
        for g, free, frags, avg, c in rows:
            orders = ",".join(f"2^{o}x{n}" for o, n in enumerate(c) if n)
            print(f"   {g:2d}  {free:6d}  {frags:4d}  {avg:5d}  {orders}")

    tracing = os.path.isdir(f"{TRACE}/events/ext4/ext4_mballoc_alloc")
    if tracing:
        wr(f"{TRACE}/trace", "")
        wr(f"{TRACE}/events/ext4/ext4_mballoc_alloc/enable", 1)
    write_direct(victim, FILE_MB * 1024, 64)
    allocs = []
    if tracing:
        wr(f"{TRACE}/events/ext4/ext4_mballoc_alloc/enable", 0)
        pat = re.compile(rf"inode {vino} orig (\d+)/\S+ goal \S+ result (\d+)/\d+/(\d+)@\d+ "
                         rf".* cr (\S+) flags \S*STREAM_ALLOC")
        for line in open(f"{TRACE}/trace"):
            m = pat.search(line)
            if m:
                allocs.append((int(m[1]), int(m[2]), int(m[3]), m[4]))

    e = [x for x in extents(victim) if x[0] < FILE_MB * 256]
    lens = [a[2] for a in allocs]
    after = group_summary(dev)
    low_1k_left = sum(c[10] for g, f, fr, a, c in after if g in LOW)
    sh(f"umount {MNT}")
    print(f"\n[{label}] global goal for victim = group {primed.get(vino % nr_goals)}, "
          f"mb_max_linear_groups={linear}")
    print(f"   allocator calls: {len(allocs)}   allocation sizes: "
          f"{ {l: lens.count(l) for l in sorted(set(lens))} }   file extents: {len(e)}")
    trimmed = [a for a in allocs if a[2] < LOW_CHUNK]
    if trimmed:
        print(f"   {len(trimmed)} allocations trimmed below 1024, e.g. "
              f"goal grp {trimmed[0][0]} -> {trimmed[0][2]} blocks in grp {trimmed[0][1]} "
              f"({trimmed[0][3]}); 1024-block extents still free in groups 0-7 "
              f"after the write: {low_1k_left}")
    return len(allocs), len(e)


def main():
    if os.geteuid():
        sys.exit("run as root")
    os.makedirs(WORK, exist_ok=True)
    print(f"kernel {os.uname().release}, {os.cpu_count()} CPUs")
    a = run("A control: goal in group 0, no linear prescan", LOW, 0, show_map=True)
    b = run("B bug: goal in group 8, no linear prescan  ", HIGH, 0)
    c = run("C bug: goal in group 0, default prescan=4  ", LOW, 4)
    print(f"\nSUMMARY (same free space, same writes, {FILE_MB} MiB file):")
    print(f"  A: {a[0]} allocator calls, {a[1]} extents")
    print(f"  B: {b[0]} allocator calls, {b[1]} extents  ({b[0] / max(a[0], 1):.1f}x calls)")
    print(f"  C: {c[0]} allocator calls, {c[1]} extents  ({c[0] / max(a[0], 1):.1f}x calls)")


if __name__ == "__main__":
    main()
