"""Run an MLX module (``mlx_lm.lora`` …) with the MLX allocator reined in.

Two limits, per the mlx.core docs:

- ``set_memory_limit`` is only a scheduling *guideline*: allocations beyond it
  still succeed until RAM + swap run out, which can hard-reset a small-RAM Mac
  before MLX ever raises. It throttles spikes but does not cap the footprint.
- ``set_cache_limit`` IS enforced (freed buffers beyond it are reclaimed on the
  next allocation) and defaults to the memory limit, so variable-length training
  piles up gigabytes of dead buffers unless it is capped. This is the limit that
  actually bounds growth.

Usage: python mlx_shim.py <module> [module args...]
Env:   VW_FT_MEM_LIMIT_GB    guideline in GiB; "0" disables; default 75% of RAM
       VW_FT_CACHE_LIMIT_GB  buffer-cache cap in GiB; "0" disables the cache; default 1
"""

import os
import runpy
import sys


def main() -> None:
    import mlx.core as mx

    raw = os.environ.get("VW_FT_MEM_LIMIT_GB", "").strip()
    if raw:
        gb = float(raw)
    else:
        ram = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        gb = ram * 0.75 / 1024**3
    if gb > 0:
        mx.set_memory_limit(int(gb * 1024**3))
        print(f"[vw] mlx memory limit {gb:.1f} GB", flush=True)
    cache_raw = os.environ.get("VW_FT_CACHE_LIMIT_GB", "").strip()
    cache_gb = float(cache_raw) if cache_raw else 1.0
    mx.set_cache_limit(int(cache_gb * 1024**3))
    print(f"[vw] mlx cache limit {cache_gb:.1f} GB", flush=True)
    module = sys.argv[1]
    del sys.argv[0]  # argv[1:] becomes the module's own argv for argparse
    runpy.run_module(module, run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
