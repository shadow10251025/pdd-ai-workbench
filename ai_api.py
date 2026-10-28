"""Shared durable AI task routes. No credentials are written to task storage."""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from pydantic import ValidationError

from safe_http import OutboundError, read_upload
from task_store import StoreError, TaskStore, public_data, request_digest

_WORKER_SLOTS = threading.BoundedSemaphore(2)


def _json(value, fallback=None):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, "配置格式不正确，请重新选择图片方案或规格") from exc


class PlanRequest(BaseModel):
    product_hint: str = ""
    style: str = "minimal"
    rows: int = Field(3, ge=1, le=4)
    cols: int = Field(3, ge=1, le=4)
    strategy: str = "grid"
    detail_modules: list | dict | None = None
    generation_rounds: int = Field(0, ge=0, le=40)
    round_strategy: str = "auto"
    auto_module_plan: bool = True
    size_chart_enabled: bool = False
    size_chart_specs: list | dict | None = None
    selected_specs: dict = Field(default_factory=dict)
    variant_references: dict = Field(default_factory=dict)
    reference_count: int = Field(12, ge=0, le=12)
    hero_count: int = Field(6, ge=1, le=10)
    max_image_requests: int = Field(0, ge=0, le=80)
    output_size: str = ""


class AIRunner:
    def __init__(self, project_dir: Path, owner="local", store=None, production=False):
        self.project_dir = Path(project_dir).resolve()
        self.owner = owner
        self.store = store or TaskStore(self.project_dir)
        self.guard = threading.Lock()
        self.busy = False
        self.production = production

    def _paths(self, payload):
        root = (self.project_dir / "data" / "task_inputs").resolve()
        paths = [(self.project_dir / item).resolve() for item in payload.get("input_files", [])]
        if not paths or any(root not in path.parents or not path.is_file() for path in paths):
            raise StoreError("原始上传照片已过期，请重新上传")
        return paths

    def start(self, task_id: str, provider: dict, *, resume=False):
        with self.guard:
            if self.busy:
                raise HTTPException(409, "当前商品任务尚未结束，请等待或取消后再开始")
            if not _WORKER_SLOTS.acquire(blocking=False):
                raise HTTPException(429, '服务生成任务已满，请稍后再试')
            self.busy = True
        thread = threading.Thread(target=self._execute, args=(task_id, dict(provider), resume), daemon=True)
        try:
            thread.start()
        except BaseException:
            _WORKER_SLOTS.release()
            self.busy = False
            raise

    def _execute(self, task_id, provider, resume):
        from safe_http import OUTBOUND_PRODUCTION
        security_context = OUTBOUND_PRODUCTION.set(self.production)
        from ai_review import regenerate_module
        from ai_workflow import WorkflowError, run_workflow
        task = self.store.get_task(self.owner, task_id)
        last_checkpoint = task.get("checkpoint") or {}
        self.store.update_task(self.owner, task_id, status="running", progress="正在准备商品任务")

        def progress(message):
            self.store.update_task(self.owner, task_id, progress=str(message)[:600])

        def checkpoint(value):
            nonlocal last_checkpoint
            last_checkpoint = public_data(value)
            self.store.update_task(self.owner, task_id, checkpoint=last_checkpoint)
            state = str(value.get("request_state") or value.get("state") or "checkpoint")
            self.store.event(self.owner, task_id, state, module_id=str(value.get("module_id") or ""), data={"job_id": value.get("job_id"), "batch": value.get("batch")})

        try:
            payload = task["payload"]
            if task['kind']=='prepare':
                from creative_proposal import prepare_proposal
                paths=self._paths(payload)
                result=prepare_proposal(self.project_dir,[path.read_bytes() for path in paths],payload['parameters'],provider,progress=progress,
                                        should_cancel=lambda:self.store.cancelled(self.owner,task_id),source_proposal_id=payload.get('source_proposal_id',''))
            elif task["kind"] == "regenerate":
                result = regenerate_module(self.project_dir, payload["source_job_id"], payload["module_id"], payload.get("prompt", ""), provider, progress,
                                           checkpoint=checkpoint, should_cancel=lambda: self.store.cancelled(self.owner, task_id))
            elif task["kind"] == "recrop":
                from image_recrop import recrop_module
                result = recrop_module(self.project_dir, payload["source_job_id"], payload["module_id"], payload["box"])
            else:
                paths = self._paths(payload)
                args = dict(payload["parameters"])
                if payload.get('approved_proposal'):
                    args['approved_proposal']=payload['approved_proposal']
                if resume:
                    resume_job = str(last_checkpoint.get("job_id") or "")
                    if not resume_job:
                        raise WorkflowError("这份任务尚未形成可恢复批次，请重新上传开始")
                    args["resume_job_id"] = resume_job
                result = run_workflow(
                    image_bytes=[path.read_bytes() for path in paths],
                    filename=payload.get("filenames") or [path.name for path in paths],
                    project_dir=self.project_dir, provider_overrides=provider,
                    progress=progress, checkpoint=checkpoint,
                    should_cancel=lambda: self.store.cancelled(self.owner, task_id), **args,
                )
            state = "cancelled" if self.store.cancelled(self.owner, task_id) and not result else "done"
            result.update({"status": state, "task_id": task_id})
            self.store.settle(self.owner, task_id, "image", int(result.get("provider_calls") or 0))
            self.store.update_task(self.owner, task_id, status=state, progress='策划已完成，请确认规格和每图提示词后再生图' if task['kind']=='prepare' else "生成完成，请人工审核商品外观、文字和规格", result=result)
        except Exception as exc:
            cancelled = self.store.cancelled(self.owner, task_id) or getattr(exc, "code", "") == "cancelled"
            request_state = str(last_checkpoint.get("request_state") or last_checkpoint.get("state") or "")
            unknown = request_state in {"sending", "sent", "request_started", "billing_unknown", "request_sent"}
            code = "billing_unknown" if unknown else "cancelled" if cancelled else getattr(exc, "code", "workflow_error")
            safe_message = str(exc) if isinstance(exc, (WorkflowError, StoreError, OutboundError)) else "处理失败，请查看任务编号对应的本地日志"
            status = "needs_reconciliation" if unknown else "cancelled" if cancelled else "error"
            self.store.event(self.owner, task_id, code, data={"exception_type": type(exc).__name__})
            used = int(last_checkpoint.get("provider_calls") or 0)
            self.store.settle(self.owner, task_id, "image", used, unknown=unknown)
            self.store.update_task(self.owner, task_id, status=status, progress=safe_message[:600], result={"status": status, "message": safe_message[:600], "error_code": code, "can_resume": task['kind']=='workflow' and bool(last_checkpoint.get("job_id")) and not unknown and code != 'failed_charged', "job_id": last_checkpoint.get("job_id", ""), 'provider_calls': int(last_checkpoint.get('provider_calls') or 0), 'billing_status': 'unknown' if unknown else 'known_requests_only', 'proposal_id':getattr(exc,'proposal_id','')})
        finally:
            provider.clear()
            OUTBOUND_PRODUCTION.reset(security_context)
            _WORKER_SLOTS.release()
            with self.guard:
                self.busy = False

    async def submit(self, uploads, parameters, provider, client_request_id="", *, kind='workflow',source_proposal_id=''):
        from ai_workflow import MAX_UPLOAD_BYTES, WorkflowError, _open_image, build_generation_plan
        if not uploads or len(uploads) > 12:
            raise HTTPException(422, "请上传1到12张商品照片")
        raw, names, total = [], [], 0
        for upload in uploads:
            if upload.content_type and upload.content_type.lower() not in {"image/jpeg", "image/png", "image/webp", "image/bmp"}:
                raise HTTPException(415, "请上传JPG、PNG、WEBP或BMP图片")
            try:
                value = await read_upload(upload, max(1, MAX_UPLOAD_BYTES - total))
                total += len(value)
                _open_image(value)
            except (OutboundError, WorkflowError) as exc:
                raise HTTPException(422, str(exc)) from exc
            raw.append(value); names.append(str(upload.filename or "product.jpg")[:120])
        try:
            preview_fields = {key: value for key, value in parameters.items() if key in PlanRequest.model_fields}
            preview_fields["reference_count"] = len(raw)
            plan = build_generation_plan(**preview_fields)
        except (ValueError, WorkflowError) as exc:
            raise HTTPException(422, str(exc)) from exc
        if kind!='prepare':
            parameters["max_image_requests"] = parameters.get("max_image_requests") or plan["planned_image_requests"]
        digest = request_digest({"parameters": parameters, "photos": [hashlib.sha256(value).hexdigest() for value in raw]})
        task, created = self.store.create_task(self.owner, kind, {"parameters": parameters, "filenames": names, "digest": digest,'source_proposal_id':source_proposal_id}, client_request_id)
        if not created:
            if task['kind']!=kind or task["payload"].get("digest") != digest:
                raise HTTPException(409, "同一提交编号对应不同商品，请重新开始提交")
            return {"task_id": task["id"], "status": task["status"], "duplicate": True}
        if self.busy:
            self.store.update_task(self.owner, task["id"], status="error", result={"status": "error", "message": "已有任务执行中"})
            raise HTTPException(409, "当前商品任务尚未结束，请等待或取消")
        input_dir = self.project_dir / "data" / "task_inputs" / task["id"]
        from local_drafts import check_capacity
        check_capacity(self.project_dir, total)
        input_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for index, value in enumerate(raw):
            path = input_dir / f"upload_{index + 1:02d}.img"
            path.write_bytes(value)
            paths.append(path.relative_to(self.project_dir).as_posix())
        payload = {**task["payload"], "input_files": paths}
        # Persisting payload here keeps credentials out of the database.
        with self.store.connection() as conn:
            conn.execute("UPDATE tasks SET payload=? WHERE id=? AND owner=?", (json.dumps(public_data(payload), ensure_ascii=False), task["id"], self.owner))
        try:
            if kind!='prepare':
                self.store.reserve(self.owner, task["id"], "image", parameters["max_image_requests"])
            self.start(task["id"], provider)
        except (StoreError, HTTPException) as exc:
            self.store.settle(self.owner, task['id'], 'image', 0)
            self.store.update_task(self.owner, task["id"], status="error", result={"status": "error", "message": str(exc)})
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(403, str(exc)) from exc
        return {"task_id": task["id"], "message": "已开始生成", "plan": plan}


