"""Measure this process tree on macOS/Linux with fresh anonymous profiles only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from operators import create_gateway


def rss():
    rows=subprocess.check_output(['ps','-axo','pid=,ppid=,rss='],text=True)
    processes={}
    for row in rows.splitlines():
        pid,ppid,memory=map(int,row.split())
        processes[pid]=(ppid,memory)
    descendants={os.getpid()}
    while True:
        new=descendants | {pid for pid,(parent,_) in processes.items() if parent in descendants}
        if new==descendants:break
        descendants=new
    return round(sum(processes[p][1] for p in descendants if p in processes)/1024,1)


def main():
    os.environ['VK_BROWSER_HEADLESS']='1'
    with tempfile.TemporaryDirectory() as tmp:
        gateway=create_gateway(tmp)
        users=gateway.operators
        users.add('owner','Disposable-test-password',first=True)
        for i in range(4):users.add('operator'+str(i),'Disposable-test-password')
        apps=[gateway.workspaces.get(u) for u in users.users()]
        pool=gateway.workspaces.pool
        report={'operators':len(apps),'note':'Sum of process-tree RSS in MiB; blank browser, no VK/network pages; not a VK load test.'}
        try:
            report['idle_before']=rss()
            pool.call(apps[0].store.browser,'ensure')
            time.sleep(1)
            report['one_blank_browser']=rss()
            pool.call(apps[1].store.browser,'ensure')
            time.sleep(1)
            report['after_operator_switch']=rss()
            pool.call(apps[1].store.browser,'close')
            time.sleep(1)
            report['after_release']=rss()
            report['open_contexts_after_release']=sum(a.store.browser.context is not None for a in apps)
        finally:pool.shutdown()
    out=Path(__file__).resolve().parents[1]/'artifacts/resource-check.json'
    out.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))

if __name__=='__main__':main()
