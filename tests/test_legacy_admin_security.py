import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient


class LegacyAdminSecurityTests(unittest.TestCase):
    def load(self, directory, password):
        path=Path(__file__).resolve().parents[1]/'server/main.py'
        spec=importlib.util.spec_from_file_location('legacy_auth_'+str(id(directory)),path)
        module=importlib.util.module_from_spec(spec)
        with patch.dict(os.environ,{'DB_PATH':str(Path(directory)/'licenses.sqlite3'),'ADMIN_PASSWORD':password}):
            spec.loader.exec_module(module)
        return module

    def test_no_default_password_and_real_health_check(self):
        with tempfile.TemporaryDirectory() as directory:
            module=self.load(directory,'')
            with TestClient(module.app) as client:
                self.assertEqual(client.post('/admin/login',json={'password':'admin123'}).status_code,503)
                self.assertFalse(client.get('/health').json()['admin_configured'])

    def test_rate_limit_and_revocation(self):
        with tempfile.TemporaryDirectory() as directory:
            module=self.load(directory,'Strong-password-29!')
            with TestClient(module.app) as client:
                login=client.post('/admin/login',json={'password':'Strong-password-29!'})
                self.assertEqual(login.status_code,200)
                auth={'Authorization':'Bearer '+login.json()['token']}
                self.assertEqual(client.post('/admin/logout-all',headers=auth).status_code,200)
                self.assertEqual(client.post('/admin/logout',headers=auth).status_code,401)
                for _ in range(7): self.assertEqual(client.post('/admin/login',json={'password':'wrong'}).status_code,401)
                self.assertEqual(client.post('/admin/login',json={'password':'wrong'}).status_code,429)


if __name__=='__main__': unittest.main()
