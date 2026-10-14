import asyncio,httpx,os
async def main():
 h={'authorization':'Bearer sk-3305cbe4a0594ca29fcd8e518c809e99','content-type':'application/json'}; b={'model':'deepseek-chat','messages':[{'role':'user','content':'OK'}],'max_tokens':8}
 try:
  async with httpx.AsyncClient(timeout=30) as c:
   r=await c.post('https://api.deepseek.com/chat/completions',headers=h,json=b);print(r.status_code,r.text[:500])
 except Exception as e: print(type(e).__name__,repr(e))
asyncio.run(main())
