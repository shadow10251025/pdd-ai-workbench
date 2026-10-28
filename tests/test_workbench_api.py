import asyncio
import copy
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

import ai_workflow as w
from ai_api import install_ai_routes
from listing_readback import identity_matches, verify_form


def photo(color='red'):
    result=io.BytesIO(); Image.new('RGB',(700,700),color).save(result,'JPEG'); return result.getvalue()


class WorkbenchAPITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); app=FastAPI(); self.runner=install_ai_routes(app,self.root,lambda:None)
        self.client=TestClient(app); self.addCleanup(self.client.close)
        self.metadata=w._validate_metadata({'title':'workflow test','specs':{},'scene_suggestions':['desk']},'test')
        self.environment=patch.dict(os.environ,{'PDD_AI_PROVIDER':'mock','PDD_AI_DEMO':'','PDD_IMAGE_API_URL':'','PDD_IMAGE_API_KEY':'','PDD_PROMPT_API_URL':'','PDD_PROMPT_API_KEY':'','PDD_AI_IMAGE_CACHE':'0'})
        self.environment.start(); self.addCleanup(self.environment.stop)

    def sheet(self, config, prompt, input_image, out_path, rows=2,cols=3,progress=None,trace=None,reference_images=None):
        image=Image.new('RGB',(cols*540,rows*540),'white'); draw=ImageDraw.Draw(image)
        for index in range(rows*cols):
            x,y=(index%cols)*540,(index//cols)*540; draw.rectangle((x+25,y+25,x+515,y+515),fill=(index*32%255,100,150))
        Path(out_path).parent.mkdir(parents=True,exist_ok=True); image.save(out_path)
        if trace is not None: trace.update(mode='offline',reference_sent=False,endpoint_kind='none')
        return image

    def wait(self,task_id):
        for _ in range(100):
            response=self.client.get('/api/ai/tasks/'+task_id); self.assertEqual(response.status_code,200,response.text)
            task=response.json()
            if task['status'] not in {'queued','running','cancel_requested'}: return task
            time.sleep(.03)
        self.fail('bounded task wait exceeded')

    def test_plan_is_free_and_workflow_consumes_locked_count_and_all_uploads(self):
        with patch.object(w,'generate_metadata',return_value=copy.deepcopy(self.metadata)), patch.object(w,'generate_sheet',side_effect=self.sheet) as generator:
            preview=self.client.post('/api/ai/plan',json={'hero_count':2,'detail_modules':[],'reference_count':2,'output_size':'2048x2048'})
            self.assertEqual(preview.status_code,200,preview.text); generator.assert_not_called()
            self.assertEqual(preview.json()['detail_count'],0)
            files=[('images',('red.jpg',photo(),'image/jpeg')),('images',('blue.jpg',photo('blue'),'image/jpeg'))]
            data={'hero_count':'2','detail_modules_json':'[]','auto_module_plan':'0','max_image_requests':'1','output_size':'2048x2048','client_request_id':'one-upload'}
            response=self.client.post('/api/ai/workflow',data=data,files=files)
            self.assertEqual(response.status_code,200,response.text); task_id=response.json()['task_id']
            task=self.wait(task_id); self.assertEqual(task['status'],'done',str(task))
            result=task; self.assertEqual(result['source_image_count'],2)
            self.assertEqual(len(result['main_images']),2); self.assertEqual(result['detail_images'],[])
            self.assertEqual(result['provider_calls'],0); self.assertEqual(result['generation_kind'],'offline_demo')
            self.assertEqual(generator.call_count,1)
            repeated=self.client.post('/api/ai/workflow',data=data,files=files)
            self.assertTrue(repeated.json()['duplicate']); self.assertEqual(repeated.json()['task_id'],task_id)
            self.assertEqual(generator.call_count,1)
            for module in result['module_outputs']:
                self.assertTrue((self.root/module['path']).exists()); self.assertEqual(module['source_width'],1080)
            # One local crop changes only one module, preserves the previous file,
            # and creates no provider request.
            before=(self.root/result['main_images'][0]).read_bytes()
            module=result['module_outputs'][0]; width,height=module['source_width'],module['source_height']
            box=dict(zip(('left','top','right','bottom'),[module['crop_box'][0]/width,module['crop_box'][1]/height,module['crop_box'][2]/width,module['crop_box'][3]/height]))
            crop=self.client.post('/api/ai/recrop',data={'source_job_id':result['job_id'],'module_id':module['id'],'box_json':json.dumps(box),'client_request_id':'crop-one'})
            self.assertEqual(crop.status_code,200,crop.text); revised=self.wait(crop.json()['task_id'])
            self.assertEqual(revised['status'],'done',str(revised))
            self.assertEqual(revised['main_images'][1],result['main_images'][1]); self.assertNotEqual(revised['main_images'][0],result['main_images'][0])
            self.assertEqual((self.root/result['main_images'][0]).read_bytes(),before); self.assertEqual(generator.call_count,1)

    def test_invalid_plan_and_missing_reference_do_not_start_paid_work(self):
        self.assertEqual(self.client.post('/api/ai/plan',json={'hero_count':0}).status_code,422)
        self.assertEqual(self.client.post('/api/ai/workflow',files={'images':('x.jpg',b'not a photo','image/jpeg')}).status_code,422)
        self.assertEqual(self.client.post('/api/ai/regenerate',data={'source_job_id':'../private','module_id':'one'}).status_code,404)
        self.assertEqual(self.client.get('/api/ai/tasks/'+'a'*32).status_code,404)

    def test_cancel_resume_reuses_finished_batches_and_cumulative_counts(self):
        calls=[]; checkpoints=[]
        def sheet(*args,**kwargs): calls.append(1); return self.sheet(*args,**kwargs)
        args=dict(image_bytes=[photo()],filename=['one.jpg'],product_hint='test',style='fresh',rows=2,cols=2,project_dir=self.root,strategy='grid',hero_count=6,detail_modules=[],auto_module_plan=False,output_size='2048x2048',checkpoint=checkpoints.append)
        with patch.object(w,'generate_metadata',return_value=copy.deepcopy(self.metadata)) as metadata, patch.object(w,'generate_sheet',side_effect=sheet):
            with self.assertRaises(w.WorkflowError) as error: w.run_workflow(**args,should_cancel=lambda:bool(calls))
            self.assertEqual(error.exception.code,'cancelled'); self.assertEqual(len(calls),1)
            job=checkpoints[-1]['job_id']; done=w.run_workflow(**args,resume_job_id=job)
            self.assertEqual(len(calls),2); self.assertEqual(len(done['main_images']),6)
            self.assertEqual(metadata.call_count,1); self.assertTrue(done['generation_trace'][0]['resumed'])

    def test_failed_unknown_request_is_not_retried_on_resume(self):
        checkpoints=[]
        def failing(config,*args,**kwargs):
            config.request_hook('request_sent')
            raise w.WorkflowError('provider timed out')
        args=dict(image_bytes=[photo()],filename=['one.jpg'],product_hint='test',style='fresh',rows=2,cols=2,project_dir=self.root,strategy='grid',hero_count=1,detail_modules=[],auto_module_plan=False,output_size='2048x2048',checkpoint=checkpoints.append,provider_overrides={'image_api_url':'https://8.8.8.8/v1','image_api_key':'private-key'})
        with patch.object(w,'generate_metadata',return_value=copy.deepcopy(self.metadata)), patch.object(w,'generate_sheet',side_effect=failing) as generate:
            with self.assertRaises(w.WorkflowError) as error: w.run_workflow(**args)
            self.assertEqual(error.exception.code,'billing_unknown')
            with self.assertRaises(w.WorkflowError) as error: w.run_workflow(**args,resume_job_id=checkpoints[-1]['job_id'])
            self.assertEqual(error.exception.code,'billing_unknown'); self.assertEqual(generate.call_count,1)

    def test_listing_preflight_and_shop_identity_stop_before_browser(self):
        import web.app as web
        req=web.StartListingRequest(url='',title='',shops=[])
        with patch.object(web.license,'check_now',return_value={'ok':True}), patch('launcher.shop_slots') as windows:
            with self.assertRaises(Exception) as error: asyncio.run(web.start_listing(req))
            self.assertEqual(error.exception.status_code,422); windows.assert_not_called()
        self.assertFalse(identity_matches({},{})); self.assertFalse(identity_matches({'mallId':'1'},{'mallId':'2'}))
        self.assertTrue(identity_matches({'id':'1'},{'mallId':1}))
        cdp=type('CDP',(),{'js':AsyncMock(return_value=json.dumps({'title':'product','main_count':1,'detail_count':0,'rows':[{'text':'red M','values':['3.00','4.00','0']}]}))})()
        payload={'title':'product','main_images':['one'],'detail_images':[],'skus':[{'name':'red M','price':3,'single_price':4,'stock':0}]}
        good=asyncio.run(verify_form(cdp,payload)); self.assertTrue(good['verified'])
        bad=asyncio.run(verify_form(cdp,payload,warnings=['price not confirmed'])); self.assertFalse(bad['verified']); self.assertEqual(bad['status'],'needs_review')


if __name__=='__main__': unittest.main()