def install_ai_routes(app, project_dir: Path, require_license: Callable, bg_tasks: dict | None = None, *, owner='local', production=False):
    router = APIRouter(dependencies=[Depends(require_license)])
    runner = AIRunner(project_dir, owner=owner, production=production)
    runner.store.interrupt_startup()
    app.state.ai_runner = runner

    @router.post("/api/ai/plan")
    def plan(request: PlanRequest):
        from ai_workflow import WorkflowError, build_generation_plan
        try:
            return build_generation_plan(**request.model_dump())
        except (ValueError, WorkflowError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get('/api/ai/capabilities')
    def capabilities():
        import ai_workflow as w
        from workflow_engine import _config
        config,kind=_config(w,runner.project_dir,{})
        return {'image_mode':kind,'prompt_configured':bool(config.prompt_api_url),'session_keys_only':True}

    @router.get('/api/ai/requests/{request_key}')
    def submitted_request(request_key:str):
        if not 1<=len(request_key)<=160:raise HTTPException(422,'提交编号无效')
        with runner.store.connection() as connection:
            row=connection.execute('SELECT id,status,kind FROM tasks WHERE owner=? AND request_key=?',(runner.owner,request_key)).fetchone()
        return {'found':bool(row),**({'task_id':row['id'],'status':row['status'],'kind':row['kind']} if row else {})}

    @router.post('/api/ai/prepare')
    async def prepare(images:list[UploadFile]=File(default=[]),options_json:str=Form('{}'),source_proposal_id:str=Form(''),client_request_id:str=Form(''),
                      prompt_api_url:str=Form(''),prompt_api_key:str=Form(''),prompt_model:str=Form(''),
                      vision_api_url:str=Form(''),vision_api_key:str=Form(''),vision_model:str=Form('')):
        from creative_proposal import load_proposal, merchant_metadata
        from ai_workflow import WorkflowError, _flag
        options=_json(options_json,{})
        if not isinstance(options,dict):raise HTTPException(422,'策划设置格式错误')
        try:
            validated=PlanRequest.model_validate(options).model_dump(exclude={'reference_count'})
            if validated['style'] not in {'minimal','fresh','luxury','tech'}:raise WorkflowError('请选择已支持的视觉风格')
            validated.update(model_scene=_flag(options.get('model_scene')),size_chart_only_selected=_flag(options.get('size_chart_only_selected'),True),specs_user_modified=_flag(options.get('specs_user_modified')),variant_references_user_modified=_flag(options.get('variant_references_user_modified')),
                             ad_copy=str(options.get('ad_copy') or '')[:160],creative_density='conservative' if options.get('creative_density')=='conservative' else 'rich',merchant_metadata=merchant_metadata(options.get('merchant_metadata')))
            manual=options.get('manual_prompts') or {}
            if not isinstance(manual,dict) or len(manual)>64 or any(not isinstance(value,str) or not 1<=len(value.strip())<=24000 for value in manual.values()):raise WorkflowError('手动提示词格式不正确，每张最多24000字')
            validated['manual_prompts']=manual
            options=validated
        except (ValidationError,WorkflowError) as exc:raise HTTPException(422,'策划配置不正确，请检查数量、规格与文字长度') from exc
        owned_uploads=[]
        if not images and source_proposal_id:
            try:
                previous=load_proposal(runner.project_dir,source_proposal_id)
                import io
                from starlette.datastructures import Headers
                for value in previous['source_images']:
                    path=(runner.project_dir/value).resolve()
                    if (runner.project_dir/'data/ai_jobs').resolve() not in path.parents:raise WorkflowError('照片路径无效')
                    upload=UploadFile(file=io.BytesIO(path.read_bytes()),filename=path.name,headers=Headers({'content-type':'image/jpeg'}));owned_uploads.append(upload)
                images=owned_uploads
            except (WorkflowError,OSError) as exc:raise HTTPException(404,'原策划照片不可用，请重新上传') from exc
        provider={'prompt_api_url':prompt_api_url,'prompt_api_key':prompt_api_key,'prompt_model':prompt_model,'vision_api_url':vision_api_url,'vision_api_key':vision_api_key,'vision_model':vision_model}
        try:
            return await runner.submit(images,options,provider,client_request_id,kind='prepare',source_proposal_id=source_proposal_id)
        finally:
            for upload in owned_uploads:await upload.close()

    @router.post('/api/ai/confirm')
    async def confirm(proposal_id:str=Form(...),edits_json:str=Form('{}'),client_request_id:str=Form(''),
                      image_api_url:str=Form(''),image_api_key:str=Form(''),image_model:str=Form(''),image_reference_mode:str=Form('auto')):
        from creative_proposal import confirm_proposal
        from ai_workflow import WorkflowError
        try:
            approved=confirm_proposal(runner.project_dir,proposal_id,_json(edits_json,{}))
        except WorkflowError as exc:raise HTTPException(422,str(exc)) from exc
        parameter_keys=(set(PlanRequest.model_fields)-{'reference_count'})|{'model_scene','ad_copy','creative_density','size_chart_only_selected'}
        parameters={key:value for key,value in approved['options'].items() if key in parameter_keys}
        parameters['max_image_requests']=parameters.get('max_image_requests') or approved['generation_plan']['planned_image_requests']
        digest=request_digest({'proposal':proposal_id,'confirmation':approved['confirmation_digest']})
        task,created=runner.store.create_task(runner.owner,'workflow',{'parameters':parameters,'approved_proposal':approved,'digest':digest},client_request_id)
        if not created:
            if task['payload'].get('digest')!=digest:raise HTTPException(409,'提交编号已用于其他确认方案')
            return {'task_id':task['id'],'duplicate':True}
        # Reuse the original task inputs through an isolated local copy. Source
        # pictures and the confirmed prompts belong to this tenant's manifest.
        directory=runner.project_dir/'data/task_inputs'/task['id'];directory.mkdir(parents=True,exist_ok=True)
        paths=[]
        for index,value in enumerate(approved['source_images']):
            source=(runner.project_dir/value).resolve()
            if (runner.project_dir/'data/ai_jobs').resolve() not in source.parents or not source.is_file():raise HTTPException(422,'方案参考照片已过期')
            target=directory/f'input_{index:02d}.img';target.write_bytes(source.read_bytes());paths.append(target.relative_to(runner.project_dir).as_posix())
        payload={**task['payload'],'input_files':paths,'filenames':[Path(value).name for value in approved['source_images']]}
        with runner.store.connection() as connection:
            connection.execute('UPDATE tasks SET payload=? WHERE id=? AND owner=?',(json.dumps(public_data(payload),ensure_ascii=False),task['id'],runner.owner))
        try:
            runner.store.reserve(runner.owner,task['id'],'image',parameters['max_image_requests'])
            runner.start(task['id'],{'image_api_url':image_api_url,'image_api_key':image_api_key,'image_model':image_model,'image_reference_mode':image_reference_mode})
        except (StoreError,HTTPException) as exc:
            runner.store.settle(runner.owner,task['id'],'image',0);runner.store.update_task(runner.owner,task['id'],status='error',progress='确认任务未发送')
            if isinstance(exc,HTTPException):raise
            raise HTTPException(403,str(exc)) from exc
        return {'task_id':task['id'],'message':'正在执行你已确认的方案','plan':approved['generation_plan']}

    @router.post("/api/ai/workflow")
    async def workflow(
        images: list[UploadFile] = File(default=[]), image: UploadFile | None = File(default=None),
        product_hint: str = Form(""), style: str = Form("minimal"), rows: int = Form(3), cols: int = Form(3),
        strategy: str = Form("grid"), generation_rounds: int = Form(0), round_strategy: str = Form("auto"),
        model_scene: str = Form("0"), ad_copy: str = Form(""), creative_density: str = Form("rich"),
        creative_brief_override: str = Form(""), detail_modules: str = Form(""), detail_modules_json: str = Form(""),
        auto_module_plan: str = Form("1"), selected_specs_json: str = Form(""), variant_references_json: str = Form(""),
        size_chart_enabled: str = Form("0"), size_chart_specs: str = Form(""), size_chart_specs_json: str = Form(""),
        size_chart_only_selected: str = Form("1"), hero_count: int = Form(6), max_image_requests: int = Form(0),
        client_request_id: str = Form(""), image_api_url: str = Form(""), image_api_key: str = Form(""),
        image_model: str = Form(""), prompt_api_url: str = Form(""), prompt_api_key: str = Form(""),
        prompt_model: str = Form(""), image_reference_mode: str = Form("auto"),
        output_size: str = Form("2048x2048"),
    ):
        from ai_workflow import _flag
        parameters = {
            "product_hint": product_hint.strip()[:200], "style": style[:30], "rows": rows, "cols": cols,
            "strategy": strategy, "generation_rounds": generation_rounds, "round_strategy": round_strategy,
            "model_scene": _flag(model_scene), "ad_copy": ad_copy[:160], "creative_density": creative_density,
            "creative_brief_override": _json(creative_brief_override),
            "detail_modules": _json(detail_modules_json or detail_modules), "auto_module_plan": _flag(auto_module_plan, True),
            "selected_specs": _json(selected_specs_json, {}), "variant_references": _json(variant_references_json, {}),
            "size_chart_enabled": _flag(size_chart_enabled), "size_chart_specs": _json(size_chart_specs_json or size_chart_specs),
            "size_chart_only_selected": _flag(size_chart_only_selected, True), "hero_count": hero_count,
            "max_image_requests": max_image_requests,
            "output_size": output_size,
        }
        provider = {"image_api_url": image_api_url, "image_api_key": image_api_key, "image_model": image_model, "prompt_api_url": prompt_api_url, "prompt_api_key": prompt_api_key, "prompt_model": prompt_model, "image_reference_mode": image_reference_mode}
        return await runner.submit(list(images) + ([image] if image else []), parameters, provider, client_request_id)

    @router.post("/api/ai/regenerate")
    async def regenerate(source_job_id: str = Form(...), module_id: str = Form(...), prompt: str = Form(""), client_request_id: str = Form(""), image_api_url: str = Form(""), image_api_key: str = Form(""), image_model: str = Form(""), image_reference_mode: str = Form("auto")):
        from ai_review import _load_package
        from ai_workflow import WorkflowError
        try:
            package = _load_package(runner.project_dir, source_job_id)
        except WorkflowError as exc:
            raise HTTPException(404, str(exc)) from exc
        module = next((item for item in package.get("module_outputs", []) if item.get("id") == module_id), None)
        if not module:
            raise HTTPException(404, "图片模块不存在")
        payload = {"source_job_id": source_job_id, "module_id": module_id, "prompt": prompt}
        task, created = runner.store.create_task(runner.owner, "regenerate", payload, client_request_id)
        if not created and (task['kind']!='regenerate' or request_digest(task['payload'])!=request_digest(payload)):
            raise HTTPException(409, '同一提交编号对应其他模块或提示词，请重新提交')
        if created:
            try:
                runner.store.reserve(runner.owner, task["id"], "image", 0 if module.get("module_type") == "size_chart" else 1)
                runner.start(task["id"], {"image_api_url": image_api_url, "image_api_key": image_api_key, "image_model": image_model, "image_reference_mode": image_reference_mode})
            except (StoreError, HTTPException) as exc:
                runner.store.settle(runner.owner, task['id'], 'image', 0)
                runner.store.update_task(runner.owner, task['id'], status='error', progress='任务未发送，请稍后重新提交')
                if isinstance(exc, HTTPException):
                    raise
                raise HTTPException(403, str(exc)) from exc
        return {"task_id": task["id"], "duplicate": not created}

    @router.get("/api/ai/tasks/active")
    def active():
        return {"tasks": [public_data({key: value for key, value in item.items() if key != "payload"}) for item in runner.store.active_tasks(runner.owner)]}

    @router.post("/api/ai/recrop")
    async def recrop(source_job_id: str = Form(...), module_id: str = Form(...), box_json: str = Form(...), client_request_id: str = Form("")):
        from ai_review import _load_package
        from ai_workflow import WorkflowError
        try:
            _load_package(runner.project_dir, source_job_id)
        except WorkflowError as exc:
            raise HTTPException(404, str(exc)) from exc
        payload={"source_job_id": source_job_id, "module_id": module_id, "box": _json(box_json, {})}
        task, created = runner.store.create_task(runner.owner, "recrop", payload, client_request_id)
        if not created and (task['kind']!='recrop' or request_digest(task['payload'])!=request_digest(payload)):
            raise HTTPException(409, '同一提交编号对应其他裁剪设置，请重新提交')
        if created:
            runner.start(task["id"], {})
        return {"task_id": task["id"], "duplicate": not created}

    @router.get("/api/ai/tasks/{task_id}")
    def status(task_id: str):
        task = runner.store.get_task(runner.owner, task_id)
        if not task:
            raise HTTPException(404, "任务不存在或已过期")
        return {**task["result"], "status": task["status"], "progress": task["progress"], "task_id": task_id, "checkpoint": task["checkpoint"]}

    @router.post("/api/ai/tasks/{task_id}/cancel")
    def cancel(task_id: str):
        if not runner.store.cancel(runner.owner, task_id):
            raise HTTPException(404, "任务不存在")
        return {"ok": True, "message": "已请求停止后续批次；已发出的请求可能仍会计费"}

    @router.post("/api/ai/tasks/{task_id}/resume")
    async def resume(task_id: str, image_api_url: str = Form(""), image_api_key: str = Form(""), image_model: str = Form(""), prompt_api_url: str = Form(""), prompt_api_key: str = Form(""), prompt_model: str = Form(""), image_reference_mode: str = Form("auto")):
        task = runner.store.get_task(runner.owner, task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        if task["status"] == "needs_reconciliation":
            raise HTTPException(409, "上次请求扣费状态未知，请先核对服务商记录，避免重复扣费")
        if task["status"] not in {"error", "interrupted", "cancelled"}:
            raise HTTPException(409, "当前任务不需要恢复")
        if task['kind'] != 'workflow' or not task.get('checkpoint', {}).get('job_id'):
            raise HTTPException(409, '当前任务没有可恢复批次，请重新提交所需图片模块')
        try:
            runner.store.reopen_reservation(runner.owner, task_id, 'image')
        except StoreError as exc:
            raise HTTPException(403, str(exc)) from exc
        runner.store.reset_cancel(runner.owner, task_id)
        runner.start(task_id, {"image_api_url": image_api_url, "image_api_key": image_api_key, "image_model": image_model, "prompt_api_url": prompt_api_url, "prompt_api_key": prompt_api_key, "prompt_model": prompt_model, "image_reference_mode": image_reference_mode}, resume=True)
        return {"task_id": task_id}

    @router.get("/api/ai/usage")
    def usage():
        return runner.store.entitlement(runner.owner)

    @router.post("/api/ai/providers/test")
    def provider_test(request: dict):
        from safe_http import OutboundError, request_json
        from urllib.parse import urlsplit, urlunsplit
        value = str(request.get("url") or "").strip()
        parts = urlsplit(value)
        base_path = parts.path
        for suffix in ("/images/generations", "/images/edits", "/chat/completions"):
            if base_path.endswith(suffix):
                base_path = base_path[:-len(suffix)]
        endpoint = urlunsplit((parts.scheme, parts.netloc, base_path.rstrip("/") + "/models", "", ""))
        try:
            request_json("GET", endpoint, key=str(request.get("api_key") or ""), timeout=15, max_bytes=2 * 1024 * 1024)
        except OutboundError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"ok": True, "message": "模型列表接口连接成功；未执行生图，不代表已验证生成能力"}

    app.include_router(router)
    return runner
