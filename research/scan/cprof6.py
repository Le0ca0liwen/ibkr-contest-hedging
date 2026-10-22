import os, sys, cProfile, pstats, warnings, io
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
from hedge.optimizer import HedgeOptimizer
from hedge.scenarios import ScenarioEngine
from hedge.universe import screen_options
n=int(sys.argv[1])
names=(MANDATED+extra_names(max(0,n-3)))[:n]
hist=OUT/f"history_{n}.csv"
m,names=build_manifest(n,hist)
now=now_at(10,30); q=make_quotes(m,names,now); s=make_snapshot(m,q,now)
ids=screen_options(m,q,s.quantities()); sc=ScenarioEngine(m).build(s,q,now,ids)
opt=HedgeOptimizer(m)
warnings.simplefilter("ignore")
pr=cProfile.Profile(); pr.enable()
t=opt.solve(s,q,sc,1000095.5,float(s.nav))
pr.disable()
print(t.status, t.solver_metrics.get("total_ms"))
st=pstats.Stats(pr); st.sort_stats("cumulative")
out=io.StringIO(); st.stream=out; st.print_stats(45); print(out.getvalue()[:9000])
