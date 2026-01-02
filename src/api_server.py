# src/api_server.py
import asyncio
import sys
import sqlite3  # [추가]
import json     # [추가]
from contextlib import asynccontextmanager # [추가]

# [필수] 윈도우 환경에서 Playwright의 비동기 subprocess 실행을 위해 반드시 필요합니다.
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, HTTPException
from src.main import MarketMakerBot
from src.config import get_settings
from pydantic import BaseModel

settings = get_settings()
bot = MarketMakerBot(settings)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 서버 시작 시 봇 루프 실행
    bot_task = asyncio.create_task(bot.run())
    yield
    # 서버 종료 시 봇 정리
    await bot.cleanup()
    bot_task.cancel()

app = FastAPI(lifespan=lifespan)

# 주문 요청을 위한 데이터 모델
class OrderRequest(BaseModel):
    market_id: str
    shares: float

@app.get("/honey-pots")
def get_honey_pots():
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    
    try:
        # [수정] 테이블이 없을 경우를 대비해 초기화 쿼리 실행
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS honeypots (
                id TEXT PRIMARY KEY,
                data TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('SELECT data FROM honeypots')
        rows = cursor.fetchall()
        return [json.loads(row[0]) for row in rows]
    except Exception as e:
        # 아직 데이터가 없는 초기 상태라면 빈 리스트 반환
        return []
    finally:
        conn.close()

@app.get("/status")
async def get_status():
    """봇의 현재 상태(인벤토리, 시장 정보, 중단 여부)를 반환합니다."""
    
    # 실시간 시장 데이터 계산
    best_bid = float(bot.current_orderbook.get("best_bid", 0))
    best_ask = float(bot.current_orderbook.get("best_ask", 1))
    mid_price = bot.quote_engine.calculate_mid_price(best_bid, best_ask)
    
    # 봇에 저장된 max_spread 기반 safety_margin 계산
    # QuoteEngine의 로직(90% 지점)을 반영하여 프론트엔드에 표시
    current_max_spread = getattr(bot, 'current_max_spread', 0.0)
    safety_margin = current_max_spread * 0.9 

    return {
        "is_halted": bot.risk_manager.is_halted,
        "inventory": {
            "yes": bot.inventory_manager.inventory.yes_position,
            "no": bot.inventory_manager.inventory.no_position,
            "net_shares": bot.inventory_manager.inventory.net_exposure_shares,
            "skew": bot.inventory_manager.inventory.get_skew()
        },
        "market": {
            "market_id": settings.market_id,
            "mid_price": round(mid_price, 4),
            "safety_margin": round(safety_margin, 4),
            "best_bid": best_bid,
            "best_ask": best_ask,
            "max_spread": current_max_spread
        }
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
    
    # 이 호출은 내부적으로 risk_manager.validate_order를 거치므로 안전합니다.
    success = await bot.execute_manual_safety_order(req.market_id, req.shares)
    if not success:
        raise HTTPException(status_code=500, detail="Order placement failed or rejected by risk manager.")
    
    return {"status": "success"}

@app.get("/wallet")
async def get_wallet():
    """지갑의 USDC 잔고 및 상세 정보를 반환합니다."""
    # 실제 구현 시에는 web3 라이브러리를 사용해 RPC로 잔고를 조회해야 합니다.
    # 여기서는 시뮬레이션을 위해 봇 객체나 설정을 참조하는 구조만 잡습니다.
    return {
        "address": settings.public_address,
        "usdc_balance": 1234.56, # 예시 데이터
        "native_token": "Polygon MATIC"
    }

@app.get("/open-orders")
async def get_open_orders():
    """현재 거래소에 걸려 있는 활성 주문 리스트를 반환합니다."""
    orders = []
    for order_id, details in bot.open_orders.items():
        # 리워드 범위 계산을 위해 현재 시장가와 비교 로직 포함
        orders.append({
            "order_id": order_id,
            "side": details.get("side"),
            "price": float(details.get("price")),
            "size": float(details.get("size")),
            "token_id": details.get("token_id")
        })
    return orders

@app.get("/logs")
async def get_logs():
    """최근 로그 파일의 마지막 20줄을 반환합니다."""
    try:
        with open("bot.log", "r", encoding="utf-8") as f:
            lines = f.readlines()
            return lines[-20:]
    except:
        return ["로그 파일을 찾을 수 없습니다."]    