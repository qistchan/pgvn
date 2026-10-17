"""Matched off/local experiments over real SVN clients and two native nodes."""
from pathlib import Path
import concurrent.futures,hashlib,json,math,os,resource,statistics,subprocess,time


def measure(work,env,product,urls,password,expected,headers,get,restart,monitor_port):
    expected_hash=hashlib.sha256(expected).hexdigest();groups=[]
    def read(index):
        arguments=['/usr/bin/svn','cat',urls[index%2]+'/trunk/cache.txt','--non-interactive','--no-auth-cache',
                   '--config-dir',str(work/'benchmark-svn-config'),'--username','writer','--password-from-stdin']
        started=time.perf_counter()
        result=subprocess.run(arguments,input=(password+'\n').encode(),capture_output=True,env=env,timeout=40)
        digest=hashlib.sha256(result.stdout).hexdigest()
        return {'elapsed_ms':(time.perf_counter()-started)*1000,'exit_code':result.returncode,
                'bytes':len(result.stdout),'sha256':digest,'ok':result.returncode==0 and digest==expected_hash,
                'error':result.stderr.decode(errors='replace') if result.returncode else None}
    def metrics():
        totals={}
        for i in range(2):
            status,body=get(monitor_port+i,'/metrics',headers)
            if status!=200:raise RuntimeError('Benchmark monitor token not ready')
            for line in body.decode().splitlines():
                if not line or line.startswith('#'):continue
                key,value=line.rsplit(' ',1);key=key.split('{',1)[0]
                if key.startswith(('pgvn_sql_','pgvn_cache_local_','pgvn_svn_bytes_')):totals[key]=totals.get(key,0)+float(value)
        return totals
    def cpu(pids):
        total=0
        for pid in pids:
            stat=Path('/proc/'+str(pid)+'/stat').read_text();fields=stat[stat.rfind(')')+2:].split()
            total+=int(fields[11])+int(fields[12])
        return total/os.sysconf('SC_CLK_TCK')
    for round_number in range(1,4):
        order=('off','local') if round_number%2 else ('local','off')
        for mode in order:
            pids=restart(mode)
            # Warm the same PG objects in both modes; local additionally warms L1.
            for i in range(4):
                if not read(i)['ok']:raise RuntimeError('Benchmark warm-up content mismatch')
            for _ in range(100):
                try:metrics();break
                except RuntimeError:time.sleep(.1)
            for concurrency,repetitions in ((1,8),(8,2),(32,1)):
                before=metrics();server_before=cpu(pids);usage=resource.getrusage(resource.RUSAGE_CHILDREN)
                client_before=usage.ru_utime+usage.ru_stime;started=time.perf_counter()
                with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
                    rows=list(pool.map(read,range(concurrency*repetitions)))
                elapsed=time.perf_counter()-started;usage=resource.getrusage(resource.RUSAGE_CHILDREN)
                after=metrics();latencies=sorted(row['elapsed_ms'] for row in rows)
                groups.append({'round':round_number,'mode':mode,'concurrency':concurrency,'samples':rows,
                    'errors':sum(not row['ok'] for row in rows),'operations':len(rows),'throughput':len(rows)/elapsed,
                    'p50_ms':statistics.median(latencies),'p95_ms':latencies[math.ceil(len(rows)*.95)-1],
                    'p99_ms':latencies[math.ceil(len(rows)*.99)-1],'client_cpu_seconds':usage.ru_utime+usage.ru_stime-client_before,
                    'server_cpu_seconds':cpu(pids)-server_before,'counters':{key:after[key]-before.get(key,0) for key in after}})
    report={'format':1,'status':'PASS' if all(not group['errors'] for group in groups) else 'FAIL','groups':groups,
            'dataset':{'bytes':len(expected),'sha256':expected_hash,'seed':'repeated pgvn-cache-seed-v1 LF, truncated to 512 KiB'},
            'product_manifest_sha256':hashlib.sha256((product/'build-manifest.json').read_bytes()).hexdigest(),
            'method':{'rounds':3,'concurrency':[1,8,32],'nodes':2,'transport':'native SVN; fresh CLI session per read',
                      'cache_state':'same PG objects prewarmed; fresh node contexts per mode/round; 2 warm-up reads per node',
                      'limits':'shared cloud host, local PG network namespace; no remote RTT, PG CPU/I/O attribution, HTTP or Polarion performance claim'}}
    (work/'wire-benchmark.json').write_text(json.dumps(report,indent=2)+'\n')
    if report['status']!='PASS':raise RuntimeError('Wire performance experiment returned an error or wrong content')
    return report
