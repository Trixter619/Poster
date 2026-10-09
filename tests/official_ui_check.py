import os,tempfile,threading
from pathlib import Path
from unittest.mock import Mock
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright,expect
from operators import create_gateway

with tempfile.TemporaryDirectory() as tmp:
 app=create_gateway(tmp)
 fake=Mock();fake.group.return_value=dict(vk_id=123,name='Тестовая страница',can_post=0,wall=2,type='page');fake.call.return_value={'items':[]}
 app.workspaces.vkid.client=lambda uid:fake
 server=make_server('127.0.0.1',18791,app,threaded=True);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
 try:
  with sync_playwright() as pw:
   b=pw.chromium.launch();page=b.new_page(viewport={'width':1280,'height':900});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
   page.goto('http://127.0.0.1:18791/setup');page.get_by_label('Логин').fill('testowner');page.get_by_label('Пароль',exact=True).fill('Test-password-2026');page.locator('button[type=submit]').click()
   expect(page.locator('#postText')).to_be_visible();page.locator('#postText').fill('Проверка API панели');page.locator('#photoLayout').select_option('carousel');page.locator('#saveDraft').click();expect(page.locator('#saveStatus')).to_have_text('Сохранено')
   page.locator('[data-tab=groups]').click();page.locator('#groupUrl').fill('https://vk.ru/club123');page.locator('#addGroup').click()
   expect(page.locator('.group-card')).to_have_count(1);expect(page.locator('[data-op=send]')).to_be_enabled();expect(page.locator('[name=mode]')).to_have_count(0)
   expect(page.locator('a[href="/vk-login"]')).to_have_count(0);page.locator('[name=display-name]').fill('Моё название');page.locator('[data-op=rename]').click();expect(page.locator('.group-name')).to_have_text('Моё название');page.locator('[name=next]').fill('2020-01-01T00:00');page.locator('[data-op=api-check]').click();expect(page.locator('#notice')).to_contain_text('Права API и данные группы проверены')
   page.screenshot(path=str(Path(__file__).resolve().parents[1]/'artifacts/official-api-panel.png'),full_page=True)
   page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(Path(__file__).resolve().parents[1]/'artifacts/official-api-panel-mobile.png'),full_page=True)
   assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'mobile overflow'
   page.goto('http://127.0.0.1:18791/');expect(page.locator('#photoLayout')).to_have_value('carousel')
   page.get_by_role('link',name='Заявки и операторы ↗',exact=True).click();expect(page.get_by_role('heading',name='Заявки и операторы',exact=True)).to_be_visible()
   page.screenshot(path=str(Path(__file__).resolve().parents[1]/'artifacts/admin-panel-mobile.png'),full_page=True)
   owner=app.operators.users()[0]['id'];oid=app.operators.add('recoverytest','Synthetic-password-2026',actor=owner);token=app.operators.issue_reset(oid,owner)
   page.goto('http://127.0.0.1:18791/reset-password#'+token);page.locator('[name=password]').fill('Synthetic-new-password-2026');page.locator('[name=password_confirm]').fill('Synthetic-new-password-2026');assert '#' not in page.url
   page.get_by_role('button',name='Сохранить новый пароль').click();expect(page.get_by_role('status')).to_contain_text('Пароль изменён')
   assert not errors,errors
   fake.post.assert_not_called();b.close()
  print('PASS: isolated API panel, group check, retired UI absent, desktop/mobile, no JS errors, no posts')
 finally:server.shutdown();app.workspaces.pool.shutdown()
