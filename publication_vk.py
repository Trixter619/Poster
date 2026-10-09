"""Read-only wall matching. A match is evidence of a public post, not an admin decision."""
from datetime import datetime
import json
import re
import time
from zoneinfo import ZoneInfo

from vk_api import VKError

MONTHS = {'янв': 1, 'фев': 2, 'мар': 3, 'апр': 4, 'мая': 5, 'июн': 6,
          'июл': 7, 'авг': 8, 'сен': 9, 'окт': 10, 'ноя': 11, 'дек': 12}
POSTS = '[data-testid="post"][data-post-nesting-lvl="0"], div[id^="post-"]'


def normalize(text):
    text = re.sub(r'https?://(?:www\.)?', '', text)
    text = re.sub(r'\bvk\.ru/', 'vk.com/', text)
    return ' '.join(text.replace('\u200b', '').split())


def date_timestamp(text, now, timezone):
    # Full tooltip date, to the minute. Never infer age from an undated post.
    m = re.search(r'(\d{1,2})\s+([а-я]+)\.?\s*(\d{4})?\s+в\s+(\d{1,2}):(\d{2})', text.lower())
    if not m or m[2][:3] not in MONTHS:
        return None
    current = datetime.fromtimestamp(now, ZoneInfo(timezone))
    year = int(m[3]) if m[3] else current.year
    try:
        date = datetime(year, MONTHS[m[2][:3]], int(m[1]), int(m[4]), int(m[5]), tzinfo=current.tzinfo)
        if not m[3] and date.timestamp() > now + 86400:
            date = date.replace(year=year - 1)
        return date.timestamp()
    except ValueError:
        return None


# Work on a DOM clone: no changes to the page or the published post.
TEXT = r'''el => {
 const e=el.cloneNode(true);
 e.querySelectorAll('[data-testid="post-footer-author"], [data-id="showMoreButton"], .wall_post_more').forEach(x=>x.remove());
 e.querySelectorAll('a').forEach(a=>{
   const label=a.textContent.trim();
   if(!/^(https?:\/\/|(?:www\.)?vk\.(?:com|ru)\/)/i.test(label))return;
   try {let u=new URL(a.getAttribute('href'),location.origin);
     if(u.pathname==='/away.php' && u.searchParams.get('to'))u=new URL(u.searchParams.get('to'));
     a.textContent=u.href;
   }catch{}
 });
 e.querySelectorAll('br').forEach(x=>x.replaceWith('\n'));
 e.querySelectorAll('div,p').forEach(x=>{x.prepend('\n');x.append('\n');});
 return e.textContent;
}'''


