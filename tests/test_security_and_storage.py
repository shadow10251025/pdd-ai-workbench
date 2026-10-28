import asyncio
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import safe_http as http
from local_drafts import save_package
from local_security import LocalSessionBoundary
from task_store import StoreError, TaskStore


class SecurityStorageTests(unittest.TestCase):
    def test_cancel_does_not_hide_unknown_billing_and_camel_case_keys_are_redacted(self):
        with tempfile.TemporaryDirectory() as temp:
            store=TaskStore(Path(temp)); task,_=store.create_task('a','workflow',{'imageApiKey':'secret-image','nested':{'api-key':'secret-api','csrf_token':'secret-csrf'}})
            self.assertEqual(task['payload'],{'nested':{}})
            store.update_task('a',task['id'],status='needs_reconciliation')
            self.assertTrue(store.cancel('a',task['id']))
            self.assertEqual(store.get_task('a',task['id'])['status'],'needs_reconciliation')
            store.update_task('a',task['id'],status='interrupted')
            store.cancel('a',task['id']);self.assertEqual(store.get_task('a',task['id'])['status'],'cancelled')

    def test_large_response_rejected_from_headers_before_body_is_read(self):
        connection=MagicMock();response=MagicMock(status=200)
        connection.getresponse.return_value=response;response.getheaders.return_value=[('Content-Length','10000000')]
        with patch.object(http.http.client,'HTTPConnection',return_value=connection):
            with self.assertRaises(http.OutboundError):
                http.download_bytes('http://8.8.8.8/photo',max_bytes=1024)
        response.read.assert_not_called()

    def test_dns_addresses_all_checked_including_ipv6_and_metadata(self):
        for address in ('127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', '::ffff:127.0.0.1', '100.64.0.1'):
            with self.subTest(address=address), self.assertRaises(http.OutboundError):
                http.validate_public_url('https://[' + address + ']/' if ':' in address else 'https://' + address)
        with patch.object(http.socket, 'getaddrinfo', return_value=[(None,None,None,None,('8.8.8.8',443)), (None,None,None,None,('192.168.1.2',443))]):
            with self.assertRaises(http.OutboundError):
                http.validate_public_url('https://provider.example')

    def test_production_forces_https_even_with_local_demo_flag(self):
        token = http.OUTBOUND_PRODUCTION.set(True)
        try:
            with patch.dict(os.environ, {'PDD_AI_DEMO': '1'}):
                self.assertFalse(http.private_api_enabled())
                for url in ('http://8.8.8.8', 'https://127.0.0.1'):
                    with self.assertRaises(http.OutboundError):
                        http.validate_public_url(url, allow_private=True)
        finally:
            http.OUTBOUND_PRODUCTION.reset(token)

    def test_connection_pins_validated_address(self):
        connection = MagicMock()
        response = MagicMock(status=200)
        response.getheaders.return_value = [('Content-Length', '2')]
        response.read.side_effect = [b'ok', b'']
        connection.getresponse.return_value = response
        connection.request.side_effect = lambda *a, **k: connection.connect()
        with patch.object(http.socket, 'getaddrinfo', return_value=[(None,None,None,None,('8.8.8.8',80))]), patch.object(http.http.client, 'HTTPConnection', return_value=connection), patch.object(http.socket, 'create_connection') as dial:
            self.assertEqual(http.download_bytes('http://provider.example')[0:2], b'ok')
            self.assertEqual(dial.call_args.args[0], ('8.8.8.8', 80))

    def test_redirects_revalidated_and_keys_never_forwarded_to_another_origin(self):
        responses = [(302, {'location':'https://8.8.4.4/file'}, b''), (200, {}, b'ok')]
        with patch.object(http, '_send', side_effect=responses) as transport:
            self.assertEqual(http.download_bytes('https://8.8.8.8/image'), b'ok')
            self.assertEqual(transport.call_count, 2)
        with patch.object(http, '_send', return_value=(302, {'location':'http://127.0.0.1/secret'}, b'')):
            with self.assertRaises(http.OutboundError):
                http.download_bytes('https://8.8.8.8/image')
        with patch.object(http, '_send', return_value=(307, {'location':'https://8.8.4.4/paid'}, b'')) as transport:
            with self.assertRaises(http.OutboundError):
                http.request_json('POST', 'https://8.8.8.8/paid', key='private-key', json_payload={})
            self.assertEqual(transport.call_count, 1)
        captured = []
        def capture(method, url, **kwargs):
            captured.append(dict(kwargs['headers']))
            return (302, {'location':'https://8.8.4.4/file'}, b'') if len(captured)==1 else (200, {}, b'{}')
        with patch.object(http, '_send', side_effect=capture):
            http.request_json('GET','https://8.8.8.8/models',key='private-key')
        self.assertIn('Authorization', captured[0]); self.assertNotIn('Authorization', captured[1])
        with patch.object(http, '_send', return_value=(401, {}, b'private-key')):
            with self.assertRaises(http.OutboundError) as error:
                http.request_json('GET','https://8.8.8.8/models',key='private-key')
            self.assertNotIn('private-key', str(error.exception))

    def test_streaming_upload_limit_and_local_session_boundary(self):
        class Upload:
            async def read(self, size): return b'x' * size
        with self.assertRaises(http.OutboundError):
            asyncio.run(http.read_upload(Upload(), 100))
        app = FastAPI(); app.add_middleware(LocalSessionBoundary)
        @app.post('/api/write')
        def write(): return {'ok':True}
        with TestClient(app) as client:
            self.assertEqual(client.post('/api/write').status_code, 403)
            token = client.get('/api/session').json()['session_token']
            self.assertEqual(client.post('/api/write',headers={'X-PDD-Session':token}).status_code, 200)
            self.assertEqual(client.get('/api/session',headers={'Host':'evil.test'}).status_code,403)
            self.assertEqual(client.get('/api/session',headers={'Origin':'https://evil.test'}).status_code,403)
            self.assertEqual(client.post('/api/write',headers={'X-PDD-Session':token,'Content-Length':str(33*1024**2)}).status_code,413)

    def test_owner_scoped_durable_tasks_drafts_and_quota_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            store = TaskStore(Path(temp)); store.set_entitlement('a',image_limit=3,listing_limit=1)
            task, created = store.create_task('a','workflow',{'image_api_key':'top-secret','nested':{'prompt_api_key':'nested-secret'}},'one')
            self.assertTrue(created)
            self.assertFalse(store.create_task('a','workflow',{},'one')[1])
            self.assertIsNone(store.get_task('b',task['id']))
            store.reserve('a',task['id'],'image',3)
            store.settle('a',task['id'],'image',1)
            store.settle('a',task['id'],'image',1)
            self.assertEqual(store.entitlement('a')['image_used'],1)
            store.reopen_reservation('a',task['id'],'image')
            store.settle('a',task['id'],'image',3)
            store.settle('a',task['id'],'image',3)
            self.assertEqual(store.entitlement('a')['image_used'],3)
            with self.assertRaises(StoreError): store.reserve('a','other','image',1)
            store.save_draft('a',{'id':'product-a','title':'saved','stock':0,'api_key':'top-secret'})
            self.assertIsNone(store.get_draft('b','product-a'))
            store.update_task('a',task['id'],status='running',checkpoint={'job_id':'a'*16})
            restored=TaskStore(Path(temp)); restored.interrupt_startup()
            self.assertEqual(restored.get_task('a',task['id'])['status'],'interrupted')
            self.assertEqual(restored.get_draft('a','product-a')['stock'],0)
            self.assertNotIn(b'top-secret',store.path.read_bytes())
            self.assertNotIn(b'nested-secret',store.path.read_bytes())

    def test_draft_freezes_images_once_preserves_complete_data_and_partial_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); image=root/'data'/'ai_jobs'/'a'/'product.jpg'; image.parent.mkdir(parents=True)
            Image.new('RGB',(640,640),'red').save(image)
            store=TaskStore(root)
            raw={'title':'product','brand':'own','attrs':{'material':'confirmed'},'description':'description','category':'candidate','main_images':[str(image)],'detail_images':[str(image)],'specs':{'color':['red']},'sku':[{'name':'red','price':3,'single_price':5,'stock':0,'enabled':False,'spec_code':'009'}], 'review':{'hero_001':'approved'},'module_outputs':[{'id':'hero_001','path':str(image),'kind':'hero'}]}
            saved=save_package(root,store,'a',raw)
            self.assertEqual(saved['sku'][0]['stock'],0); self.assertFalse(saved['sku'][0]['enabled'])
            self.assertEqual(saved['sku'][0]['spec_code'],'009'); self.assertEqual(saved['brand'],'own')
            self.assertEqual(len(list((root/'data/products/assets').glob('*'))),1)
            saved_again=save_package(root,store,'a',saved)
            self.assertEqual(saved_again['review'],raw['review'])
            self.assertEqual(len(list((root/'data/products/assets').glob('*'))),1)
            bad={**saved,'main_images':[str(image),'data/missing.jpg']}
            with self.assertRaises(StoreError): save_package(root,store,'a',bad)
            self.assertEqual(store.get_draft('a',saved['id'])['main_images'],saved['main_images'])

    def test_zip_bombs_and_traversal_rejected_before_decryption(self):
        import asset_store
        for filename,content in (('../x.bin',b'x'),('x.bin',b'x'*(1024*1024+1))):
            data=io.BytesIO()
            with zipfile.ZipFile(data,'w',zipfile.ZIP_DEFLATED) as archive: archive.writestr(filename,content)
            with patch.object(asset_store,'_license_seed',return_value=b'x'*16), patch.object(asset_store,'names',return_value=[]), patch.object(asset_store,'src_names',return_value=[]):
                with self.assertRaises(asset_store.PackInvalid): asset_store.import_pack(data.getvalue(),'A'*16)


if __name__=='__main__': unittest.main()
