# src/api_server.py
import asyncio
import sys
import sqlite3
import json
from contextlib import asynccontextmanager

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from src.main import MarketMakerBot
from src.config import get_settings
from src.logging_config import configure_logging # [추가]
from pydantic import BaseModel
from web3 import Web3

settings = get_settings()
configure_logging(settings.log_level)
bot = MarketMakerBot(settings)

# USDC (Polygon) 컨트랙트 주소 및 최소 ABI
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"}
]

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 서버 시작 시 봇 루프 실행
    bot_task = asyncio.create_task(bot.run())
    yield
    # 서버 종료 시 봇 정리
    await bot.cleanup()
    bot_task.cancel()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class OrderRequest(BaseModel):
    market_id: str
    amount: float
    yes_token_id: str
    no_token_id: str

@app.get("/honey-pots")
def get_honey_pots():
    """DB에서 꿀통 데이터를 읽어옵니다."""
    conn = sqlite3.connect('bot_data.db')
    cursor = conn.cursor()
    try:
        cursor.execute('CREATE TABLE IF NOT EXISTS honeypots (id TEXT PRIMARY KEY, data TEXT, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)')
        cursor.execute('SELECT data FROM honeypots')
        rows = cursor.fetchall()
        return [json.loads(row[0]) for row in rows]
    except Exception:
        return []
    finally:
        conn.close()

@app.get("/status")
async def get_status():
    """대시보드 실시간 모니터링을 위한 상태 정보를 반환합니다."""
    # 실시간 호가 정보
    best_bid = float(bot.current_orderbook.get("best_bid", 0))
    best_ask = float(bot.current_orderbook.get("best_ask", 1))
    mid_price = bot.quote_engine.calculate_mid_price(best_bid, best_ask)
    
    # 리워드 세이프티 마진 계산
    current_spread = getattr(bot, 'spread_cents', 3)

    return {
        "is_halted": bot.risk_manager.is_halted,
        "is_locked": bot.state_lock.locked(), # [추가] 마켓 전환 중(Lock) 여부
        "inventory": {
            "yes": bot.inventory_manager.inventory.yes_position,
            "no": bot.inventory_manager.inventory.no_position,
            "net_shares": bot.inventory_manager.inventory.net_exposure_shares,
            "skew": bot.inventory_manager.inventory.get_skew()
        },
        "market": {
            # [수정] settings.market_id 대신 현재 활성 마켓 ID 사용
            "market_id": bot.current_market_id, 
            "mid_price": round(mid_price, 4),
            "margin_usd": round(current_spread * 0.9 / 100.0, 4),
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread_cents": current_spread
        }
    }

@app.post("/place-semi-auto-order")
async def place_semi_auto_order(req: OrderRequest):
    """대시보드에서 누른 '유동성 공급' 버튼을 처리합니다."""
    if bot.risk_manager.is_halted:
        raise HTTPException(status_code=400, detail="시스템이 중단되었습니다. 먼저 리셋 버튼을 눌러주세요.")
    
    success = await bot.execute_manual_safety_order(
        req.market_id, 
        req.amount, 
        req.yes_token_id, 
        req.no_token_id
    )
    if not success:
        raise HTTPException(status_code=500, detail="주문 전송에 실패했습니다. 로그를 확인하세요.")
    
    return {"status": "success"}

# [추가] 봇 리셋 엔드포인트
@app.post("/reset-bot")
async def reset_bot():
    """서킷 브레이커로 중단된 봇을 수동으로 재개합니다."""
    if bot.risk_manager.is_halted:
        bot.risk_manager.reset_halt()
        return {"status": "success", "message": "봇이 정상 상태로 재설정되었습니다."}
    
    return {"status": "ignored", "message": "봇이 이미 정상 작동 중입니다."}

@app.get("/wallet")
async def get_wallet():
    """RPC를 통해 실제 지갑의 USDC 및 MATIC 잔고를 조회합니다."""
    try:
        w3 = Web3(Web3.HTTPProvider(settings.rpc_url))
        address = Web3.to_checksum_address(settings.public_address)
        
        native_balance = w3.eth.get_balance(address)
        matic_balance = w3.from_wei(native_balance, 'ether')
        
        usdc_contract = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_ABI)
        raw_balance = usdc_contract.functions.balanceOf(address).call()
        decimals = usdc_contract.functions.decimals().call()
        usdc_balance = raw_balance / (10 ** decimals)

        return {
            "address": settings.public_address,
            "usdc_balance": round(usdc_balance, 2),
            "matic_balance": round(float(matic_balance), 4),
            "native_token": "Polygon MATIC"
        }
    except Exception as e:
        print(f"❌ 잔고 조회 실패: {e}")
        return {
            "address": settings.public_address,
            "usdc_balance": 0.0,
            "matic_balance": 0.0,
            "error": str(e)
        }

@app.get("/open-orders")
async def get_open_orders():
    """현재 거래소 활성 주문 리스트 (마켓 및 결과 정보 포함)"""
    orders = []
    for order_id, details in bot.open_orders.items():
        orders.append({
            "order_id": order_id,
            "market": details.get("market", "Unknown"), # 마켓 ID 추가
            "outcome": details.get("outcome", "Unknown"), # YES/NO 추가
            "side": details.get("side"),
            "price": float(details.get("price")),
            "size": float(details.get("size"))
        })
    return orders

@app.post("/batch-cancel-manual")
async def batch_cancel_manual():
    success = await bot.batch_cancel_manual_orders()
    return {"status": "success" if success else "failed"}    

@app.post("/cancel-order/{order_id}")
async def cancel_order(order_id: str):
    """특정 주문 ID를 사용하여 개별 취소"""
    success = await bot.cancel_single_order(order_id)
    if not success:
        raise HTTPException(status_code=404, detail="주문을 찾을 수 없거나 취소에 실패했습니다.")
    return {"status": "success"}

@app.get("/logs")
async def get_logs():
    """최근 로그 20줄"""
    try:
        with open("bot.log", "r", encoding="utf-8") as f:
            lines = f.readlines()
            return lines[-20:]
    except:
        return ["로그 파일을 찾을 수 없습니다."]