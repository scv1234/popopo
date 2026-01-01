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
async def get_honey_pots():
    """수익성과 안전성이 검증된 마켓 리스트를 점수 순으로 반환합니다."""
    # main.py의 스캔 및 점수화 로직 호출
    candidates = await bot.get_all_candidates_scored() 
    return candidates

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