import re,tempfile,unittest
from unittest.mock import Mock
from app import create_app
class GroupNameTests(unittest.TestCase):
 def test_name_persists_api_checks_and_does_not_change_history_or_schedule(self):
  with tempfile.TemporaryDirectory() as tmp:
   app=create_app(tmp,official=True);c=app.test_client();s=app.store
   s.state['groups']=[dict(id='g',name='old',slug='club123',url='https://vk.ru/club123',mode='api_suggest',interval_hours=24,next_at=9999999999,enabled=False)]
   s.state['history']=[dict(id='h',group_name='old',group_id='g',status='error')]
   csrf=re.search(r'name="csrf-token" content="([^"]+)"',c.get('/').text)[1];headers={'X-CSRF-Token':csrf}
   self.assertEqual(c.put('/api/groups/g/name',json={'name':'Моя группа'},headers=headers).status_code,200)
   fake=Mock();fake.group.return_value=dict(vk_id=123,name='VK title',type='page',can_post=0,wall=2);s.browser.factory=lambda:fake
   self.assertEqual(c.post('/api/groups/g/check',json={},headers=headers).status_code,200)
   g=s.group('g');self.assertEqual(g['name'],'Моя группа');self.assertEqual(g['vk_name'],'VK title');self.assertEqual(g['interval_hours'],24);self.assertFalse(g['enabled'])
   self.assertEqual(s.state['history'][0]['group_name'],'old')
   self.assertEqual(c.put('/api/groups/g/name',json={'name':''},headers=headers).status_code,400)
   self.assertEqual(c.put('/api/groups/g/name',json={'name':'x'}).status_code,403)
   self.assertEqual(create_app(tmp,official=True).store.group('g')['name'],'Моя группа')

 def test_photo_layout_saved_and_validated(self):
  with tempfile.TemporaryDirectory() as tmp:
   app=create_app(tmp,official=True);c=app.test_client()
   csrf=re.search(r'name="csrf-token" content="([^"]+)"',c.get('/').text)[1];headers={'X-CSRF-Token':csrf}
   body=dict(text='test',photos=[],photo_layout='carousel',revision=0)
   self.assertEqual(c.put('/api/draft',json=body,headers=headers).status_code,200)
   self.assertEqual(create_app(tmp,official=True).store.state['draft']['photo_layout'],'carousel')
   body.update(photo_layout='invalid',revision=1)
   self.assertEqual(c.put('/api/draft',json=body,headers=headers).status_code,400)
