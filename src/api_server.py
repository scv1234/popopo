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
from src.logging_config import configure_logging
from src.polymarket.honeypot_service import HoneypotService
from pydantic import BaseModel
from web3 import Web3

settings = get_settings()
configure_logging(settings.log_level)
bot = MarketMakerBot(settings)
honeypot_service = HoneypotService(settings)

# USDC (Polygon) 컨트랙트 주소 및 최소 ABI
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"}
]

# --- Pydantic Models ---
class OrderRequest(BaseModel):
    market_id: str
    amount: float
    yes_token_id: str
    no_token_id: str

class MintRequest(BaseModel):
    amount: float  # 프론트엔드에서 입력받을 민팅 금액

# --- Lifecycle Management ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 서버 시작 시 봇 루프 및 스캐너 실행
    bot_task = asyncio.create_task(bot.run())
    scanner_task = asyncio.create_task(run_honeypot_scanner())
    yield
    # 서버 종료 시 정리
    await bot.cleanup()
    bot_task.cancel()
    scanner_task.cancel()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Background Tasks ---
async def run_honeypot_scanner():
    """10분마다 폴리마켓을 스캔하여 DB를 갱신합니다."""
    while True:
        try:
            print("🔍 [Scanner] 주기적 시장 스캔 시작...")
            await honeypot_service.scan()
            print("✅ [Scanner] 시장 스캔 및 DB 갱신 완료.")
        except Exception as e:
            print(f"❌ [Scanner] 스캔 중 오류 발생: {e}")
        await asyncio.sleep(600)

# --- Helper Functions ---
def load_honeypots_from_db():
    try:
        conn = sqlite3.connect('bot_data.db')
        cursor = conn.cursor()
        cursor.execute('SELECT data FROM honeypots')
        rows = cursor.fetchall()
        conn.close()
        return [json.loads(row[0]) for row in rows]
    except Exception as e:
        print(f"❌ DB 조회 실패: {e}")
        return []

# --- API Endpoints ---

@app.get("/honey-pots")
async def get_honey_pots():
    db_markets = load_honeypots_from_db()
    if not db_markets: return []
    results = []
    for m in db_markets:
        y_id = m.get("yes_token_id")
        n_id = m.get("no_token_id")
        mid_yes = m.get("mid_yes", 0.5)
        mid_no = m.get("mid_no", 0.5)

        # 실시간 데이터 반영 로직 (중략 - 기존 코드 유지)
        results.append({
            "market_id": m.get("market_id"),
            "title": m.get("title"),
            "slug": m.get("slug"),
            "mid_yes": mid_yes,
            "mid_no": mid_no,
            "score": m.get("score"),
            "total_depth": m.get("total_depth"),
            "reward": m.get("reward"),
            "min_size": m.get("min_size", 20),
            "spread_cents": m.get("spread_cents", 3),
            "yes_token_id": y_id,
            "no_token_id": n_id
        })
    return results

@app.get("/status")
async def get_status():
    """봇의 작동 상태와 인벤토리 정보 반환"""
    # 봇이 현재 보고 있는 마켓의 정보 추출
    mid_price = 0.5
    if bot.current_market_id:
        yes_book = bot.orderbooks.get(bot.yes_token_id, {})
        best_bid = float(yes_book.get("bids")[0][0] if yes_book.get("bids") else 0.5)
        best_ask = float(yes_book.get("asks")[0][0] if yes_book.get("asks") else 0.5)
        mid_price = (best_bid + best_ask) / 2.0

    return {
        "is_halted": bot.risk_manager.is_halted,
        "is_locked": bot.state_lock.locked(),
        "inventory": {
            "yes": bot.inventory_manager.inventory.yes_position,
            "no": bot.inventory_manager.inventory.no_position,
            "net_shares": bot.inventory_manager.inventory.net_exposure_shares,
            "skew": bot.inventory_manager.inventory.get_skew()
        },
        "market": {
            "market_id": bot.current_market_id, 
            "mid_price": round(mid_price, 4),
            "spread_cents": bot.spread_cents
        }
    }

@app.get("/wallet")
async def get_wallet():
    """RPC를 통해 지갑의 USDC 및 MATIC 잔고 조회 (중복 제거 및 통합)"""
    try:
        w3 = Web3(Web3.HTTPProvider(settings.rpc_url))
        address = Web3.to_checksum_address(settings.public_address)
        
        # MATIC 잔고
        native_balance = w3.eth.get_balance(address)
        matic_balance = w3.from_wei(native_balance, 'ether')
        
        # USDC 잔고
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
        return {"usdc_balance": 0.0, "matic_balance": 0.0, "error": str(e)}

# --- [추가] 수동 민팅 엔드포인트 ---
@app.post("/mint")
async def manual_mint(req: MintRequest):
    """프론트엔드에서 설정한 금액만큼 USDC를 Yes/No 세트로 분할(Mint)합니다."""
    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="금액은 0보다 커야 합니다.")
    
    # 봇의 수동 민팅 함수 호출 (src/main.py에 구현되어야 함)
    success = await bot.execute_manual_mint(req.amount)
    if not success:
        raise HTTPException(status_code=500, detail="민팅 처리에 실패했습니다. 잔고나 로그를 확인하세요.")
    
    return {"status": "success", "amount": req.amount}

@app.post("/reset-bot")
async def reset_bot():
    bot.risk_manager.reset_halt()
    return {"status": "success"}

@app.get("/open-orders")
async def get_open_orders():
    # 주문 그룹화 로직 (기존 코드 유지)
    grouped = {}
    for order_id, details in bot.open_orders.items():
        mid = details.get("market", "Unknown")
        outcome = details.get("outcome", "YES")
        if mid not in grouped: grouped[mid] = {"YES": [], "NO": []}
        grouped[mid][outcome].append({
            "order_id": order_id, "side": details.get("side"),
            "price": float(details.get("price")), "size": float(details.get("size"))
        })
    return grouped

@app.get("/logs")
async def get_logs():
    try:
        with open("bot.log", "r", encoding="utf-8") as f:
            return f.readlines()[-20:]
    except: return ["로그 파일을 찾을 수 없습니다."]
