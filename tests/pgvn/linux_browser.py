"""Real Chromium against a byte-for-byte forward to the isolated Linux monitor.

Only a temporary host loopback socket is opened. Docker publishes no ports.
The bridge preserves Host/Origin and never records credentials or HTTP bytes.
"""
from pathlib import Path
import json
import re
import socket
import subprocess
import threading
import time


def execute(args,docker,container,work,env,root):
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(16);listener.settimeout(.1)
    port=listener.getsockname()[1];stopping=threading.Event();bridges=[];connections=[];threads=[]
    def relay(source,target):
        try:
            while not stopping.is_set():
                data=source.read1(65536)
                if not data:break
                target.sendall(data)
        except (OSError,ValueError):pass
    def handle(sock):
        bridge=subprocess.Popen(docker+['exec','-i',container,'python3',str(root/'tests/pgvn/linux_browser_bridge.py'),str(port)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env)
        bridges.append(bridge);connections.append(sock)
        thread=threading.Thread(target=relay,args=(bridge.stdout,sock),daemon=True);thread.start();threads.append(thread)
        try:
            while not stopping.is_set():
                data=sock.recv(65536)
                if not data:break
                bridge.stdin.write(data);bridge.stdin.flush()
        except (OSError,ValueError):pass
        finally:
            bridge.stdin.close();sock.close()
    def accept():
        while not stopping.is_set():
            try:sock,_=listener.accept()
            except socket.timeout:continue
            except OSError:break
            thread=threading.Thread(target=handle,args=(sock,),daemon=True);threads.append(thread);thread.start()
    thread=threading.Thread(target=accept,daemon=True);thread.start()
    output=work/'client-container.stdout';error=work/'client-container.stderr'
    process=None;browser_report={'status':'FAIL'}
    try:
        with output.open('w') as stdout,error.open('w') as stderr:
            process=subprocess.Popen(list(map(str,[*args,port])),cwd=root,env=env,stdout=stdout,stderr=stderr)
            state=work/'client/browser-state.json';result=work/'client/browser-result.json';deadline=time.monotonic()+600
            while not state.exists():
                if process.poll() is not None:raise RuntimeError('Native client failed before browser phase: '+error.read_text())
                if time.monotonic()>=deadline:raise RuntimeError('Browser fixture timed out')
                time.sleep(.1)
            issued=json.loads(state.read_text());state.unlink()
            try:
                browser_report=probe('http://127.0.0.1:'+str(port)+'/monitor/',issued['token'],work)
                result.write_text(json.dumps(browser_report))
            except Exception:
                result.write_text(json.dumps({'status':'FAIL'}));raise
            process.wait(timeout=120)
            if process.returncode:raise RuntimeError('Native client failed: '+error.read_text())
        return output.read_text(),browser_report
    finally:
        stopping.set();listener.close()
        for sock in connections:
            try:sock.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            sock.close()
        for bridge in bridges:
            if bridge.poll() is None:bridge.terminate()
        for bridge in bridges:
            try:bridge.wait(timeout=2)
            except subprocess.TimeoutExpired:bridge.kill();bridge.wait()
        if process and process.poll() is None:process.terminate();process.wait(timeout=10)
        thread.join(timeout=1)
        (work/'browser-summary.json').write_text(json.dumps(browser_report,indent=2)+'\n')


def probe(url,token,work):
    from playwright.sync_api import sync_playwright,expect
    errors=[];checks=[]
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        try:
            context=browser.new_context(viewport={'width':1365,'height':900})
            page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(url);expect(page.locator('#token')).to_be_visible()
            page.locator('#token').fill(token);page.locator('#token-form button').click()
            expect(page.locator('#content')).to_be_visible(timeout=20000)
            expect(page.locator('#login')).to_be_hidden();checks.append('real token login')
            for view in ('overview','nodes','repositories','database','requests'):
                page.locator('[data-view="'+view+'"]').click()
                expect(page.locator('[data-view="'+view+'"]')).to_have_attribute('aria-current','page')
                expect(page.locator('#content')).not_to_have_attribute('aria-busy','true',timeout=20000)
                expect(page.locator('#content')).to_be_visible()
                expect(page.locator('#content')).to_have_text(re.compile(r'.{20}',re.S),timeout=20000)
                checks.append('real '+view+' view')
            page.locator('#theme').click();page.locator('#pause').click();checks.append('theme and pause')
            page.screenshot(path=str(work/'monitor-desktop.png'),full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth+1')
            page.screenshot(path=str(work/'monitor-mobile.png'),full_page=True);checks.append('mobile layout')
            assert page.evaluate('Object.keys(localStorage).length+Object.keys(sessionStorage).length')==0
            page.reload();expect(page.locator('#token')).to_be_visible();checks.append('reload clears token')
            assert not errors,errors
            return {'status':'PASS','browser':browser.version,'checks':checks,'page_errors':errors,'transport':'opaque host loopback/stdin bridge; no Docker published ports'}
        except Exception:
            (work/'browser-failure.json').write_text(json.dumps({'page_errors':errors,'body':page.locator('body').inner_text()},ensure_ascii=False,indent=2)+'\n')
            page.screenshot(path=str(work/'monitor-failure.png'),full_page=True)
            raise
        finally:browser.close()
