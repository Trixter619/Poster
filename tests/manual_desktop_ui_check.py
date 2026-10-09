"""Opening the login UI must not start account verification by itself."""
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parents[1]
with sync_playwright() as p:
 browser=p.chromium.launch(headless=True)
 page=browser.new_page();calls=[];errors=[]
 page.on('pageerror',lambda e:errors.append(str(e)))
 def route(r):
  path=r.request.url.split('http://localhost',1)[-1]
  if path=='/vk-login':return r.fulfill(content_type='text/html',body=(root/'templates/vk-desktop.html').read_text().replace('{{ csrf }}','fixture'))
  if path.startswith('/static/'):
   f=root/path[1:]
   return r.fulfill(content_type='text/javascript' if f.suffix=='.js' else 'text/css',body=f.read_text())
  if path=='/desktop-client/core/rfb.js':return r.fulfill(content_type='text/javascript',body="export default class RFB extends EventTarget {constructor(){super();setTimeout(()=>this.dispatchEvent(new Event('connect')),30);}disconnect(){}sendKey(){}}")
  if path.startswith('/api/browser/'):
   action=path.rsplit('/',1)[-1];calls.append(action)
   return r.fulfill(json={'state':'session_rejected' if action=='connect-status' else 'waiting','message':'fixture'})
  r.fulfill(status=404)
 page.route('http://localhost/**',route)
 page.goto('http://localhost/vk-login');page.wait_for_timeout(3500)
 assert calls[:2]==['connect','desktop'],calls
 assert 'desktop-heartbeat' in calls,calls
 assert 'desktop-verify' not in calls and 'connect-status' not in calls,calls
 page.locator('#desktopCheck').click();page.wait_for_timeout(6500)
 assert calls.count('desktop-verify')==1,calls
 assert calls.count('connect-status')==1,calls
 assert calls.count('desktop-release')==1,calls
 assert calls[-1]=='desktop-heartbeat',calls
 assert not errors,errors
 print('MANUAL_VERIFY_ONLY_AND_RELEASE_ON_FAILURE_OK')
 browser.close()
