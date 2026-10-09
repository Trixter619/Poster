"""Real panel test with empty temporary data; no VK access."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import requests
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, VK_POSTER_DATA=tmp, VK_POSTER_PORT='18789', VK_BROWSER_HEADLESS='1')
        env.pop('VK_ACCESS_TOKEN', None)
        code = """from browser_vk import BrowserVK
from app import run
def fixture_open(self):
 self._ensure()
 self.context.route('https://vk.ru/**',lambda r:r.fulfill(body='<input type="tel" oninput="document.getElementById(123).hidden=false;this.hidden=true;console.log(123)" style="position:absolute;left:0;top:0;width:400px;height:60px"><div id="123" hidden><a id="top_profile_link">Профиль</a></div>',content_type='text/html'))
 self.page.goto('https://vk.ru/')
 if not getattr(self, '_test_opened', False): self.auth_api_state='rejected'
 self.page.on("console",lambda message: setattr(self,"auth_api_state","accepted") if message.text=="123" else None)
 self._test_opened=True
 return self.info()
BrowserVK._open=fixture_open
run()
"""
        server = subprocess.Popen([sys.executable, '-c', code], cwd=ROOT, env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                try:
                    if requests.get('http://127.0.0.1:18789/api/health',timeout=1).ok:break
                except requests.RequestException:pass
                if server.poll() is not None:raise RuntimeError('test server exited')
                time.sleep(.1)
            with sync_playwright() as p:
                browser = p.chromium.launch()
                owner = browser.new_context(viewport={'width':1280,'height':900})
                page = owner.new_page()
                errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto('http://127.0.0.1:18789/')
                expect(page).to_have_url('http://127.0.0.1:18789/setup')
                page.get_by_label('Логин').fill('owner')
                page.get_by_label('Пароль',exact=True).fill('Test-owner-password-2026')
                page.get_by_role('button',name='Создать и войти').click()
                expect(page.locator('#postText')).to_be_visible()
                page.locator('#postText').fill('Объявление первого оператора')
                page.locator('#saveDraft').click()
                expect(page.locator('#saveStatus')).to_have_text('Сохранено')
                page.get_by_role('link',name='Учётная запись и операторы').click()
                page.get_by_label('Логин',exact=True).fill('second')
                page.get_by_label('Начальный пароль').fill('Test-second-password-2026')
                page.get_by_role('button',name='Создать оператора').click()
                expect(page.get_by_role('status')).to_contain_text('Оператор добавлен')
                artifacts=ROOT/'artifacts';artifacts.mkdir(exist_ok=True)
                page.screenshot(path=str(artifacts/'operators-desktop.png'),full_page=True)
                page.set_viewport_size({'width':390,'height':844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(artifacts/'operators-mobile.png'),full_page=True)
                other=browser.new_context();second=other.new_page()
                second.goto('http://127.0.0.1:18789/')
                second.get_by_label('Логин').fill('second')
                second.get_by_label('Пароль',exact=True).fill('Test-second-password-2026')
                second.get_by_role('button',name='Войти',exact=True).click()
                expect(second.locator('#postText')).to_have_value('')
                second.locator('#postText').fill('Текст второго оператора')
                second.locator('#saveDraft').click()
                expect(second.locator('#saveStatus')).to_have_text('Сохранено')
                page.goto('http://127.0.0.1:18789/')
                expect(page.locator('#postText')).to_have_value('Объявление первого оператора')
                second.goto('http://127.0.0.1:18789/account')
                expect(second.get_by_role('button',name='Создать оператора')).to_have_count(0)
                candidate=browser.new_context().new_page()
                candidate.goto('http://127.0.0.1:18789/register')
                candidate.get_by_label('Логин').fill('candidate')
                candidate.get_by_label('Пароль',exact=True).fill('Candidate-test-password-2026')
                candidate.get_by_role('button',name='Отправить заявку').click()
                expect(candidate.get_by_role('status')).to_contain_text('после одобрения')
                page.goto('http://127.0.0.1:18789/account')
                page.get_by_role('button',name='Одобрить доступ').click()
                expect(page.get_by_role('status')).to_contain_text('Доступ одобрен')
                candidate.goto('http://127.0.0.1:18789/login')
                candidate.get_by_label('Логин').fill('candidate')
                candidate.get_by_label('Пароль',exact=True).fill('Candidate-test-password-2026')
                candidate.get_by_role('button',name='Войти',exact=True).click()
                expect(candidate.locator('#postText')).to_have_value('')
                # The single panel button opens and starts the login flow.
                page.goto('http://127.0.0.1:18789/')
                with owner.expect_page() as popup:
                    page.locator('#accountBadge').click()
                remote=popup.value
                expect(remote.locator('#remoteImage')).to_be_visible(timeout=15000)
                expect(remote.get_by_role('button',name='Вход завершён',exact=True)).to_have_count(0)
                intruder=other.new_page()
                intruder.goto('http://127.0.0.1:18789/vk-login')
                expect(intruder.locator('#remoteImage')).to_be_visible(timeout=15000)
                box=remote.locator('#remoteImage').bounding_box()
                remote.locator('#remoteImage').click(position={'x':box['width']*50/1360,'y':box['height']*30/960})
                remote.locator('#mobileControls summary').click()
                with remote.expect_request(lambda r:r.url.endswith('/api/browser/input-batch') and 'test-mobile-input' in (r.post_data or '')):
                    remote.get_by_label('Ввод в выбранное поле VK').fill('test-mobile-input')
                    remote.get_by_role('button',name='Ввести',exact=True).click()
                # Completion closes the popup and updates the original panel.
                expect(page.locator('#accountBadge')).to_contain_text('Вход в VK подтверждён',timeout=15000)
                page.wait_for_function('true')
                assert remote.is_closed(), 'login popup should close automatically'
                expect(intruder.locator('#remoteImage')).to_be_visible()
                assert not errors, errors
                browser.close()
            print('PASS: setup, login, operator creation, separate drafts, admin permissions, access request/approval, private VK web screen/input, desktop/mobile UI')
        finally:
            server.terminate()
            try:server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill();server.wait()

if __name__ == '__main__':main()
