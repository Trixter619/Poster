"""Offline regression tests for the observed VK suggestion editor."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright
from browser_vk import BrowserVK
from vk_api import VKError, UncertainDelivery


EDITOR = '''
<style>[contenteditable] { display: block; min-height: 30px; min-width: 200px; }</style>
<button data-testid="group_publish_suggest_button" onclick="document.querySelector('#editor').hidden=false">Предложить пост</button>
<button hidden>Предложить новость</button>
<div id="editor" data-testid="posting_modal_box" hidden>
 <span contenteditable="true" role="textbox" data-testid="posting_base_screen_input_message"></span>
 <input type="file" multiple data-testid="posting_base_screen_download_from_device">
 <div data-testid="sortable-carousel-track"></div>
 <button id="layout" onclick="document.querySelector('#grid-option').hidden=false">Карусель</button>
 <button id="grid-option" data-testid="posting_base_screen_view_option" hidden>Сетка</button>
 <button data-testid="posting_base_screen_next">Далее</button>
 <section id="settings" hidden>
  <div data-testid="showmoretext-in"></div><div id="preview"></div>
  <label data-testid="posting_settings_sign_switch">Подпись автора<input type="checkbox" data-testid="posting_settings_sign_switch_input"></label>
  <input type="checkbox" data-testid="posting_settings_repost_to_story_switch" checked>
  <button data-testid="posting_suggest_button" onclick="window.sent++;document.querySelector('#confirmation').innerText='Пост предложен'">Предложить пост</button>
 </section>
</div><div id="confirmation"></div>
<script>
window.sent=0;
const field=document.querySelector('[contenteditable]');
const track=document.querySelector('[data-testid="sortable-carousel-track"]');
document.querySelector('input[type=file]').onchange=e=>{
 for(const file of e.target.files){
  const item=document.createElement('div');item.dataset.testid='posting_attachment_item';
  const img=new Image();img.src=URL.createObjectURL(file);item.append(img);
  const remove=document.createElement('button');remove.dataset.testid='posting_attachment_photo_item_remove';item.append(remove);
  track.append(item);
 }
};
document.querySelector('#grid-option').onclick=()=>{
 document.querySelector('#grid-option').hidden=true;document.querySelector('#layout').innerText='Сетка';
 track.dataset.testid='media-grid';
 for(const item of track.children)item.dataset.testid='media-grid-item';
};
document.querySelector('[data-testid="posting_base_screen_next"]').onclick=()=>{
 document.querySelector('#settings').hidden=false;
 document.querySelector('[data-testid="showmoretext-in"]').innerText=field.innerText;
 const grid=track.dataset.testid==='media-grid';
 if(grid)track.hidden=true;
 for(const image of track.querySelectorAll('img')){
  const item=document.createElement('div');item.dataset.testid='posting_preview_attachment_item';
  if(grid)item.dataset.testid='media-grid-item';
  // VK may render both a blurred placeholder and the actual image.
  item.append(image.cloneNode(),image.cloneNode());document.querySelector('#preview').append(item);
 }
 if(grid)track.remove();
};
</script>
'''


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(Path('.browsers').resolve()))
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vk = BrowserVK(self.temp.name)
        self.page = self.browser.new_page()
        self.vk.page = self.page
        self.page.set_content(EDITOR)
        self.navigate = patch.object(self.vk, '_navigate').start()
        self.group = {'mode': 'browser_suggest'}
        self.draft = {'text': 'Тест объявления\nВторая строка', 'photos': []}

    def tearDown(self):
        patch.stopall()
        self.page.close()
        self.vk.executor.shutdown()
        self.temp.cleanup()

    def test_delayed_new_button_and_hidden_legacy_duplicate(self):
        self.page.get_by_test_id('group_publish_suggest_button').evaluate(
            'e=>{e.hidden=true;setTimeout(()=>e.hidden=false,150)}')
        field, button = self.vk._composer(self.group)
        self.assertTrue(field.is_visible())
        self.assertEqual(button.inner_text(), 'Далее')
        self.assertEqual(self.page.evaluate('sent'), 0)

    def test_eight_photos_signature_no_story_no_submission(self):
        for i in range(8):
            name = f'{i}.png'
            Image.new('RGB', (4, 4), (i, 20, 30)).save(Path(self.temp.name) / name)
            self.draft['photos'].append(name)
        field, nxt = self.vk._composer(self.group)
        submit = self.vk._prepare_modern_suggestion(field, nxt, self.draft, self.temp.name)
        self.assertEqual(submit.inner_text(), 'Предложить пост')
        self.assertTrue(self.page.get_by_test_id('posting_settings_sign_switch_input').is_checked())
        self.assertFalse(self.page.get_by_test_id('posting_settings_repost_to_story_switch').is_checked())
        self.assertEqual(self.page.get_by_test_id('media-grid-item').count(), 8)
        self.assertEqual(self.page.evaluate('sent'), 0)

    def test_existing_draft_preserved(self):
        field, nxt = self.vk._composer(self.group)
        field.fill('Чужой черновик')
        with self.assertRaisesRegex(VKError, 'уже есть'):
            self.vk._prepare_modern_suggestion(field, nxt, self.draft, self.temp.name)
        self.assertEqual(field.inner_text(), 'Чужой черновик')
        self.assertEqual(self.page.evaluate('sent'), 0)

    def test_publish_label_is_rejected(self):
        self.page.get_by_test_id('posting_suggest_button').evaluate('e=>e.innerText="Опубликовать"')
        with self.assertRaisesRegex(VKError, 'не соответствует'):
            self.vk._post(self.group, self.draft, self.temp.name)
        self.assertEqual(self.page.evaluate('sent'), 0)

    def test_new_confirmation_is_recognized(self):
        result = self.vk._post(self.group, self.draft, self.temp.name)
        self.assertEqual(result['status'], 'browser_suggested')
        self.assertEqual(self.page.evaluate('sent'), 1)

    def test_missing_toast_uses_suggested_list_without_second_send(self):
        field, nxt = self.vk._composer(self.group)
        button = self.vk._prepare_modern_suggestion(field, nxt, self.draft, self.temp.name)
        with patch('browser_vk.time') as clock, patch.object(
                self.vk, '_confirm_suggestion', return_value={'url':'https://vk.ru/wall-123?suggested=1'}) as receipt:
            clock.monotonic.side_effect=[0,30]
            clock.time.return_value=1
            result = self.vk._submit(self.group, self.draft, button)
        self.assertEqual(result['status'], 'browser_suggested')
        receipt.assert_called_once()
        self.assertEqual(self.page.evaluate('sent'), 1)

    def test_missing_toast_and_missing_receipt_remain_unknown(self):
        field, nxt = self.vk._composer(self.group)
        button = self.vk._prepare_modern_suggestion(field, nxt, self.draft, self.temp.name)
        with patch('browser_vk.time') as clock, patch.object(
                self.vk, '_confirm_suggestion', return_value=None):
            clock.monotonic.side_effect=[0,30]
            clock.time.return_value=1
            with self.assertRaises(UncertainDelivery):
                self.vk._submit(self.group, self.draft, button)
        self.assertEqual(self.page.evaluate('sent'), 1)

    def test_unknown_delivery_is_not_retried_or_reported_as_unsent(self):
        with patch.object(self.vk, '_submit', side_effect=UncertainDelivery('Проверь VK')) as submit:
            with self.assertRaises(UncertainDelivery):
                self.vk._post(self.group, self.draft, self.temp.name)
        self.assertEqual(submit.call_count, 1)

    def test_legacy_suggestion_still_opens(self):
        self.page.set_content('''<button onclick="document.querySelector('#legacy').hidden=false">Предложить новость</button>
        <div id="legacy" hidden><div id="post_field" contenteditable="true"></div><button id="send_post">Предложить</button></div>''')
        field, button = self.vk._composer(self.group)
        self.assertEqual(field.get_attribute('id'), 'post_field')
        self.assertEqual(button.inner_text(), 'Предложить')


if __name__ == '__main__':
    unittest.main()
