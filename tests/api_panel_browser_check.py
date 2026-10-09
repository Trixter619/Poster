"""Isolated API-only panel smoke test; Chromium is the test client, not the bot."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import requests
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory() as tmp:
    env=dict(os.environ,VK_POSTER_DATA=tmp,VK_POSTER_PORT='18790',VK_POSTER_LEGACY_BROWSER='0')
    env.pop('VK_POSTER_PUBLIC_HOST',None)
    process=subprocess.Popen([sys.executable,'app.py'],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                if requests.get('http://127.0.0.1:18790/api/health',timeout=1).ok:break
            except requests.RequestException:pass
            time.sleep(.1)
        with sync_playwright() as pw:
            browser=pw.chromium.launch()
            page=browser.new_page(viewport={'width':1280,'height':900})
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto('http://127.0.0.1:18790/')
            page.get_by_label('Логин').fill('apitest')
            page.get_by_label('Пароль',exact=True).fill('Only-test-password-2026')
            page.locator('button[type=submit]').click()
            expect(page.locator('#postText')).to_be_visible()
            page.locator('#postText').fill('Сохранённое объявление')
            page.locator('#saveDraft').click()
            expect(page.locator('#saveStatus')).to_contain_text('Сохранено')
            page.locator('[data-tab=groups]').click()
            page.locator('#groupUrl').fill('https://vk.ru/club241712258')
            page.locator('#addGroup').click()
            expect(page.locator('.group-card')).to_have_count(1)
            expect(page.locator('[name=mode]')).to_have_value('api')
            page.locator('[name=hours]').fill('72')
            page.locator('[data-op=save]').click()
            expect(page.locator('#notice')).to_contain_text('Настройки сохранены')
            expect(page.locator('#pauseAll')).to_be_disabled()
            expect(page.get_by_role('button',name='Отправка пока недоступна')).to_be_disabled()
            page.screenshot(path=str(ROOT/'artifacts/api-only-panel.png'),full_page=True)
            page.locator('#accountBadge').click()
            expect(page.get_by_role('heading',name='Подключение VK API')).to_be_visible()
            expect(page.locator('#remoteImage')).to_have_count(0)
            assert not errors,errors
            browser.close()
        print('PASS: API-only panel, draft/group/interval editing, blocked sending, setup requirements, no remote browser UI')
    finally:
        process.terminate()
        try:process.wait(timeout=15)
        except subprocess.TimeoutExpired:process.kill();process.wait()