def scan_wall(page, watch, max_pages=8, suggested=False):
    wanted = normalize(watch['text'])
    if not wanted:
        raise VKError('Для поиска нужна сохранённая версия текста; объявления только с фото автоматически не сопоставляются.')
    owner = re.search(r'(?:club|public)(\d+)(?:[/?#]|$)', watch.get('group_url', page.url))
    expected_owner = int(owner[1]) if owner else None
    if expected_owner is None:
        # A named community identifies itself in each original post's header.
        # Resolve only when the header link is exactly the opened group's path.
        owners = page.locator(POSTS).evaluate_all(r'''es=>{
          const path=location.pathname.replace(/\/$/,'');const ids=[];
          for(const e of es){const a=e.querySelector('[data-testid="post-header-title"], .author');
            const id=(e.getAttribute('data-post-id')||e.id.replace(/^post/,'')).match(/^-(\d+)_\d+$/);
            if(a&&id&&new URL(a.getAttribute('href'),location.origin).pathname.replace(/\/$/,'')===path)ids.push(Number(id[1]));
          }return [...new Set(ids)];}''')
        if len(owners) != 1:
            raise VKError('Не удалось определить ID стены. Для проверки используй числовую ссылку группы вида https://vk.ru/club123.')
        expected_owner = owners[0]
    timezone = page.evaluate('Intl.DateTimeFormat().resolvedOptions().timeZone')
    seen, matches = set(), []
    unreadable = 0
    try:
        page.locator(POSTS).first.wait_for(timeout=10000)
    except Exception:
        raise VKError('На странице не распознаны публичные записи. Проверка не подтверждает отказ модератора.') from None
    for _ in range(max_pages):
        fresh = 0
        for post in page.locator(POSTS).all():
            pid = post.get_attribute('data-post-id') or (post.get_attribute('id') or '').removeprefix('post')
            match = re.fullmatch(r'-(\d+)_(\d+)', pid)
            if not match or pid in seen:
                continue
            seen.add(pid)
            fresh += 1
            if pid in watch.get('known_posts', []):
                continue
            if expected_owner is not None and int(match[1]) != expected_owner:
                continue
            # Never identify a quoted/reposted announcement as this group's own post.
            if post.locator('[data-post-nesting-lvl="1"], .copy_quote').count():
                continue
            text = post.locator('[data-testid="post_text"], .wall_post_text').first
            if not text.count():
                continue
            more = text.locator('[data-id="showMoreButton"], .wall_post_more')
            if more.count() and more.first.is_visible():
                more.first.click()
            body = text.evaluate(TEXT)
            if normalize(body) != wanted:
                continue
            photos = post.locator('a[href*="photo"]').evaluate_all(
                r'''es=>[...new Set(es.map(e=>(e.getAttribute('href')||'').match(/^\/photo(-?\d+_\d+)(?:[?#]|$)/)?.[1]).filter(Boolean))].length''')
            if photos != watch['photo_count']:
                continue
            stamp = None
            date = post.locator('[data-testid="post-header-subtitle-date"]' if suggested else
                                '[data-testid="post_date_block_preview"], .post_link').first
            ts = date.locator('[data-ts]').first
            if date.count():
                value = date.get_attribute('data-ts') or (ts.get_attribute('data-ts') if ts.count() else '') or ''
                stamp = int(value) if value.isdigit() else None
            if stamp is None and suggested and date.count():
                label = ' '.join(date.inner_text().lower().split())
                minutes = re.fullmatch(r'(\d+) мин(?:ут[аы]?)?\.? назад', label)
                if minutes:
                    stamp = time.time() - int(minutes[1]) * 60
                elif label in ('только что', 'сейчас'):
                    stamp = time.time()
                else:
                    stamp = date_timestamp(label, time.time(), timezone)
            if stamp is None and date.count():
                date.hover()
                try:
                    page.wait_for_function('(el)=>el.getAttribute("aria-describedby")', arg=date.element_handle(), timeout=2000)
                    tip_id = date.get_attribute('aria-describedby')
                    tooltip = page.locator('[id=' + json.dumps(tip_id) + ']')
                    stamp = date_timestamp(tooltip.inner_text(), time.time(), timezone)
                except Exception:
                    stamp = date_timestamp(date.get_attribute('title') or '', time.time(), timezone)
            if stamp is None:
                unreadable += 1
                continue
            # Tooltip precision is one minute. A pre-submission minute cannot match.
            if stamp < int(watch['since'] // 60) * 60 or stamp > time.time() + 60:
                continue
            matches.append({'url': 'https://vk.ru/wall' + pid, 'post_time': stamp})
        if not fresh:
            break
        page.mouse.wheel(0, 2400)
        page.wait_for_timeout(800)
    if len(matches) == 1:
        return {'state': 'found', **matches[0], 'scanned': len(seen),
                'detail': 'На стене найдена запись: совпали полный текст и число фото, дата не раньше отправки (точность — минута). Это сопоставление по содержимому, а не ответ администратора.'}
    detail = ('Найдено несколько совпадающих публикаций; однозначно определить запись нельзя.' if matches else
              f'Среди {len(seen)} просмотренных записей публикация пока не найдена. Это не означает отказ администратора.')
    if unreadable:
        detail += ' У совпадающей записи не удалось проверить дату.'
    return {'state': 'pending', 'url': '', 'scanned': len(seen), 'detail': detail}
