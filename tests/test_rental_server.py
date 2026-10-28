import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from rental_server import create_app


class RentalIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.env=patch.dict(os.environ, {'PDD_RENTAL_BOOTSTRAP_USER':'admin','PDD_RENTAL_BOOTSTRAP_PASSWORD':'Test-password-927!','PDD_RENTAL_HOSTS':'testserver'}, clear=False)
        self.env.start(); self.addCleanup(self.env.stop)
        self.app=create_app(self.root,production=False)
        self.client=TestClient(self.app); self.addCleanup(self.client.close)
        self.admin=self.login('admin')

    def login(self,name):
        response=self.client.post('/api/auth/login',json={'username':name,'password':'Test-password-927!','device_name':'test-device'})
        self.assertEqual(response.status_code,200,response.text)
        return {'Authorization':'Bearer '+response.json()['access_token']}

    def tenant(self,name):
        response=self.client.post('/api/platform/tenants',headers=self.admin,json={'name':name,'owner_username':name+'-owner','owner_password':'Test-password-927!','image_limit':2,'listing_limit':1})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()['id'], self.login(name+'-owner')

    def test_cross_tenant_drafts_tasks_assets_and_keys(self):
        a,ha=self.tenant('first'); b,hb=self.tenant('second')
        response=self.client.post('/api/drafts',headers=ha,json={'id':'draft-a','title':'first','image_api_key':'private-provider-key','metadata':{'prompt_api_key':'second-key'}})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.client.get('/api/drafts/draft-a',headers=hb).status_code,404)
        sub=self.app.state.tenant_app(a); store=sub.state.ai_runner.store
        task,_=store.create_task(a,'workflow',{'image_api_key':'private-provider-key'},'one')
        self.assertEqual(self.client.get('/api/ai/tasks/'+task['id'],headers=hb).status_code,404)
        self.assertEqual(self.client.post('/api/ai/regenerate',headers=hb,data={'source_job_id':'a'*16,'module_id':'hero_001'}).status_code,404)
        self.assertEqual(self.client.get('/data/../rental_auth.sqlite3',headers=ha).status_code,404)
        self.assertEqual(self.client.get('/data/ai_jobs/'+a+'/private.jpg',headers=hb).status_code,404)
        for database in self.root.rglob('*.sqlite3'):
            self.assertNotIn(b'private-provider-key',database.read_bytes()); self.assertNotIn(b'second-key',database.read_bytes())
        self.assertEqual(self.client.post('/api/start-listing',headers=ha,json={}).status_code,404)
        self.assertEqual(self.client.get('/api/admin/list',headers=ha).status_code,404)

    def test_roles_revocation_device_ownership_and_tenant_disable(self):
        tid,owner=self.tenant('merchant')
        response=self.client.post('/api/tenant/users',headers=owner,json={'username':'review-user','password':'Test-password-927!','role':'reviewer'})
        self.assertEqual(response.status_code,200,response.text)
        reviewer=self.login('review-user')
        self.assertEqual(self.client.post('/api/drafts',headers=reviewer,json={'title':'x'}).status_code,403)
        self.assertEqual(self.client.post('/api/ai/workflow',headers=reviewer).status_code,403)
        self.assertEqual(self.client.post('/api/tenant/users',headers=reviewer,json={'username':'bad','password':'Test-password-927!'}).status_code,403)
        devices=self.client.get('/api/auth/devices',headers=reviewer).json()['devices']
        self.assertEqual(self.client.delete('/api/auth/devices/'+devices[0]['id'],headers=owner).status_code,404)
        self.assertEqual(self.client.delete('/api/auth/devices/'+devices[0]['id'],headers=reviewer).status_code,200)
        self.assertEqual(self.client.get('/api/auth/me',headers=reviewer).status_code,401)
        self.assertEqual(self.client.put('/api/platform/tenants/'+tid+'/entitlement',headers=owner,json={'enabled':False,'image_limit':0,'listing_limit':0}).status_code,403)
        disabled=self.client.put('/api/platform/tenants/'+tid+'/entitlement',headers=self.admin,json={'enabled':False,'image_limit':0,'listing_limit':0})
        self.assertEqual(disabled.status_code,200,disabled.text)
        self.assertEqual(self.client.get('/api/tenant/usage',headers=owner).status_code,401)

    def test_csrf_https_limit_and_online_separate_quotas(self):
        tid,owner=self.tenant('budget')
        usage=self.client.get('/api/tenant/usage',headers=owner).json()
        self.assertEqual(usage['image_limit'],2); self.assertEqual(usage['listing_limit'],1)
        # Cookie authentication requires CSRF; bearer clients are explicit.
        self.assertEqual(self.client.post('/api/drafts',json={'title':'x'}).status_code,403)
        self.assertEqual(self.client.post('/api/drafts',headers={**owner,'Origin':'https://other.example'},json={'title':'x'}).status_code,403)
        self.assertEqual(self.client.post('/api/drafts',headers={**owner,'Content-Length':str(14*1024**2)},json={}).status_code,413)
        app=create_app(self.root/'production',production=True)
        with TestClient(app) as client:
            self.assertEqual(client.get('/health').status_code,426)
        with TestClient(app,base_url='https://testserver') as client:
            login=client.post('/api/auth/login',json={'username':'admin','password':'Test-password-927!'})
            self.assertEqual(login.status_code,200,login.text)
            self.assertIn('Secure',login.headers['set-cookie']); self.assertIn('HttpOnly',login.headers['set-cookie'])
            self.assertEqual(login.headers['x-content-type-options'],'nosniff')

    def test_login_limit_and_no_default_admin(self):
        with patch.dict(os.environ,{'PDD_RENTAL_BOOTSTRAP_USER':'','PDD_RENTAL_BOOTSTRAP_PASSWORD':''}):
            app=create_app(self.root/'empty',production=False)
        with TestClient(app) as client:
            for index in range(8):
                self.assertEqual(client.post('/api/auth/login',json={'username':'admin','password':'admin123'}).status_code,401)
            self.assertEqual(client.post('/api/auth/login',json={'username':'admin','password':'admin123'}).status_code,429)


if __name__=='__main__': unittest.main()
