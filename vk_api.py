"""VK transport. Never log response bodies, request URLs or access tokens."""
import time
import io
from PIL import Image, ImageOps
from urllib.parse import urlparse

import requests


SUGGEST_DENIED = ('VK API не разрешает этому аккаунту предлагать новости в сообщество (can_suggest=0). '
                  'Проверь подписку на группу и подключённый профиль, затем нажми «Проверить доступ».')


class VKError(Exception):
    pass


class UncertainDelivery(VKError):
    pass


class VK:
    def __init__(self, token):
        self.token = token

    def call(self, method, **params):
        time.sleep(0.4)
        try:
            r = requests.post(
                'https://api.vk.com/method/' + method,
                data={**params, 'access_token': self.token, 'v': '5.199'},
                timeout=(10, 45), allow_redirects=False,
            )
            r.raise_for_status()
            body = r.json()
        except (requests.RequestException, ValueError):
            if method == 'wall.post':
                raise UncertainDelivery('Нет подтверждения VK. Проверь стену и предложенные записи перед повтором.') from None
            raise VKError('VK недоступен или вернул некорректный ответ. Попробуй позже.') from None
        if 'error' in body:
            code = body['error'].get('error_code', 0)
            messages = {
                5: 'Авторизация истекла или токен недействителен. Подключи аккаунт заново.',
                6: 'VK ограничил частоту запросов. Увеличь интервал.',
                7: 'У приложения нет прав на этот метод VK.',
                14: 'VK запросил капчу. Открой VK и пройди проверку вручную.',
                15: 'VK запретил доступ. Проверь права аккаунта и настройки группы.',
                17: 'VK требует подтверждения аккаунта на своём сайте.',
                20: 'Этот метод недоступен для типа приложения, выдавшего токен.',
                27: 'Нужен пользовательский токен, токен сообщества не подходит.',
                100: 'VK отклонил параметры записи или группы.',
                214: 'Добавление записей на эту стену запрещено.',
            }
            raise VKError(f'VK {code}: ' + messages.get(code, 'VK отклонил запрос. Проверь настройки группы и приложения.'))
        if 'response' not in body:
            if method == 'wall.post':
                raise UncertainDelivery('Неизвестный результат отправки. Проверь VK перед повтором.')
            raise VKError('Неожиданный формат ответа VK.')
        return body['response']

    def identity(self):
        user = self.call('users.get')[0]
        permissions = self.call('account.getAppPermissions')
        if permissions & (4 | 8192) != (4 | 8192):
            raise VKError('Токену нужны разрешения photos и wall. Проверка аккаунта не включает тестовую публикацию.')
        return {'id': user['id'], 'name': f"{user['first_name']} {user['last_name']}"}

    def group(self, slug):
        result = self.call('groups.getById', group_ids=slug, fields='can_post,can_suggest,wall')
        items = result.get('groups', []) if isinstance(result, dict) else result
        if not items:
            raise VKError('Сообщество не найдено.')
        g = items[0]
        return {'vk_id': g['id'], 'name': g['name'], 'can_post': g.get('can_post'), 'can_suggest': g.get('can_suggest'), 'wall': g.get('wall'), 'type': g.get('type')}

    def photo(self, path):
        # Upload a bounded copy into the user's album; preserve the original.
        try:
            with Image.open(path) as original:
                picture = ImageOps.exif_transpose(original).convert('RGB')
                picture.thumbnail((1280, 1280))
                stream = io.BytesIO()
                picture.save(stream, 'JPEG', quality=85)
        except (OSError, ValueError):
            raise VKError('Не удалось подготовить фотографию. Запись не отправлена.') from None

        error = 'Не удалось загрузить фото в VK. Запись не отправлена.'
        for attempt in range(4):
            # Retry only temporary uploads, before saveWallPhoto or wall.post.
            # A fresh signed URL avoids reusing a failed upload session.
            url = self.call('photos.getWallUploadServer')['upload_url']
            parsed = urlparse(url)
            host = parsed.hostname or ''
            if parsed.scheme != 'https' or parsed.port not in (None, 443) or not any(
                host == d or host.endswith('.' + d) for d in ('vk.com', 'vk.ru', 'vkuserphoto.ru', 'userapi.com', 'vk-cdn.net')
            ):
                raise VKError('VK вернул неизвестный сервер загрузки. Нужна проверка адреса разработчиком.')
            stream.seek(0)
            try:
                r = requests.post(url, files={'photo': ('photo.jpg', stream, 'image/jpeg')},
                                  timeout=(10, 60), allow_redirects=False)
                if r.status_code in (502, 503, 504):
                    error = f'Сервер загрузки фото VK временно недоступен (HTTP {r.status_code}). Запись не отправлена. Повтори позже.'
                else:
                    r.raise_for_status()
                    uploaded = r.json()
                    if (isinstance(uploaded, dict) and all(uploaded.get(k) for k in ('photo', 'server', 'hash'))
                            and uploaded['photo'] not in ('[]', [])):
                        break
                    error = 'VK не принял фотографию после повторных загрузок. Запись не отправлена. Повтори позже.'
            except (requests.Timeout, requests.ConnectionError, ValueError):
                error = 'VK не подтвердил загрузку фото после повторных попыток. Запись не отправлена.'
            except requests.RequestException:
                raise VKError('VK отклонил загрузку фотографии. Запись не отправлена.') from None
            if attempt < 3:
                time.sleep(1 + attempt)
        else:
            raise VKError(error)

        # Saving and posting are never retried by this loop.
        try:
            result = self.call('photos.saveWallPhoto', **{k: uploaded[k] for k in ('photo', 'server', 'hash')})
            p = result[0]
            return f"photo{p['owner_id']}_{p['id']}" + ('_' + p['access_key'] if p.get('access_key') else '')
        except (ValueError, KeyError, IndexError, TypeError):
            raise VKError('VK не подтвердил сохранение фото. Запись не отправлена.') from None

    def post(self, group_id, message, attachments, guid, layout='grid'):
        if layout not in ('grid','carousel'):
            raise VKError('Неизвестный формат фотографий.')
        result = self.call('wall.post', owner_id=-int(group_id), from_group=0,
                           primary_attachments_mode=layout, message=message, attachments=','.join(attachments), guid=guid)
        if not isinstance(result, dict) or not result.get('post_id'):
            raise UncertainDelivery('VK не вернул номер записи. Проверь стену и предложенные записи перед повтором.')
        return f"https://vk.com/wall-{group_id}_{int(result['post_id'])}"
