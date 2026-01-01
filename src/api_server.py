# src/api_server.py
import asyncio
import sys

# [필수] 윈도우 환경에서 Playwright의 비동기 subprocess 실행을 위해 반드시 필요합니다.
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, HTTPException
from src.main import MarketMakerBot
from src.config import get_settings
from pydantic import BaseModel

app = FastAPI()
settings = get_settings()
bot = MarketMakerBot(settings)

# 주문 요청을 위한 데이터 모델
class OrderRequest(BaseModel):
    market_id: str
    shares: float

@app.get("/honey-pots")
def get_honey_pots():
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    cursor.execute('SELECT data FROM honeypots')
    rows = cursor.fetchall()
    conn.close()
    
    return [json.loads(row[0]) for row in rows]

@app.get("/status")
async def get_status():
    """봇의 현재 상태(인벤토리, Circuit Breaker 중단 여부)를 반환합니다."""
    return {
        "is_halted": bot.risk_manager.is_halted,
        "inventory": {
            "yes": bot.inventory_manager.inventory.yes_position,
            "no": bot.inventory_manager.inventory.no_position,
            "net_shares": bot.inventory_manager.inventory.net_exposure_shares
        },
        "current_market": settings.market_id
    }

@app.post("/reset-halt")
async def reset_halt():
    """Circuit Breaker로 중단된 봇을 다시 가동시킵니다."""
    bot.risk_manager.reset_halt()
    return {"status": "success", "message": "System resumed"}

@app.post("/place-semi-auto-order")
async def place_semi_auto_order(req: OrderRequest):
    """사용자가 입력한 수량으로 안전 끝단 주문을 실행합니다."""
    if bot.risk_manager.is_halted:
        raise HTTPException(status_code=400, detail="System is halted. Please reset first.")
    
    # main.py의 수동 주문 집행 로직 호출
    success = await bot.execute_manual_safety_order(req.market_id, req.shares)
    return {"status": "success" if success else "failed"}