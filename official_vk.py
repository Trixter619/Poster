"""Operator-scoped API adapter; no browser state or secret serialization."""
import re
import time
from vk_api import VKError
from publication_vk import normalize


class OfficialVK:
    def __init__(self, folder):
        self.folder = folder
        self.factory = None
        self.pool = None

    def client(self):
        if not self.factory:
            raise VKError('Подключи VK ID.')
        return self.factory()

    def info(self):
        return {'opened': False, 'authenticated': False, 'message': 'Используется VK ID и официальный API.'}

    def call(self, action, *args):
        if action == 'close':
            return self.info()
        if action != 'publication':
            raise VKError('Браузерное подключение отключено. Используй VK ID.')
        group, watch = args
        client = self.client()
        owner = watch.get('vk_id')
        if not owner:
            owner = client.group(group['url'].rstrip('/').split('/')[-1])['vk_id']
        # Public wall only: a suggested record is not a confirmed publication.
        matches = []
        scanned = 0
        for offset in range(0, 500, 100):
            result = client.call('wall.get', owner_id=-int(owner), count=100, offset=offset, filter='all')
            items = result.get('items', [])
            scanned += len(items)
            for post in items:
                pid = f"-{owner}_{post['id']}"
                if post.get('post_type', 'post') != 'post' or post.get('copy_history') or pid in watch.get('known_posts', []):
                    continue
                if post.get('owner_id') != -int(owner) or post.get('date', 0) < watch['since'] - 60:
                    continue
                photos = [a for a in post.get('attachments', []) if a.get('type') == 'photo']
                if normalize(post.get('text', '')) != normalize(watch['text']) or len(photos) != watch['photo_count']:
                    continue
                matches.append(post)
            if len(items) < 100 or (items and all(p.get('date', time.time()) < watch['since'] - 60 for p in items if not p.get('is_pinned'))):
                break
        # The public record may receive a new ID after moderation. Prefer the
        # returned ID, otherwise require one unique match against the snapshot.
        exact = [p for p in matches if p['id'] == watch.get('post_id')]
        if exact:
            matches = exact
        if len(matches) == 1:
            return {'state': 'found', 'url': f"https://vk.ru/wall-{owner}_{matches[0]['id']}",
                    'post_time': matches[0]['date'], 'scanned': scanned,
                    'detail': 'Запись найдена на публичной стене: совпали текст, число фото и время.'}
        return {'state': 'pending', 'url': '', 'scanned': scanned,
                'detail': 'Публикация пока не найдена на публичной стене. Это не означает отказ модератора.'}
