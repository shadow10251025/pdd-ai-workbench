import unittest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from local_security import LocalSessionBoundary
from run_dev import DevelopmentBoundary


class DeveloperEntryTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.add_middleware(DevelopmentBoundary)
        app.add_middleware(LocalSessionBoundary)

        @app.post('/api/ai/plan')
        def plan():
            return {'ok': True}

        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_key_free_status_and_local_image_operations(self):
        status = self.client.get('/api/license/status').json()
        self.assertTrue(status['development_mode'])
        self.assertFalse(status['admin_mode'])
        self.assertEqual(self.client.post('/api/ai/plan').status_code, 403)
        token = self.client.get('/api/session').json()['session_token']
        self.assertEqual(self.client.post('/api/ai/plan', headers={'X-PDD-Session': token}).status_code, 200)

    def test_shop_admin_and_cross_origin_requests_stay_blocked(self):
        token = self.client.get('/api/session').json()['session_token']
        for path in ('/api/start-listing', '/api/admin/gen', '/api/open-page', '/api/license/activate'):
            response = self.client.post(path, headers={'X-PDD-Session': token})
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()['code'], 'development_operation_disabled')
        self.assertEqual(self.client.get('/api/license/status', headers={'Host': 'example.com'}).status_code, 403)
        self.assertEqual(self.client.get('/api/license/status', headers={'Origin': 'https://example.com'}).status_code, 403)


if __name__ == '__main__':
    unittest.main()
