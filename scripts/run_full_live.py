import json,time,urllib.request
BASE='http://127.0.0.1:8014'; project='1ec19ffa-d5e3-40f9-8cde-7ea2fad3b705'
def req(path,method='GET',body=None):
    data=json.dumps(body).encode() if body is not None else None; h={'Content-Type':'application/json'} if data else {}
    return json.loads(urllib.request.urlopen(urllib.request.Request(BASE+path,data=data,headers=h,method=method),timeout=180).read())
r=req(f'/api/projects/{project}/continuation-runs','POST',{'target_chars':4000,'user_requirements':'保持原作人物、世界观和叙事风格，承接第十二章结尾继续写作','routing_preset':'deepseek-only'})['run']['id']; print('RUN',r)
for _ in range(180):
    time.sleep(5); run=req('/api/runs/'+r)['run']; print(run['status'],run.get('stage'),run.get('metrics',{}).get('calls'),run.get('error'))
    if run['status']=='awaiting_outline_approval':
        a=next(x for x in run['artifacts'] if x['kind']=='run_plan'); req('/api/runs/'+r+'/outline/approve','POST',{'plan_hash':a['sha256']}); print('APPROVED')
    if run['status'] in ('completed','failed','needs_human','cancelled'): break
print(json.dumps(req('/api/runs/'+r),ensure_ascii=False)[:12000])
