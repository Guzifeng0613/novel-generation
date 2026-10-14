import asyncio,os,sys
sys.path.insert(0,os.path.dirname(os.path.dirname(__file__)))
from backend.app.providers import DeepSeekChatProvider,LLMRequest,AgentRole
async def main():
 p=DeepSeekChatProvider(api_key=os.getenv('DEEPSEEK_API_KEY') or 'sk-3305cbe4a0594ca29fcd8e518c809e99')
 print(p.base_url)
 r=await p.complete('deepseek-chat',LLMRequest(system_prompt='You are helpful.',user_prompt='Reply OK',role=AgentRole.PLANNER,max_output_tokens=8))
 print(r.text)
asyncio.run(main())
