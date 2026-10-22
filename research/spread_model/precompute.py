import sys, time
from common import build_cache, SESSIONS
for d in sys.argv[1:] or SESSIONS:
    t = time.time(); p = build_cache(d); print(d, p, round(time.time() - t, 1), flush=True)
