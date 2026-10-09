import tempfile,unittest
from operators import create_gateway
class RetiredDesktopTests(unittest.TestCase):
 def test_old_screen_routes_closed_and_login_redirects(self):
  with tempfile.TemporaryDirectory() as tmp:
   app=create_gateway(tmp);uid=app.operators.add('owner','Test-password-2026',first=True);c=app.test_client()
   self.assertEqual(c.get('/api/desktop-access').status_code,401)
   with c.session_transaction() as s:s.update(uid=uid,version=1)
   for path in ['/api/desktop-access','/desktop-client/vnc.html']:
    self.assertEqual(c.get(path).status_code,410)
   self.assertEqual(c.get('/vk-login').location,'/vk-id')
   self.assertNotIn('data:',c.get('/').headers['Content-Security-Policy'])
   self.assertEqual(c.post('/api/browser/desktop').status_code,403)
   app.workspaces.pool.shutdown()
