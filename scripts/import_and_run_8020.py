import urllib.request,json,uuid,time
src=urllib.request.urlopen('http://127.0.0.1:8014/api/artifacts/890370fd-e492-451c-b313-c7c6cdd63f96/preview').read()
boundary='----Codex'+uuid.uuid4().hex
body=(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="示例素材.md"\r\nContent-Type: text/plain\r\n\r\n'.encode()+src+f'\r\n--{boundary}--\r\n'.encode())
req=urllib.request.Request('http://127.0.0.1:8020/api/projects/import',data=body,headers={'Content-Type':'multipart/form-data; boundary='+boundary},method='POST')
p=json.loads(urllib.request.urlopen(req).read())['project']; print('PROJECT',p['id'])
def call(path,method='GET',obj=None):
 d=json.dumps(obj).encode() if obj else None; h={'Content-Type':'application/json'} if d else {}; return json.loads(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8020'+path,data=d,headers=h,method=method),timeout=180).read())
rid=call(f"/api/projects/{p['id']}/continuation-runs",'POST',{'target_chars':4000,'user_requirements':'保持原作人物、世界观和叙事风格，承接示例素材结尾继续写作','routing_preset':'deepseek-only'})['run']['id']; print('RUN',rid)
for _ in range(180):
 time.sleep(5); run=call('/api/runs/'+rid)['run']; print(run['status'],run.get('stage'),run.get('metrics',{}).get('calls'),run.get('error'))
 if run['status']=='awaiting_outline_approval':
  a=next(x for x in run['artifacts'] if x['kind']=='run_plan'); call('/api/runs/'+rid+'/outline/approve','POST',{'plan_hash':a['sha256']})
 if run['status'] in ('completed','failed','needs_human','cancelled'): break
