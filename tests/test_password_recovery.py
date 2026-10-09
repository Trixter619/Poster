import re,tempfile,unittest
from urllib.parse import urlsplit
from operators import create_gateway

class RecoveryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.app=create_gateway(self.tmp.name);self.app.testing=True
  self.uid=self.app.operators.add('owner','Test-owner-password',first=True)
  self.oid=self.app.operators.add('operator','Test-operator-password',actor=self.uid)
  self.admin=self.app.test_client();self.operator=self.app.test_client();self.anon=self.app.test_client()
  for c,uid in [(self.admin,self.uid),(self.operator,self.oid)]:
   with c.session_transaction() as s:s.update(uid=uid,version=1)
 def tearDown(self):self.app.workspaces.pool.shutdown();self.tmp.cleanup()
 def csrf(self,c,path):return re.search(r'name="csrf" value="([^"]+)"',c.get(path).text)[1]
 def post(self,c,path,**data):return c.post(path,data={'csrf':self.csrf(c,path),**data})
 def test_admin_button_and_role_guard(self):
  self.assertIn('Заявки и операторы',self.admin.get('/').text)
  self.assertNotIn('href="/admin"',self.operator.get('/').text)
  self.assertEqual(self.operator.get('/admin').status_code,403)
  self.assertEqual(self.anon.get('/admin').location,'/login')
  self.assertEqual(self.operator.post('/admin',data={'csrf':self.csrf(self.operator,'/account'),'action':'reset_password','uid':self.oid}).status_code,403)
 def test_recovery_request_generic_and_visible_to_admin(self):
  a=self.post(self.anon,'/forgot-password',username='operator')
  b=self.post(self.anon,'/forgot-password',username='unknown')
  self.assertIn('Если это активная учётная запись',a.text);self.assertIn('Если это активная учётная запись',b.text)
  self.assertEqual(len(self.app.operators.recovery_requests()),1)
  self.assertIn('запросил восстановление',self.admin.get('/admin').text)
 def test_issue_link_single_use_revokes_sessions_and_new_link_revokes_old(self):
  response=self.post(self.admin,'/admin',action='reset_password',uid=self.oid)
  link=re.search(r'readonly value="([^"]+)"',response.text)[1];token=urlsplit(link).fragment
  self.assertTrue(token);self.assertFalse(urlsplit(link).query)
  self.assertNotIn(token,self.admin.get('/admin').text)
  r=self.post(self.anon,'/reset-password',token=token,password='New-password-2026',password_confirm='New-password-2026')
  self.assertIn('Пароль изменён',r.text);self.assertEqual(self.operator.get('/api/state').status_code,401)
  self.assertIsNotNone(self.app.operators.authenticate('operator','New-password-2026','new-ip'))
  with self.assertRaises(ValueError):self.app.operators.reset_password(token,'Another-password-2026')
  old=self.app.operators.issue_reset(self.oid,self.uid);new=self.app.operators.issue_reset(self.oid,self.uid)
  with self.assertRaises(ValueError):self.app.operators.reset_password(old,'Another-password-2026')
  self.app.operators.reset_password(new,'Another-password-2026')
 def test_expiry_disable_version_and_confirmation(self):
  for kind in ('expired','disabled','version'):
   with self.app.operators.connect() as db:db.execute('UPDATE users SET active=1 WHERE id=?',(self.oid,))
   token=self.app.operators.issue_reset(self.oid,self.uid)
   with self.app.operators.connect() as db:
    if kind=='expired':db.execute('UPDATE password_resets SET expires=0')
    if kind=='disabled':db.execute('UPDATE users SET active=0 WHERE id=?',(self.oid,))
    if kind=='version':db.execute('UPDATE users SET version=version+1 WHERE id=?',(self.oid,))
   with self.assertRaises(ValueError):self.app.operators.reset_password(token,'Another-password-2026')
  r=self.post(self.anon,'/reset-password',token='x'*43,password='New-password-2026',password_confirm='different')
  self.assertIn('не совпадают',r.text)
 def test_csrf_and_rate_limit(self):
  self.assertEqual(self.anon.post('/forgot-password',data={'username':'operator'}).status_code,403)
  for _ in range(5):self.app.operators.request_recovery('unknown','rate-test')
  with self.assertRaises(ValueError):self.app.operators.request_recovery('operator','rate-test')
  with self.assertRaises(ValueError):self.app.operators.issue_reset(self.uid,self.uid)
  with self.assertRaises(ValueError):self.app.operators.issue_reset(self.oid,self.oid)

class RoleTests(unittest.TestCase):
 setUp=RecoveryTests.setUp
 tearDown=RecoveryTests.tearDown
 def test_grant_revoke_reauth_and_last_admin(self):
  users=self.app.operators
  with self.assertRaises(ValueError):users.set_admin(self.uid,self.uid,False)
  with self.assertRaises(ValueError):users.set_admin(self.uid,self.oid,False)
  users.set_admin(self.oid,self.uid,True)
  self.assertEqual(self.operator.get('/api/state').status_code,401)
  with self.operator.session_transaction() as s:s.update(uid=self.oid,version=users.user(self.oid)['version'])
  self.assertIn('href="/admin"',self.operator.get('/').text)
  users.set_admin(self.uid,self.oid,False)
  self.assertEqual(self.admin.get('/api/state').status_code,401)
  with self.admin.session_transaction() as s:s.update(uid=self.uid,version=users.user(self.uid)['version'])
  self.assertEqual(self.admin.get('/admin').status_code,403)
  self.assertNotIn('href="/admin"',self.admin.get('/').text)
  with self.assertRaises(ValueError):users.set_admin(self.oid,self.oid,False)
  users.set_admin(self.uid,self.oid,True)
  users.set_admin(self.oid,self.uid,False)
  self.assertFalse(users.user(self.oid)['admin'])

 def test_other_admin_can_recover_but_role_change_invalidates_link(self):
  users=self.app.operators;users.set_admin(self.oid,self.uid,True)
  token=users.issue_reset(self.oid,self.uid)
  users.reset_password(token,'Recovered-admin-password')
  self.assertTrue(users.user(self.oid)['admin'])
  token=users.issue_reset(self.oid,self.uid)
  users.set_admin(self.oid,self.uid,False)
  with self.assertRaises(ValueError):users.reset_password(token,'Another-admin-password')
