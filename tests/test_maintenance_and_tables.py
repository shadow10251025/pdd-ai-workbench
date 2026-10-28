import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
import ai_workflow as w
from maintenance import backup, cleanup, restore_backup
from task_store import TaskStore


class MaintenanceTablesTests(unittest.TestCase):
    def test_sku_excel_roundtrip_preserves_zero_disabled_code_and_dimension_order(self):
        import asyncio
        from sku_workbook import parse_workbook
        import web.app as app
        with tempfile.TemporaryDirectory() as temp, patch.object(app,'DATA_DIR',Path(temp)):
            request=app.SkuListRequest(specs={'颜色':['red'],'尺码':['S','M']},skus=[{'name':'red S','spec_values':{'颜色':'red','尺码':'S'},'price':3.5,'single_price':4.5,'stock':0,'spec_code':'009','enabled':False},{'name':'red M','price':5,'single_price':6,'stock':2,'spec_code':'custom-10','enabled':True}])
            exported=asyncio.run(app.generate_sku_excel(request))
            rows=parse_workbook((Path(temp)/exported['filename']).read_bytes())
            self.assertEqual(rows[0]['stock'],0);self.assertFalse(rows[0]['enabled']);self.assertEqual(rows[0]['spec_code'],'009')
            self.assertEqual(rows[1]['spec_code'],'custom-10');self.assertEqual(rows[1]['price'],5)
            self.assertEqual(rows[0]['name'],'red S');self.assertEqual(rows[0]['spec_values'],{'颜色':'red','尺码':'S'})

    def test_size_table_pagination_preserves_every_value(self):
        rows=[{'size':'S'+str(index),'measurements':{'field'+str(column):str(index*10+column)+' cm' for column in range(7)}} for index in range(17)]
        plan=w.build_generation_plan(hero_count=1,detail_modules=[{'type':'size_chart','count':1}],size_chart_enabled=True,size_chart_specs=rows,selected_specs={'尺码':[row['size'] for row in rows]},output_size='2048x2048')
        pages=[item for item in plan['module_outputs'] if item['module_type']=='size_chart']
        self.assertEqual(len(pages),6); self.assertEqual(plan['planned_image_requests'],1)
        seen={}
        with tempfile.TemporaryDirectory() as temp:
            for page in pages:
                chart=page['table_page']; file=Path(temp)/(page['id']+'.jpg')
                w._render_verified_size_chart(chart['rows'],file,columns=chart['columns'],page_label=page['page_label'])
                self.assertTrue(file.is_file())
                for row in chart['rows']:
                    seen.setdefault(row['size'],{}).update({key:row['measurements'][key] for key in chart['columns']})
        self.assertEqual(seen,{row['size']:row['measurements'] for row in rows})
        with self.assertRaises(w.WorkflowError):
            w.build_generation_plan(size_chart_enabled=True,size_chart_specs=[{'size':'M','measurements':{'chest':'100'}}])

    def test_cleanup_protects_draft_references_and_previous_versions(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); store=TaskStore(root); jobs=root/'data/ai_jobs'
            keep=jobs/('a'*16); previous=jobs/('b'*16); unused=jobs/('c'*16)
            for job in (keep,previous,unused):
                job.mkdir(parents=True); Image.new('RGB',(640,640),'red').save(job/'image.jpg')
                (job/'review_package.json').write_text(json.dumps({'job_id':job.name,'parent_job_id':'b'*16 if job==keep else ''}),'utf-8')
                os.utime(job,(time.time()-60*86400,)*2)
            store.save_draft('local',{'id':'saved','main_images':['data/ai_jobs/'+keep.name+'/image.jpg']})
            dry=cleanup(root)
            self.assertEqual(dry['candidates'],['data\\ai_jobs\\'+unused.name] if os.name=='nt' else ['data/ai_jobs/'+unused.name])
            self.assertTrue(unused.exists())
            cleanup(root,apply=True)
            self.assertTrue(keep.exists()); self.assertTrue(previous.exists()); self.assertFalse(unused.exists())

    def test_backup_restore_checks_hashes_and_rejects_workspace_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp); root=base/'app'; store=TaskStore(root)
            store.save_draft('a',{'id':'one','title':'complete','stock':0})
            with self.assertRaises(ValueError): backup(root,root/'backups')
            archive=base/'backup'; result=backup(root,archive); self.assertGreater(result['files'],0)
            restored=base/'restored';restore_backup(archive,restored)
            self.assertEqual(TaskStore(restored).get_draft('a','one')['stock'],0)
            (archive/'data/workbench.sqlite3').write_bytes(b'tampered')
            with self.assertRaises(ValueError): restore_backup(archive,base/'bad-restore')

    def test_code_release_excludes_keys_user_data_and_account_databases(self):
        script=Path(__file__).resolve().parents[1]/'tools/release_workbench.py'
        spec=importlib.util.spec_from_file_location('release_workbench',script); module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        release=module.manifest(script.parent.parent)
        self.assertFalse(any('private_key' in path or '/data/' in path or 'licenses.db' in path for path in release['files']))
        self.assertIn('skill_template.lock.json',release['files']); self.assertIn('web/static/api_client.js',release['files'])


if __name__=='__main__': unittest.main()
