import tempfile,time,unittest,re
from unittest.mock import Mock,patch
from app import create_app
from operators import create_gateway
from vk_api import VK,UncertainDelivery

class OfficialTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.app=create_app(self.tmp.name,official=True);self.s=self.app.store
  self.v=Mock();self.v.group.return_value=dict(can_suggest=1,can_post=0);self.v.call.return_value={'items':[]};self.s.browser.factory=lambda:self.v
  self.g=dict(id='g',name='test',slug='club123',url='https://vk.ru/club123',vk_id=123,mode='browser_suggest',enabled=True,interval_hours=24,next_at=time.time()-1,browser_checked='browser_suggest')
  self.s.state['groups']=[self.g];self.s.state['draft']=dict(text='test text',photos=[]);self.s.recover()
 def tearDown(self): self.tmp.cleanup()
 def test_migration_pauses_preserves_schedule(self):
  self.assertEqual(self.g['mode'],'api_suggest');self.assertTrue(self.g['enabled']);self.assertNotIn('browser_checked',self.g)
  self.assertTrue(self.s.state['automation_paused']);self.s.tick();self.v.post.assert_not_called()
 def test_delivery_snapshot_and_unknown_block_repeat(self):
  self.g['api_checked']=True;self.v.post.return_value='https://vk.com/wall-123_7';self.s.deliver(self.s.claim('g'))
  h=self.s.state['history'][0];self.assertEqual(h['status'],'api_suggested');self.assertEqual(h['publication']['post_id'],7)
  with self.assertRaises(ValueError):self.s.claim('g')
  h['publication']['state']='stopped';self.v.post.side_effect=UncertainDelivery('no response');self.s.deliver(self.s.claim('g'))
  self.assertEqual(self.s.state['history'][0]['status'],'unknown');self.assertFalse(self.g['enabled'])
  with self.assertRaises(ValueError):self.s.claim('g')
 def test_publication_public_only(self):
  self.g['api_checked']=True;self.v.post.return_value='https://vk.com/wall-123_7';self.s.deliver(self.s.claim('g'));h=self.s.state['history'][0]
  self.v.call.return_value=dict(items=[dict(id=7,owner_id=-123,date=int(time.time()),text='test text',attachments=[],post_type='suggest')])
  self.s.check_publication(self.s.claim_publication(h['id']));self.assertEqual(h['publication']['state'],'pending')
  self.v.call.return_value['items'][0]['post_type']='post';self.s.check_publication(self.s.claim_publication(h['id']));self.assertEqual(h['status'],'published')
 def test_publication_changed_id_after_moderation(self):
  self.g['api_checked']=True;self.v.post.return_value='https://vk.com/wall-123_7';self.s.deliver(self.s.claim('g'));h=self.s.state['history'][0]
  self.v.call.return_value=dict(items=[dict(id=8,owner_id=-123,date=int(time.time()),text='test text',attachments=[],post_type='post')])
  self.s.check_publication(self.s.claim_publication(h['id']))
  self.assertEqual(h['status'],'published');self.assertEqual(h['url'],'https://vk.ru/wall-123_8')
 def test_changed_id_ambiguous_matches_stay_pending(self):
  self.g['api_checked']=True;self.v.post.return_value='https://vk.com/wall-123_7';self.s.deliver(self.s.claim('g'));h=self.s.state['history'][0]
  self.v.call.return_value=dict(items=[dict(id=i,owner_id=-123,date=int(time.time()),text='test text',attachments=[],post_type='post') for i in (8,9)])
  self.s.check_publication(self.s.claim_publication(h['id']));self.assertEqual(h['publication']['state'],'pending')
 def test_matching_original_id_preferred_among_duplicates(self):
  self.g['api_checked']=True;self.v.post.return_value='https://vk.com/wall-123_7';self.s.deliver(self.s.claim('g'));h=self.s.state['history'][0]
  self.v.call.return_value=dict(items=[dict(id=i,owner_id=-123,date=int(time.time()),text='test text',attachments=[],post_type='post') for i in (7,8)])
  self.s.check_publication(self.s.claim_publication(h['id']));self.assertEqual(h['url'],'https://vk.ru/wall-123_7')
 @patch('vk_api.requests.post')
 def test_post_personal_grid_guid(self,p):
  p.return_value=Mock(json=Mock(return_value={'response':{'post_id':7}}));VK('TEST-ONLY').post(123,'text',[],'unique');d=p.call_args.kwargs['data']
  self.assertEqual((d['from_group'],d['primary_attachments_mode'],d['guid']),(0,'grid','unique'));self.assertNotIn('signed',d)

 def test_schedule_runs_only_after_check_and_resume(self):
  self.s.state['automation_paused']=False;self.s.tick();self.v.post.assert_not_called()
  self.g['api_checked']=True;self.v.post.return_value='https://vk.com/wall-123_7'
  self.s.tick();self.assertEqual(self.v.post.call_count,1);self.s.tick();self.assertEqual(self.v.post.call_count,1)
 def test_restart_never_resends_interrupted_job(self):
  self.g['api_checked']=True;self.s.claim('g');self.s.recover()
  self.assertEqual(self.s.state['history'][0]['status'],'unknown');self.assertFalse(self.g['enabled'])
  self.v.post.assert_not_called()

 def test_group_check_warns_and_blocks_direct_publication(self):
  c=self.app.test_client();csrf=re.search(r'name="csrf-token" content="([^"]+)"',c.get('/').text)[1]
  self.v.group.return_value=dict(vk_id=123,name='group',type='group',can_post=0,wall=2)
  r=c.post('/api/groups/g/check',json={},headers={'X-CSRF-Token':csrf})
  self.assertEqual(r.status_code,200);self.assertTrue(self.g['api_checked']);self.assertTrue(self.g['api_warning'])
  self.v.group.return_value['can_post']=1
  r=c.post('/api/groups/g/check',json={},headers={'X-CSRF-Token':csrf})
  self.assertEqual(r.status_code,400);self.assertFalse(self.g['api_checked']);self.v.post.assert_not_called()

 @patch('vk_api.requests.post')
 def test_carousel_parameter_and_snapshot(self,p):
  p.return_value=Mock(json=Mock(return_value={'response':{'post_id':7}}))
  VK('TEST-ONLY').post(123,'text',[],'unique',layout='carousel')
  self.assertEqual(p.call_args.kwargs['data']['primary_attachments_mode'],'carousel')
  self.s.state['draft']['photo_layout']='carousel';self.g['api_checked']=True;self.v.post.return_value='https://vk.com/wall-123_7'
  self.s.deliver(self.s.claim('g'))
  self.assertEqual(self.v.post.call_args.kwargs['layout'],'carousel')
  self.assertEqual(self.s.state['history'][0]['publication']['photo_layout'],'carousel')

 def test_can_suggest_denied_on_check(self):
  c=self.app.test_client();csrf=re.search(r'name="csrf-token" content="([^\"]+)"',c.get('/').text)[1]
  self.v.group.return_value=dict(vk_id=123,name='test',can_post=0,can_suggest=0,type='group',wall=2)
  r=c.post('/api/groups/g/check',json={},headers={'X-CSRF-Token':csrf})
  self.assertEqual(r.status_code,400);self.assertFalse(self.g['api_checked']);self.assertFalse(self.g['enabled']);self.assertIn('can_suggest=0',self.g['api_error'])
 def test_changed_permission_stops_before_upload(self):
  self.g['api_checked']=True;self.s.state['draft']['photos']=['placeholder.jpg'];self.v.group.return_value=dict(can_post=0,can_suggest=0)
  self.s.deliver(self.s.claim('g'));self.v.photo.assert_not_called();self.v.post.assert_not_called()
  self.assertFalse(self.g['api_checked']);self.assertFalse(self.g['enabled']);self.assertEqual(self.s.state['history'][0]['status'],'error')

class RefreshTests(unittest.TestCase):
 def test_refresh_saved_reused(self):
  with tempfile.TemporaryDirectory() as tmp:
   app=create_gateway(tmp);uid=app.operators.add('owner','Test-password-2026',first=True);auth=app.vkid
   with patch('vkid_auth.requests.post') as p:
    p.return_value=Mock(json=Mock(return_value=dict(state='state',access_token='TEST-OLD',refresh_token='TEST-REFRESH',user_id=123,expires_in=1,scope='wall photos')))
    auth.exchange(app.operators.user(uid),'code','device','state','verifier')
    def response(*a,**kw):return Mock(json=Mock(return_value=dict(state=kw['data']['state'],access_token='TEST-NEW',refresh_token='TEST-ROTATED',user_id=123,expires_in=3600,scope='wall photos')))
    p.side_effect=response;self.assertEqual(auth.access(uid),'TEST-NEW');self.assertEqual(auth.access(uid),'TEST-NEW');self.assertEqual(p.call_count,2)
   app.workspaces.pool.shutdown()
