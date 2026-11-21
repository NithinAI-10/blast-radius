import resource,subprocess,sys,time
t=time.time()
r=subprocess.run(sys.argv[1:])
u=resource.getrusage(resource.RUSAGE_CHILDREN)
print(f"CPU={u.ru_utime+u.ru_stime:.2f}s wall={time.time()-t:.2f}s rc={r.returncode}",file=sys.stderr)
