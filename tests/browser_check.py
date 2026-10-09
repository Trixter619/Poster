"""Isolated UI smoke test; never uses VK credentials or the real data directory."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

import requests
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory() as temp:
        data = Path(temp) / 'data'
        data.mkdir()
        # Never copy the authenticated browser profile into a test fixture.
        shutil.copytree(ROOT / 'data/photos', data / 'photos')
        shutil.copyfile(ROOT / 'data/state.json', data / 'state.json')
        # Retain only the supplied draft in the disposable fixture.
        import json
        state = json.loads((data / 'state.json').read_text())
        state.update(groups=[], history=[], tasks=[], revision=0)
        (data / 'state.json').write_text(json.dumps(state))
        env = dict(os.environ)
        env.pop('VK_ACCESS_TOKEN', None)
        env.update(VK_POSTER_DATA=str(data), VK_POSTER_PORT='8789')
        server = subprocess.Popen([sys.executable, '-c', "import os; from app import create_app; from waitress import serve; app=create_app(os.environ['VK_POSTER_DATA']); app.store.browser.call=lambda *args: {'composer_found':True,'message':'Подготовка проверена без отправки'}; serve(app,host='127.0.0.1',port=8789)"], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(50):
                try:
                    if requests.get('http://127.0.0.1:8789', timeout=1).status_code == 200:
                        break
                except requests.RequestException:
                    time.sleep(.2)
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(viewport={'width':1440,'height':1100}, device_scale_factor=1)
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto('http://127.0.0.1:8789')
                expect(page.locator('#photos img')).to_have_count(8)
                assert 'S.A.G' in page.locator('#postText').input_value()
                page.wait_for_load_state('networkidle')
                assert page.locator('img').evaluate_all('(images) => images.every(i => i.complete && i.naturalWidth > 0)')
                (ROOT / 'artifacts').mkdir(exist_ok=True)
                page.screenshot(path=str(ROOT/'artifacts/interface.png'), full_page=True)
                original = page.locator('#postText').input_value()
                page.locator('#postText').fill(original + '\nТест редактирования')
                page.locator('#photos [data-photo=right]').first.click()
                page.locator('#saveDraft').click()
                expect(page.locator('#saveStatus')).to_have_text('Сохранено')
                page.reload()
                expect(page.locator('#photos img')).to_have_count(8)
                assert page.locator('#postText').input_value().endswith('Тест редактирования')
                image_path = next((data/'photos').glob('*.jpg'))
                page.locator('#photos [data-photo=replace]').first.click()
                page.locator('#replaceInput').set_input_files(str(image_path))
                expect(page.locator('#notice')).to_contain_text('Фото загружены')
                expect(page.locator('#photos img')).to_have_count(8)
                page.locator('#photos [data-photo=delete]').last.click()
                expect(page.locator('#photos img')).to_have_count(7)
                page.locator('#photoInput').set_input_files(str(image_path))
                expect(page.locator('#photos img')).to_have_count(8)
                page.locator('#saveDraft').click()
                expect(page.locator('#saveStatus')).to_have_text('Сохранено')
                page.locator('[data-tab=groups]').click()
                page.locator('#groupUrl').fill('https://vk.com/club160305394')
                page.locator('#addGroup').click()
                expect(page.locator('.group-card')).to_have_count(1)
                expect(page.locator('[name=mode] option')).to_have_count(1)
                expect(page.locator('[name=mode]')).to_have_value('browser_suggest')
                expect(page.locator('#notice')).to_contain_text('Подготовка проверена')
                page.locator('[name=hours]').fill('48')
                page.locator('[name=enabled]').check()
                page.locator('[data-op=save]').click()
                expect(page.locator('#notice')).to_contain_text('Группа проверена')
                expect(page.locator('#automationStatus')).to_have_text('Запущена')
                page.locator('#pauseAll').click()
                expect(page.locator('#automationStatus')).to_have_text('Остановлена')
                expect(page.locator('[name=enabled]')).to_be_checked()
                page.locator('#pauseAll').click()
                expect(page.locator('#automationStatus')).to_have_text('Запущена')
                page.reload();page.locator('[data-tab=groups]').click()
                expect(page.locator('[name=hours]')).to_have_value('48')
                page.locator('[data-op=browser-check]').click()
                expect(page.locator('#notice')).to_contain_text('Подготовка проверена')
                expect(page.locator('[name=enabled]')).to_be_checked()
                expect(page.locator('[data-tab=settings]')).to_have_count(0)
                page.locator('[data-tab=history]').click()
                live=requests.get('http://127.0.0.1:8789/api/state').json()
                live['history']=[{'id':'fixture','time':time.time(),'group_name':'Test','status':'published','detail':'Опубликована','url':'https://vk.ru/wall-160305394_99','publication':{'state':'found','url':'https://vk.ru/wall-160305394_99','checked_at':time.time(),'detail':'Совпадение найдено'}}]
                page.route('**/api/state', lambda route: route.fulfill(json=live))
                page.locator('#refreshHistory').click()
                expect(page.locator('.publication.found a')).to_have_attribute('href','https://vk.ru/wall-160305394_99')
                page.unroute('**/api/state')
                page.set_viewport_size({'width':390,'height':844})
                page.locator('[data-tab=editor]').click()
                page.locator('#notice').evaluate('(el) => el.hidden = true')
                page.screenshot(path=str(ROOT/'artifacts/mobile.png'), full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Mobile horizontal overflow'
                page.locator('[data-tab=history]').click()
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'History mobile overflow'
                assert not errors, errors
                browser.close()
                print('Browser checks passed: draft persistence, upload/replace/reorder/remove photos, groups, intervals, errors, mobile layout.')
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__ == '__main__':
    main()
