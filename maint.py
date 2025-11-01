import os
import logging
import httpx
import uuid
import re
from datetime import datetime
from dotenv import load_dotenv
from functools import wraps
import io
import qrcode
from PIL import Image
from thefuzz import process  # <<< PENTING: Pastikan ini di-install (pip install thefuzz)

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ConversationHandler,
    Defaults,
    PicklePersistence
)

# --- Import Helper Database Kita ---
from db_helpers import (
    register_user,
    get_user,
    update_credits,
    set_permission,
    get_users_paginated
)

# --- 1. Konfigurasi Logging dan .env ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
try:
    ADMIN_ID = int(os.getenv("ADMIN_USER_ID"))
except (ValueError, TypeError):
    logger.error("Error: ADMIN_USER_ID tidak valid atau tidak diatur di .env")
    exit()

# --- Konstanta API (CEK AKUN FORE) ---
APP_VERSION_FORE = '4.4.6'
SECRET_KEY_FORE = '0kFe6Oc3R1eEa2CpO2FeFdzElp'
PUSH_TOKEN_FORE = 'fGrAkMySNEQhq6MwdM1tCN:APA91bFf_R3hwVWd1HPU-CR17o4BV88zydGzs7FRS9RvBSZrOf7ghL194e3sWGV2koSLS5icz-FxHKUKjmL4neCiGnagTsV4cHfRJJsw2wOcM6fxZihfQZ4'
USER_AGENT_FORE = f'Fore Coffee/{APP_VERSION_FORE} (coffee.fore.fore; build:1459; iOS 18.5.0) Alamofire/4.9.1'

# --- Konstanta API BARU (CEK ORDER & AUTO ORDER) ---
APP_VERSION_ORDER = '4.8.0'
SECRET_KEY_ORDER = '0kFe6Oc3R1eEa2CpO2FeFdzElp'
PUSH_TOKEN_ORDER = 'eR0EtNreq07htx3o06Hwwv:APA91bHqmhJLoFT0tAWBVGW0klBY-O3YmjHsGrQjFPlh4EvewiMzm8gBR422Ob6O9aMjH5n3cIXcF6-BGShYC6C7KC0Ymrxrkkp-bGe6fXsGNfsEZwjOuPk'
USER_AGENT_ORDER = f'Fore Coffee/{APP_VERSION_ORDER} (coffee.fore.fore; build:1553; iOS 18.5.0) Alamofire/4.9.1'
API_URL_STORE_SEARCH = "https://api.fore.coffee/store/all"
# (API Produk sekarang dinamis, cek api_get_all_products)

# Konstanta Umum
PLATFORM = 'ios'
OS_VERSION = '18.5'
DEVICE_MODEL = 'iPhone 12'
COUNTRY_ID = '1'
LANGUAGE = 'id'
DEFAULT_TIMEOUT_SECONDS = 600

# --- 2. Decorator untuk Izin & Kredit ---
def admin_only(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id == ADMIN_ID: return await func(update, context, *args, **kwargs)
        user_data = get_user(user_id)
        if user_data and user_data['is_admin'] == 1: return await func(update, context, *args, **kwargs)
        if update.callback_query: await update.callback_query.answer("Maaf, Anda tidak punya izin admin.", show_alert=True)
        else: await update.message.reply_text("Maaf, Anda tidak punya izin admin untuk tombol ini.")
        return
    return wrapped

def check_access(permission_required: str, credit_cost: int = 0):
    def decorator(func):
        @wraps(func)
        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            user_id = update.effective_user.id
            if user_id == ADMIN_ID: return await func(update, context, *args, **kwargs)
            user_data = get_user(user_id)
            if not user_data:
                reply_func = update.message.reply_text if update.message else context.bot.send_message
                chat_id = update.effective_chat.id if update.effective_chat else user_id
                await reply_func(chat_id=chat_id, text="Anda belum terdaftar. Silakan ketik /start dulu.")
                return
            if user_data['is_admin'] == 1: return await func(update, context, *args, **kwargs)
            
            # <<< PERBAIKAN: Gunakan akses key ['...'] untuk sqlite3.Row >>>
            if not user_data[permission_required] == 1:
                 reply_func = update.message.reply_text if update.message else context.bot.send_message
                 chat_id = update.effective_chat.id if update.effective_chat else user_id
                 await reply_func(chat_id=chat_id, text="Anda tidak memiliki izin, hubungi admin untuk mendapatkan izin.")
                 return
            
            if credit_cost > 0:
                 current_credits = user_data['credits'] # <<< PERBAIKAN
                 if current_credits < credit_cost:
                    reply_func = update.message.reply_text if update.message else context.bot.send_message
                    chat_id = update.effective_chat.id if update.effective_chat else user_id
                    await reply_func(chat_id=chat_id, text=f"Kredit Anda tidak mencukupi (Sisa: {current_credits}, Butuh: {credit_cost})")
                    return
                 
            return await func(update, context, *args, **kwargs)
        return wrapped
    return decorator
# -----------------------------

# --- 3. Definisi Keyboard & State ---
# State Cek Fore
ASK_FORE_PHONE, ASK_FORE_PIN = range(2)
# State Admin
SELECTING_USER, ASKING_CREDIT_AMOUNT, ASKING_PERMISSION_TYPE = range(2, 5)
# State Cek Order
ASK_ORDER_PHONE, ASK_ORDER_PIN, SELECTING_ORDER, AWAITING_REFRESH = range(5, 9)
# State Auto Order
(
    ASK_LOGIN_PHONE_FOR_AUTO_ORDER, ASK_LOGIN_PIN_FOR_AUTO_ORDER, 
    ASK_STORE_KEYWORD, SHOW_STORE_LIST, CONFIRM_STORE_SELECTION,
    SHOW_PRODUCT_CATEGORIES, SHOW_PRODUCT_LIST, 
    ASK_PRODUCT_SEARCH, SHOW_SEARCH_RESULTS
) = range(9, 18)


PERMISSION_NAMES = ['can_cek_akun', 'can_cek_fore', 'is_admin', 'can_cek_order', 'can_auto_order']
USERS_PER_PAGE = 5

# --- Keyboard Utama ---
user_keyboard = [
    [KeyboardButton("Cek Akun"), KeyboardButton("Cek Akun Fore")],
    [KeyboardButton("Cek Orderan"), KeyboardButton("Auto Order")]
]
user_reply_markup = ReplyKeyboardMarkup(user_keyboard, resize_keyboard=True)
admin_keyboard = [
    [KeyboardButton("Cek Akun"), KeyboardButton("Cek Akun Fore")],
    [KeyboardButton("Cek Orderan"), KeyboardButton("Auto Order")],
    [KeyboardButton("Tambah Kredit"), KeyboardButton("Beri Izin"), KeyboardButton("Cabut Izin")]
]
admin_reply_markup = ReplyKeyboardMarkup(admin_keyboard, resize_keyboard=True)

def is_admin_check(user_id: int) -> bool:
    if user_id == ADMIN_ID: return True
    user_data = get_user(user_id)
    # <<< PERBAIKAN: Gunakan akses key ['...'] untuk sqlite3.Row >>>
    return bool(user_data and user_data['is_admin'] == 1)

# --- Helper Headers ---
def _get_api_headers(access_token: str, refresh_token: str, device_id: str) -> dict:
     return {
         'Host': 'api.fore.coffee', 'language': LANGUAGE, 'User-Agent': USER_AGENT_ORDER,
         'push-token': PUSH_TOKEN_ORDER, 'secret-key': SECRET_KEY_ORDER, 'refresh-token': refresh_token,
         'device-id': device_id, 'country-id': COUNTRY_ID, 'platform': PLATFORM,
         'Connection': 'keep-alive', 'access-token': access_token, 'app-version': APP_VERSION_ORDER,
         'os-version': OS_VERSION, 'Content-Type': 'application/json', 'device-model': DEVICE_MODEL,
         'timezone': '+07:00'
    }

# --- 4. Fungsi Logika API (Cek Akun Fore) ---
# <<< LOGIKA POIN DIKEMBALIKAN KE ASLI (HISTORY) >>>
async def process_fore_check(phone_root: str, pin: str) -> dict:
    device_id = str(uuid.uuid4()).upper()
    async with httpx.AsyncClient() as client:
        try:
            headers_step1={'User-Agent':USER_AGENT_FORE,'device-id':device_id,'app-version':APP_VERSION_FORE,'secret-key':SECRET_KEY_FORE,'push-token':PUSH_TOKEN_FORE,'platform':PLATFORM,'os-version':OS_VERSION,'device-model':DEVICE_MODEL,'country-id':COUNTRY_ID,'language':LANGUAGE}
            resp1=await client.get('https://api.fore.coffee/auth/get-token',headers=headers_step1);resp1.raise_for_status();token_data=resp1.json()
            token_payload = token_data.get('payload', {})
            access_token = token_payload.get('access_token')
            refresh_token = token_payload.get('refresh_token')
            if not access_token: raise ValueError("Gagal ambil token")
            
            headers_step2={'User-Agent':USER_AGENT_FORE,'Content-Type':'application/json','access-token':access_token,'secret-key':SECRET_KEY_FORE,'app-version':APP_VERSION_FORE,'device-id':device_id,'push-token':PUSH_TOKEN_FORE,'platform':PLATFORM,'os-version':OS_VERSION,'device-model':DEVICE_MODEL,'country-id':COUNTRY_ID,'language':LANGUAGE}
            payload_step2={"phone":f"+62{phone_root}","pin":pin}
            resp2=await client.post('https://api.fore.coffee/auth/login/pin',headers=headers_step2,json=payload_step2);resp2.raise_for_status();login_data=resp2.json()
            if login_data.get('payload',{}).get('code')!='success': raise ValueError("Gagal login, cek nomor atau PIN")
            
            access_token = login_data.get('payload',{}).get('access_token', access_token)
            refresh_token = login_data.get('payload',{}).get('refresh_token', refresh_token)
            
            headers_step3={'User-Agent':USER_AGENT_FORE,'Content-Type':'application/json','access-token':access_token,'app-version':APP_VERSION_FORE,'platform':PLATFORM,'os-version':OS_VERSION,'device-id':device_id,'secret-key':SECRET_KEY_FORE}
            resp3=await client.get('https://api.fore.coffee/user/profile/detail',headers=headers_step3);resp3.raise_for_status();profile_data=resp3.json()
            profile_payload=profile_data.get('payload',{});reff=profile_payload.get('user_code','-');nama=profile_payload.get('user_name','Tidak Diketahui')
            
            # <<< LOGIKA HITUNG POIN ASLI (DARI HISTORY) >>>
            headers_step4={'access-token':access_token,'device-id':device_id,'app-version':APP_VERSION_FORE,'secret-key':SECRET_KEY_FORE,'platform':PLATFORM,'user-agent':USER_AGENT_FORE}
            resp4=await client.get('https://api.fore.coffee/loyalty/history',headers=headers_step4);resp4.raise_for_status();points_data=resp4.json();history=points_data.get('payload',[]);total_poin=0
            for item in history:
                jenis=item.get('lylhis_type_remarks','');jumlah=item.get('ulylhis_amount',0)
                if jenis in ['Poin Didapat','Bonus Poin','Poin Ditukar']:
                    total_poin+=jumlah
            # <<< AKHIR LOGIKA ASLI >>>

            headers_step5={'Access-Token':access_token,'Device-Id':device_id,'App-Version':APP_VERSION_FORE,'Secret-Key':SECRET_KEY_FORE,'User-Agent':USER_AGENT_FORE,'Platform':PLATFORM}
            resp5=await client.get('https://api.fore.coffee/user/voucher?disc_type=cat_promo&page=1&perpage=100&st_id=0&vc_disc_type=order',headers=headers_step5);resp5.raise_for_status();vouchers_data=resp5.json();voucher_list_raw=vouchers_data.get('payload',{}).get('data',[]);vouchers_list=[]
            for v in voucher_list_raw:
                if v.get('vc_status')=='active':
                    name=v.get('prm_name','Voucher Tidak Dikenal');end_date_str=v.get('prm_end','')
                    try:tgl_obj=datetime.strptime(end_date_str,"%Y-%m-%d %H:%M:%S");tgl_formatted=tgl_obj.strftime("%d-%m-%Y")
                    except(ValueError,TypeError):tgl_formatted="N/A"
                    vouchers_list.append({"name":name,"end":tgl_formatted})
            return{
                "success": True, "nama": nama, "reff": reff, "total_points": total_poin, "vouchers": vouchers_list,
                "access_token": access_token, "refresh_token": refresh_token, "device_id": device_id
            }
        except httpx.HTTPStatusError as e:error_message=f"Gagal API: {e.response.status_code}";logger.error(f"HTTP Error: {e.response.text}");return{"success":False,"message":error_message}
        except ValueError as e:logger.error(f"Value Error: {e}");return{"success":False,"message":str(e)}
        except Exception as e:logger.error(f"General Error: {e}");return{"success":False,"message":f"Terjadi error: {e}"}

# --- 5. Fungsi Logika API (Cek Order, Logout, Store Search, Product List) ---
async def api_login_order(phone_root: str, pin: str) -> dict:
    device_id=str(uuid.uuid4()).upper()
    async with httpx.AsyncClient() as client:
        try:
            headers_step1={'User-Agent':USER_AGENT_ORDER,'device-id':device_id,'app-version':APP_VERSION_ORDER,'secret-key':SECRET_KEY_ORDER,'push-token':PUSH_TOKEN_ORDER,'platform':PLATFORM,'os-version':OS_VERSION,'device-model':DEVICE_MODEL,'country-id':COUNTRY_ID,'language':LANGUAGE}
            resp1=await client.get('https://api.fore.coffee/auth/get-token',headers=headers_step1);resp1.raise_for_status();token_data=resp1.json()
            access_token=token_data.get('payload',{}).get('access_token');refresh_token=token_data.get('payload',{}).get('refresh_token')
            if not access_token:raise ValueError("Gagal ambil token")
            
            headers_step2=headers_step1.copy();headers_step2['Content-Type']='application/json';headers_step2['access-token']=access_token
            payload_step2={"phone":f"+62{phone_root}","pin":pin}
            resp2=await client.post('https://api.fore.coffee/auth/login/pin',headers=headers_step2,json=payload_step2);resp2.raise_for_status();login_data=resp2.json()
            
            if login_data.get('payload',{}).get('code')!='success':
                error_detail = login_data.get('payload',{}).get('message', 'PIN atau nomor salah')
                raise ValueError(f"Gagal login: {error_detail}")
                
            new_access_token=login_data.get('payload',{}).get('access_token',access_token);new_refresh_token=login_data.get('payload',{}).get('refresh_token',refresh_token)
            return{"success":True,"access_token":new_access_token,"refresh_token":new_refresh_token,"device_id":device_id}
        except httpx.HTTPStatusError as e:
            error_msg = f"Gagal API Login ({e.response.status_code})"
            logger.error(f"HTTP Error {e.request.url}: {e.response.status_code} - {e.response.text}")
            try:
                error_data = e.response.json()
                api_msg = error_data.get('message') or error_data.get('payload', {}).get('message')
                if api_msg: error_msg += f": {api_msg}"
            except Exception: pass
            return {"success": False, "message": error_msg}
        except ValueError as e:
            logger.error(f"Value Error during login: {e}")
            return{"success":False,"message":str(e)}
        except Exception as e:logger.exception(f"General Error in api_login_order:");return{"success":False,"message":f"Terjadi error: {e}"}

async def api_get_ongoing_orders(access_token: str, refresh_token: str, device_id: str) -> dict:
    async with httpx.AsyncClient() as client:
        try:
            headers = _get_api_headers(access_token, refresh_token, device_id)
            resp=await client.get('https://api.fore.coffee/order/ongoing/all',headers=headers);resp.raise_for_status()
            return{"success":True,"data":resp.json().get('payload',[])}
        except httpx.HTTPStatusError as e:
             logger.error(f"HTTP Error getting orders {e.request.url}: {e.response.status_code} - {e.response.text}")
             return {"success": False, "message": f"Gagal API Orders: {e.response.status_code}"}
        except Exception as e:logger.exception(f"Gagal api_get_ongoing_orders:");return{"success":False,"message":f"Error ambil order: {str(e)}"}

async def api_get_order_detail(access_token: str, refresh_token: str, device_id: str, uor_id: str) -> dict:
    async with httpx.AsyncClient() as client:
        try:
            headers = _get_api_headers(access_token, refresh_token, device_id)
            url=f'https://api.fore.coffee/order/{uor_id}'
            resp=await client.get(url,headers=headers);resp.raise_for_status()
            return{"success":True,"data":resp.json().get('payload',{})}
        except httpx.HTTPStatusError as e:
             logger.error(f"HTTP Error getting order detail {e.request.url}: {e.response.status_code} - {e.response.text}")
             return {"success": False, "message": f"Gagal API Detail: {e.response.status_code}"}
        except Exception as e:logger.exception(f"Gagal api_get_order_detail:");return{"success":False,"message":f"Error ambil detail: {str(e)}"}

async def api_logout(access_token: str, refresh_token: str, device_id: str):
    if not all([access_token, refresh_token, device_id]):
         logger.warning("Attempted logout with missing credentials.")
         return
    headers = _get_api_headers(access_token, refresh_token, device_id)
    url = 'https://api.fore.coffee/auth/logout'
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=headers, json={})
            if resp.status_code >= 400:
                 logger.warning(f"Gagal melakukan auto-logout (HTTP {resp.status_code}): {resp.text}")
            else:
                 logger.info(f"Berhasil auto-logout dari sesi device {device_id}")
    except Exception as e:
        logger.warning(f"Exception saat auto-logout: {e}")

async def api_search_stores(access_token: str, refresh_token: str, device_id: str, keyword: str) -> dict:
    if not all([access_token, refresh_token, device_id]):
        return {"success": False, "message": "Token/Device ID tidak ditemukan untuk search stores."}
    
    headers = _get_api_headers(access_token, refresh_token, device_id)
    headers.pop('Content-Type', None)
    params = {'keyword': keyword, 'lat': '', 'long': '', 'pd_id': '', 'prm_custom_code': ''}
    
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(API_URL_STORE_SEARCH, headers=headers, params=params)
            resp.raise_for_status()
            payload = resp.json().get('payload', [])
            active_stores = [store for store in payload if store.get('st_status') == 'active']
            logger.info(f"API Search Stores: Ditemukan {len(active_stores)} toko aktif untuk keyword '{keyword}'")
            return {"success": True, "stores": active_stores}
        except httpx.HTTPStatusError as e:
            logger.error(f"Gagal API search_stores (HTTP {e.response.status_code}) for '{keyword}': {e.response.text}")
            return {"success": False, "message": f"Gagal cari toko (API Error {e.response.status_code})"}
        except Exception as e:
            logger.exception(f"Gagal api_search_stores for '{keyword}':")
            return {"success": False, "message": f"Terjadi error saat mencari toko: {str(e)}"}

# <<< FUNGSI API GET PRODUCTS DIPERBAIKI >>>
async def api_get_all_products(access_token: str, refresh_token: str, device_id: str, store_id: int) -> dict:
    """Mengambil semua produk untuk store_id tertentu DENGAN delivery_type=pickup."""
    if not all([access_token, refresh_token, device_id]):
        return {"success": False, "message": "Token/Device ID tidak ditemukan untuk get products."}

    headers = _get_api_headers(access_token, refresh_token, device_id)
    headers.pop('Content-Type', None)
    
    # Endpoint diubah ke /store/{store_id}
    url = f"https://api.fore.coffee/store/{store_id}"
    # Params diubah untuk menyertakan delivery_type=pickup
    params = {'delivery_type': 'pickup', 'lat': '', 'long': ''}

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            
            payload = resp.json().get('payload', [])

            # PENANGANAN JIKA PAYLOAD ADALAH DICT (Info Toko + Produk)
            if isinstance(payload, dict):
                # Kita asumsikan list produk ada di key 'product'
                if 'product' in payload and isinstance(payload.get('product'), list):
                    payload = payload['product']
                else:
                    logger.error(f"API /store/{store_id} mengembalikan payload dict, tapi tidak ditemukan list 'product': {payload.keys()}")
                    return {"success": False, "message": "Format data produk tidak dikenal."}
            
            elif not isinstance(payload, list):
                logger.error(f"API /store/{store_id} mengembalikan tipe payload aneh: {type(payload)}")
                return {"success": False, "message": "Format data API tidak dikenal."}

            # Filter produk yang aktif (pd_status dan stpd_status)
            active_products = [prod for prod in payload if prod.get('pd_status') == 'active' and prod.get('stpd_status') == 'active']
            
            if not active_products and len(payload) > 0:
                 logger.warning(f"Store {store_id}: Dapat {len(payload)} produk, tapi 0 yang aktif (pd_status atau stpd_status tidak 'active').")
            elif not active_products and len(payload) == 0:
                 logger.warning(f"Store {store_id}: API mengembalikan 0 produk di payload.")

            logger.info(f"API Get Products: Ditemukan {len(active_products)} produk aktif untuk store {store_id}")
            return {"success": True, "products": active_products}
        
        except httpx.HTTPStatusError as e:
            logger.error(f"Gagal API get_all_products (HTTP {e.response.status_code}) for store {store_id}: {e.response.text}")
            try:
                error_payload = e.response.json().get('payload', {})
                api_error = error_payload.get('errors', [{}])[0].get('text', 'Error tidak diketahui')
                return {"success": False, "message": f"Gagal ambil produk (API: {api_error})"}
            except Exception:
                return {"success": False, "message": f"Gagal ambil produk (API Error {e.response.status_code})"}
        except Exception as e:
            logger.exception(f"Gagal api_get_all_products for store {store_id}:")
            return {"success": False, "message": f"Terjadi error saat mengambil produk: {str(e)}"}
# <<< AKHIR PERBAIKAN FUNGSI API >>>


# --- 6. Perintah Bot (Handlers Async) ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    try: register_user(user.id, user.first_name)
    except Exception as e: logger.error(f"Gagal mendaftarkan user {user.id}: {e}")
    markup = admin_reply_markup if is_admin_check(user.id) else user_reply_markup
    await update.message.reply_text(f"Selamat datang, {user.first_name}!", reply_markup=markup)

async def check_credits_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id; user_name = update.effective_user.first_name
    user_data = get_user(user_id)
    if not user_data and user_id == ADMIN_ID:
        try: register_user(user_id, user_name); user_data = get_user(user_id)
        except Exception as e: logger.error(f"Gagal mendaftarkan admin {user_id}: {e}"); await update.message.reply_text("Error DB."); return
    elif not user_data: await update.message.reply_text("Anda belum terdaftar. /start dulu."); return
    # <<< PERBAIKAN: Gunakan akses key ['...'] >>>
    sisa_kredit_display = "∞ (Admin)" if is_admin_check(user_id) else user_data['credits']
    await update.message.reply_text(f"👤 **Nama:** {user_data['user_name']}\n🆔 **User ID:** `{user_id}`\n💳 **Sisa kredit:** {sisa_kredit_display}", parse_mode=ParseMode.MARKDOWN)

@check_access(permission_required="can_cek_akun", credit_cost=0)
async def cek_akun(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_data = get_user(user_id)
    if not user_data: await update.message.reply_text("Data tidak ditemukan. /start dulu."); return
    # <<< PERBAIKAN: Gunakan akses key ['...'] >>>
    sisa_kredit_display = "∞ (Admin)" if is_admin_check(user_id) else user_data['credits']
    markup = admin_reply_markup if is_admin_check(user_id) else user_reply_markup
    await update.message.reply_text(f"👤 **Nama:** {user_data['user_name']}\n🆔 **User ID:** `{user_id}`\n💳 **Sisa kredit:** {sisa_kredit_display}", parse_mode=ParseMode.MARKDOWN, reply_markup=markup)

# --- 7. Alur Percakapan (User) "Cek Akun Fore" ---
@check_access(permission_required="can_cek_fore", credit_cost=1)
async def start_fore_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not is_admin_check(user_id):
        user_data = get_user(user_id)
        # <<< PERBAIKAN: Gunakan akses key ['...'] >>>
        if user_data['credits'] < 1:
            await update.message.reply_text(f"Kredit Anda tidak mencukupi (Sisa: {user_data['credits']}, Butuh: 1)", reply_markup=user_reply_markup)
            return ConversationHandler.END
    await update.message.reply_text("Izin OK. Masukkan Nomor HP (tanpa +62 atau 0).\n\n/cancel batal.", reply_markup=ReplyKeyboardRemove()); return ASK_FORE_PHONE

async def receive_fore_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone_input = update.message.text
    phone_cleaned = re.sub(r"[^0-9]", "", phone_input)
    if not phone_cleaned or len(phone_cleaned) < 9 or len(phone_cleaned) > 13:
        await update.message.reply_text("Format nomor HP tidak valid (9-13 digit angka). Coba lagi:")
        return ASK_FORE_PHONE
    if phone_cleaned.startswith("62"): phone_root = phone_cleaned[2:]
    elif phone_cleaned.startswith("0"): phone_root = phone_cleaned[1:]
    else: phone_root = phone_cleaned
    context.user_data["phone_root"] = phone_root
    await update.message.reply_text("HP OK. Masukkan 6 digit PIN:"); return ASK_FORE_PIN

async def receive_fore_pin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    pin_input = update.message.text; phone = context.user_data.get("phone_root"); user_id = update.effective_user.id
    if not (pin_input.isdigit() and len(pin_input) == 6):
        await update.message.reply_text("PIN salah format. Harap masukkan 6 digit angka. Coba lagi:")
        return ASK_FORE_PIN
    
    await update.message.reply_text("PIN OK. Memproses Akun Fore...")
    data = await process_fore_check(phone, pin_input)
    markup = admin_reply_markup if is_admin_check(user_id) else user_reply_markup
    
    if data.get("success"):
        try:
            if not is_admin_check(user_id): update_credits(user_id, -1); logger.info(f"1 kredit dipotong (Cek Akun Fore)")
        except Exception as e: logger.error(f"Gagal potong kredit {user_id}: {e}")
        
        nama=data.get("nama","N/A"); reff=data.get("reff","N/A"); total_poin=data.get("total_points",0); voucher_list=data.get("vouchers",[])
        voucher_display = "Tidak ada voucher." if not voucher_list else "\n\n".join([f"  • *{v['name']}*\n    (Exp: `{v['end']}`)" for v in voucher_list])
        
        hasil_teks = (f"☕️ **Hasil Cek Akun Fore** ☕️\n\n👤 Nama: `{nama}`\n🎟️ Reff: `{reff}`\n✨ Poin (dari History): `{total_poin}`\n\n---\n🏷️ **Voucher**\n---\n{voucher_display}")
        
        await update.message.reply_text(hasil_teks, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
        at = data.get("access_token"); rt = data.get("refresh_token"); did = data.get("device_id")
        if at and rt and did:
            await api_logout(at, rt, did)
    else:
        await update.message.reply_text(f"Gagal: {data.get('message', 'Error')}", reply_markup=markup)
    
    context.user_data.clear(); return ConversationHandler.END

async def cancel_fore_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id; markup = admin_reply_markup if is_admin_check(user_id) else user_reply_markup
    await update.message.reply_text("Cek Akun Fore dibatalkan.", reply_markup=markup)
    context.user_data.clear(); return ConversationHandler.END

# --- 8. Alur Percakapan (User) "Cek Orderan" ---
@check_access(permission_required="can_cek_order", credit_cost=5)
async def start_order_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not is_admin_check(user_id):
        user_data = get_user(user_id)
        # <<< PERBAIKAN: Gunakan akses key ['...'] >>>
        if user_data['credits'] < 5:
            await update.message.reply_text(f"Kredit Anda tidak mencukupi (Sisa: {user_data['credits']}, Butuh: 5)", reply_markup=user_reply_markup)
            return ConversationHandler.END
            
    await update.message.reply_text("Izin OK. Masukkan Nomor HP (tanpa +62 atau 0).\n\n/cancel batal.", reply_markup=ReplyKeyboardRemove()); return ASK_ORDER_PHONE

async def receive_order_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone_input = update.message.text
    phone_cleaned = re.sub(r"[^0-9]", "", phone_input)
    if not phone_cleaned or len(phone_cleaned) < 9 or len(phone_cleaned) > 13:
        await update.message.reply_text("Format nomor HP tidak valid (9-13 digit angka). Coba lagi:")
        return ASK_ORDER_PHONE
    if phone_cleaned.startswith("62"): phone_root = phone_cleaned[2:]
    elif phone_cleaned.startswith("0"): phone_root = phone_cleaned[1:]
    else: phone_root = phone_cleaned
    context.user_data["phone_root"] = phone_root
    await update.message.reply_text("HP OK. Masukkan PIN:"); return ASK_ORDER_PIN

async def receive_order_pin_and_get_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    pin_input = update.message.text; phone = context.user_data.get("phone_root"); user_id = update.effective_user.id
    markup = admin_reply_markup if is_admin_check(user_id) else user_reply_markup
    if not (pin_input.isdigit() and len(pin_input) == 6):
        await update.message.reply_text("PIN salah format. Harap masukkan 6 digit angka. Coba lagi:")
        return ASK_ORDER_PIN

    await update.message.reply_text("PIN OK. Login & cari orderan...")
    login_result = await api_login_order(phone, pin_input)
    if not login_result.get("success"): await update.message.reply_text(f"Login Gagal: {login_result.get('message')}", reply_markup=markup); context.user_data.clear(); return ConversationHandler.END
    
    context.user_data['access_token']=login_result['access_token']; context.user_data['refresh_token']=login_result['refresh_token']; context.user_data['device_id']=login_result['device_id']
    orders_result = await api_get_ongoing_orders(login_result['access_token'], login_result['refresh_token'], login_result['device_id'])
    
    if not orders_result.get("success"):
        await update.message.reply_text(f"Gagal ambil order: {orders_result.get('message')}", reply_markup=markup)
        await api_logout(login_result['access_token'], login_result['refresh_token'], login_result['device_id'])
        context.user_data.clear(); return ConversationHandler.END
    
    ongoing_orders = orders_result.get("data", [])
    if not ongoing_orders:
        await update.message.reply_text("Tidak ada orderan berjalan.", reply_markup=markup)
        await api_logout(login_result['access_token'], login_result['refresh_token'], login_result['device_id'])
        context.user_data.clear(); return ConversationHandler.END

    keyboard = []
    if len(ongoing_orders) > 1:
        summary_lines = ["🔍 **Ditemukan Beberapa Orderan** 🔍"]
        for order in ongoing_orders:
            order_id = order.get('uor_id', 'N/A')
            queue_num = order.get('uor_queue', 'N/A')
            store_name = order.get('store', {}).get('sto_name', 'N/A')
            summary_lines.append(f"\nID Orderan: `{order_id}`\nNomor Antrian: `{queue_num}`\nOutlet: `{store_name}`")
            keyboard.append([InlineKeyboardButton(f"{store_name} (Antrian {queue_num})", callback_data=f"order_select_{order_id}")])
        await update.message.reply_text("\n".join(summary_lines), parse_mode=ParseMode.MARKDOWN)
    else:
         order = ongoing_orders[0]
         order_id = order.get('uor_id', 'N/A')
         queue_num = order.get('uor_queue', 'N/A')
         store_name = order.get('store', {}).get('sto_name', 'Outlet Tidak Dikenal')
         keyboard.append([InlineKeyboardButton(f"{store_name} (Antrian {queue_num})", callback_data=f"order_select_{order_id}")])

    
    keyboard.append([InlineKeyboardButton("Batalkan", callback_data="order_cancel")])
    await update.message.reply_text("Pilih orderan di bawah untuk detail & QR Code:", reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECTING_ORDER

async def _send_formatted_order_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict, user_id: int):
    try:
        status_code = data.get('uor_status')
        if status_code == 'in_process': status = "Dalam Pembuatan 👨‍🍳"
        elif status_code == 'ready_for_pickup': status = "Ready For PickUp 🥤"
        else: status = status_code.replace('_', ' ').title() if status_code else "Status Tidak Diketahui"
        antrian = data.get('uor_queue', 'N/A'); receipt_url = data.get('url_webview_e_receipt', 'N/A')
        qr_hash = data.get('uorsh_hash'); nama_user = data.get('user_name', 'N/A')
        nama_outlet = data.get('st_name', 'N/A')
        kode_outlet = data.get('address', {}).get('st_code', 'N/A') if isinstance(data.get('address'), dict) else 'N/A'
        pesan_dict = data.get('estimated_time_seconds', {})
        pesan_custom = pesan_dict.get('title_message', "Tidak ada pesan") if isinstance(pesan_dict, dict) else "Tidak ada pesan"
        produk_list = data.get('product', []); orderan_lines = []
        if not produk_list: orderan_lines.append("• Gagal memuat daftar produk")
        else:
            for prod in produk_list:
                qty = prod.get('uorpd_qty', 1); nama_prod = prod.get('uorpd_name', 'Produk tidak diketahui')
                orderan_lines.append(f"• {qty}x {nama_prod}")
        daftar_orderan = "\n".join(orderan_lines)
        hasil_teks_list = [
            "✅ **Detail Order** ✅\n", f"Nama: `{nama_user}`", f"Outlet: `{nama_outlet} ({kode_outlet})`",
            f"No Antrian: **{antrian}**", f"Status: **{status}**\n", "--- **Orderan** ---",
            daftar_orderan, "\n--- **Pesan** ---", f"`{pesan_custom}`\n",
        ]
        if receipt_url and receipt_url != 'N/A':
             hasil_teks_list.append(f"E-Receipt: [Klik di sini]({receipt_url})")
        hasil_teks = "\n".join(hasil_teks_list)
        target_chat_id = update.effective_chat.id
        if not qr_hash:
            await context.bot.send_message(chat_id=target_chat_id, text=hasil_teks, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        else:
            try:
                qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=4)
                qr.add_data(qr_hash); qr.make(fit=True)
                img = qr.make_image(fill_color="black", back_color="white").convert('RGB')
                logo_path = 'logo.png'
                if os.path.exists(logo_path):
                    try:
                        logo = Image.open(logo_path)
                        qr_width, qr_height = img.size; logo_max_size = qr_height // 4
                        logo.thumbnail((logo_max_size, logo_max_size), Image.Resampling.LANCZOS)
                        pos = ((qr_width - logo.width) // 2, (qr_height - logo.height) // 2)
                        if logo.mode == 'RGBA':
                             mask = logo.split()[3]
                             img.paste(logo, pos, mask=mask)
                        else: img.paste(logo, pos)
                    except Exception as logo_err: logger.warning(f"Gagal menambahkan logo ke QR: {logo_err}. Mengirim QR standar.")
                else: logger.warning("logo.png tidak ditemukan. Mengirim QR standar.")
                bio = io.BytesIO(); img.save(bio, 'PNG'); bio.seek(0)
                await context.bot.send_photo(chat_id=target_chat_id, photo=bio, caption=hasil_teks, parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                logger.error(f"Gagal generate/send QR code: {e}. Mengirim teks saja.")
                await context.bot.send_message(chat_id=target_chat_id, text=f"{hasil_teks}\n\n(Gagal generate QR Code)", parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        refresh_keyboard = [[
            InlineKeyboardButton("🔄 Refresh", callback_data="order_action_refresh"),
            InlineKeyboardButton("✅ Selesai", callback_data="order_action_finish")
        ]]
        refresh_markup = InlineKeyboardMarkup(refresh_keyboard)
        await context.bot.send_message(chat_id=target_chat_id, text="Refresh status atau selesaikan sesi?", reply_markup=refresh_markup)
    except Exception as e:
        logger.error(f"Gagal memformat/mengirim detail order: {e}")
        await context.bot.send_message(chat_id=user_id, text=f"Terjadi error saat menampilkan data: {e}")

async def select_order_and_show_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    query_data = query.data; user_id = update.effective_user.id
    markup = admin_reply_markup if is_admin_check(user_id) else user_reply_markup
    access_token=context.user_data.get('access_token'); refresh_token=context.user_data.get('refresh_token'); device_id=context.user_data.get('device_id')
    
    if query_data == "order_cancel":
        await query.edit_message_text("Cek order dibatalkan.")
        if access_token and refresh_token and device_id: await api_logout(access_token, refresh_token, device_id)
        await context.bot.send_message(chat_id=user_id, text="Pilih aksi.", reply_markup=markup); context.user_data.clear(); return ConversationHandler.END
    
    try: uor_id = query_data.split("_")[-1]
    except Exception:
        await query.edit_message_text("Data salah. Dibatalkan.", reply_markup=markup)
        if access_token and refresh_token and device_id: await api_logout(access_token, refresh_token, device_id)
        context.user_data.clear(); return ConversationHandler.END

    if not all([access_token, refresh_token, device_id]):
        await query.edit_message_text("Sesi habis. Ulangi.", reply_markup=markup); context.user_data.clear(); return ConversationHandler.END
    
    await query.edit_message_text(f"Ambil detail order {uor_id}...")
    context.user_data['uor_id'] = uor_id
    detail_result = await api_get_order_detail(access_token, refresh_token, device_id, uor_id)
    
    if not detail_result.get("success"):
        await context.bot.send_message(chat_id=user_id, text=f"Gagal ambil detail: {detail_result.get('message')}", reply_markup=markup)
        if access_token and refresh_token and device_id: await api_logout(access_token, refresh_token, device_id)
        context.user_data.clear(); return ConversationHandler.END
    
    data = detail_result.get("data", {})
    if not data:
        await context.bot.send_message(chat_id=user_id, text="Gagal dapat detail.", reply_markup=markup)
        if access_token and refresh_token and device_id: await api_logout(access_token, refresh_token, device_id)
        context.user_data.clear(); return ConversationHandler.END

    cost = 5; credit_deducted_key = f'credit_deducted_for_{uor_id}'
    if not is_admin_check(user_id) and not context.user_data.get(credit_deducted_key, False):
        user_data_before = get_user(user_id)
        # <<< PERBAIKAN: Gunakan akses key ['...'] >>>
        if user_data_before and user_data_before['credits'] >= cost:
            try:
                update_credits(user_id, -cost); context.user_data[credit_deducted_key] = True
                logger.info(f"{cost} kredit dipotong (Cek Order {uor_id})")
            except Exception as e:
                logger.error(f"Gagal potong kredit {user_id}: {e}"); await context.bot.send_message(chat_id=user_id, text="Error potong kredit.", reply_markup=markup)
                if access_token and refresh_token and device_id: await api_logout(access_token, refresh_token, device_id)
                context.user_data.clear(); return ConversationHandler.END
        else:
             # <<< PERBAIKAN: Gunakan akses key ['...'] >>>
             await context.bot.send_message(chat_id=user_id, text=f"Kredit Anda ({user_data_before['credits']}) tidak mencukupi untuk melihat detail (butuh {cost}).", reply_markup=markup)
             if access_token and refresh_token and device_id: await api_logout(access_token, refresh_token, device_id)
             context.user_data.clear(); return ConversationHandler.END

    await _send_formatted_order_detail(update, context, data, user_id)
    return AWAITING_REFRESH

async def handle_refresh_or_finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); query_data = query.data
    user_id = update.effective_user.id
    access_token=context.user_data.get('access_token'); refresh_token=context.user_data.get('refresh_token')
    device_id=context.user_data.get('device_id'); uor_id=context.user_data.get('uor_id')
    markup = admin_reply_markup if is_admin_check(user_id) else user_reply_markup

    if not all([access_token, refresh_token, device_id, uor_id]):
        await query.edit_message_text("Sesi Anda telah berakhir. Silakan ulangi.")
        context.user_data.clear(); return ConversationHandler.END

    if query_data == "order_action_finish":
        await query.edit_message_text("✅ Selesai. Menutup sesi...")
        await api_logout(access_token, refresh_token, device_id)
        await context.bot.send_message(chat_id=user_id, text="Sesi ditutup. Pilih aksi.", reply_markup=markup)
        context.user_data.clear(); return ConversationHandler.END

    if query_data == "order_action_refresh":
        try: await query.delete_message()
        except Exception: pass
        
        await context.bot.send_message(chat_id=user_id, text="🔄 Meresfresh status order...")
        
        detail_result = await api_get_order_detail(access_token, refresh_token, device_id, uor_id)
        
        if not detail_result.get("success") or not detail_result.get("data"):
            await context.bot.send_message(chat_id=user_id, text=f"Gagal refresh data: {detail_result.get('message', 'Data kosong')}. Sesi ditutup.", reply_markup=markup)
            await api_logout(access_token, refresh_token, device_id)
            context.user_data.clear(); return ConversationHandler.END
        
        data = detail_result.get("data", {})
        await _send_formatted_order_detail(update, context, data, user_id)
        return AWAITING_REFRESH
    
    return AWAITING_REFRESH

async def cancel_order_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id; markup = admin_reply_markup if is_admin_check(user_id) else user_reply_markup
    access_token=context.user_data.get('access_token'); refresh_token=context.user_data.get('refresh_token'); device_id=context.user_data.get('device_id')
    if access_token and refresh_token and device_id:
        await api_logout(access_token, refresh_token, device_id)
    await update.message.reply_text("Cek order dibatalkan.", reply_markup=markup)
    context.user_data.clear(); return ConversationHandler.END

# --- 9. Alur Percakapan Admin (Pilih User dari Daftar) ---
def build_user_list_keyboard(users: list, total_pages: int, current_page: int, action: str) -> InlineKeyboardMarkup:
    keyboard = []
    for user in users: keyboard.append([InlineKeyboardButton(f"{user['user_name']} ({user['user_id']})", callback_data=f"admin_{action}_select_{user['user_id']}")])
    nav_row = []
    if current_page > 1: nav_row.append(InlineKeyboardButton("◀ Prev", callback_data=f"admin_{action}_page_{current_page - 1}"))
    nav_row.append(InlineKeyboardButton(f"{current_page}/{total_pages}", callback_data="admin_nop"))
    if current_page < total_pages: nav_row.append(InlineKeyboardButton("Next ▶", callback_data=f"admin_{action}_page_{current_page + 1}"))
    if nav_row: keyboard.append(nav_row)
    keyboard.append([InlineKeyboardButton("Batalkan Aksi", callback_data="admin_cancel")])
    return InlineKeyboardMarkup(keyboard)
@admin_only
async def start_admin_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    action_text = update.message.text
    if action_text == "Tambah Kredit": action = "credit"; prompt = "Pilih user:"
    elif action_text == "Beri Izin": action = "grant"; prompt = "Pilih user:"
    elif action_text == "Cabut Izin": action = "revoke"; prompt = "Pilih user:"
    else: return ConversationHandler.END
    context.user_data['admin_action'] = action
    users, total_pages = get_users_paginated(page=1, per_page=USERS_PER_PAGE, exclude_admin_id=ADMIN_ID)
    if not users: await update.message.reply_text("Tidak ada user lain.", reply_markup=admin_reply_markup); return ConversationHandler.END
    keyboard = build_user_list_keyboard(users, total_pages, 1, action)
    await update.message.reply_text(f"{prompt}\n(Hal 1/{total_pages})", reply_markup=keyboard)
    return SELECTING_USER
async def admin_user_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); query_data = query.data
    if query_data == "admin_nop": return SELECTING_USER
    if query_data == "admin_cancel": await query.edit_message_text("Aksi dibatalkan."); context.user_data.clear(); return ConversationHandler.END
    try: _, action, command, value = query_data.split("_")
    except ValueError: logger.warning(f"Callback data tidak valid: {query_data}"); return SELECTING_USER
    context.user_data['admin_action'] = action
    if command == "page":
        page = int(value); users, total_pages = get_users_paginated(page=page, per_page=USERS_PER_PAGE, exclude_admin_id=ADMIN_ID)
        keyboard = build_user_list_keyboard(users, total_pages, page, action)
        await query.edit_message_text(f"Pilih user:\n(Hal {page}/{total_pages})", reply_markup=keyboard)
        return SELECTING_USER
    if command == "select":
        target_user_id = int(value); target_user = get_user(target_user_id)
        if not target_user: await query.edit_message_text("User tidak ditemukan.", reply_markup=None); context.user_data.clear(); return ConversationHandler.END
        context.user_data['target_user_id'] = target_user_id; context.user_data['target_user_name'] = target_user['user_name']
        await query.delete_message()
        if action == "credit":
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Target: {target_user['user_name']}\nJumlah kredit (misal: `100` atau `-10`):", parse_mode=ParseMode.MARKDOWN)
            return ASKING_CREDIT_AMOUNT
        if action in ("grant", "revoke"):
            # <<< PERBAIKAN: Fitur admin dikembalikan, filter 'is_admin' Dihapus >>>
            perm_keyboard = [[InlineKeyboardButton(p.replace('_', ' ').title(), callback_data=f"admin_perm_{p}")] for p in PERMISSION_NAMES]
            perm_keyboard.append([InlineKeyboardButton("Batalkan", callback_data="admin_cancel_perm")])
            verb = "diberikan" if action == "grant" else "dicabut"
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Target: {target_user['user_name']}\nPilih izin yang akan di-{verb}:", reply_markup=InlineKeyboardMarkup(perm_keyboard))
            return ASKING_PERMISSION_TYPE
    return ConversationHandler.END
async def receive_credit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try: amount = int(update.message.text)
    except ValueError: await update.message.reply_text("Jumlah harus angka. Coba lagi:"); return ASKING_CREDIT_AMOUNT
    target_user_id = context.user_data['target_user_id']; target_user_name = context.user_data['target_user_name']
    try:
        update_credits(target_user_id, amount); new_data = get_user(target_user_id)
        # <<< PERBAIKAN: Gunakan akses key ['...'] >>>
        await update.message.reply_text(f"✅ Berhasil!\nUser: {target_user_name}\nKredit sekarang: **{new_data['credits']}**", parse_mode=ParseMode.MARKDOWN, reply_markup=admin_reply_markup)
    except Exception as e: await update.message.reply_text(f"Gagal update DB: {e}", reply_markup=admin_reply_markup)
    context.user_data.clear(); return ConversationHandler.END
async def receive_permission_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); query_data = query.data
    
    if query_data == "admin_cancel_perm":
        await query.edit_message_text("Aksi dibatalkan.")
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Pilih aksi.", reply_markup=admin_reply_markup)
        context.user_data.clear(); return ConversationHandler.END

    parts = query_data.split("_"); permission_name = "_".join(parts[2:])
    
    # <<< PERBAIKAN: Filter 'is_admin' Dihapus, fitur dikembalikan >>>
    if permission_name not in PERMISSION_NAMES:
        await query.edit_message_text("Izin tidak valid. Dibatalkan.", reply_markup=None)
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Pilih aksi.", reply_markup=admin_reply_markup)
        context.user_data.clear(); return ConversationHandler.END
    
    target_user_id = context.user_data['target_user_id']; target_user_name = context.user_data['target_user_name']
    action = context.user_data['admin_action']; action_value = 1 if action == 'grant' else 0
    action_text = "DIBERIKAN" if action == 'grant' else "DICABUT"
    
    try:
        set_permission(target_user_id, permission_name, action_value)
        perm_display_name = permission_name.replace('_', ' ').title()
        await query.edit_message_text(f"✅ Berhasil!\nUser: {target_user_name}\nIzin '{perm_display_name}' **{action_text}**.", parse_mode=ParseMode.MARKDOWN)
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Pilih aksi.", reply_markup=admin_reply_markup)
    except Exception as e:
        await query.edit_message_text(f"Gagal update DB: {e}", reply_markup=None)
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Pilih aksi.", reply_markup=admin_reply_markup)
    
    context.user_data.clear(); return ConversationHandler.END
async def cancel_admin_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer(); await update.callback_query.edit_message_text("Aksi dibatalkan.")
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Menu Admin", reply_markup=admin_reply_markup)
    else: await update.message.reply_text("Aksi admin dibatalkan.", reply_markup=admin_reply_markup)
    context.user_data.clear(); return ConversationHandler.END

# --- 10. Alur Percakapan Auto Order ---

# Helper untuk build keyboard store list
def build_store_keyboard(stores: list, keyword: str) -> tuple[InlineKeyboardMarkup, dict]:
    keyboard = []; store_map = {}
    for store in stores:
        st_id = store.get('st_id'); st_name = store.get('st_name', f'ID: {st_id}')
        if st_id:
            button_text = st_name[:50] + '...' if len(st_name) > 50 else st_name
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"auto_order_select_{st_id}")])
            store_map[str(st_id)] = st_name
    keyboard.append([
        InlineKeyboardButton("🔄 Cari Ulang", callback_data="auto_order_search_again"),
        InlineKeyboardButton("❌ Batal", callback_data="auto_order_cancel")
    ])
    return InlineKeyboardMarkup(keyboard), store_map

# Helper untuk build keyboard kategori produk
def build_category_keyboard(categories: dict) -> InlineKeyboardMarkup:
    keyboard = []
    sorted_cat_names = sorted(categories.keys())
    for cat_name in sorted_cat_names:
        callback_data = f"prod_cat_{cat_name[:40]}"
        keyboard.append([InlineKeyboardButton(cat_name, callback_data=callback_data)])
    
    keyboard.append([
        InlineKeyboardButton("🔍 Cari Produk", callback_data="prod_search"),
        InlineKeyboardButton("◀️ Ganti Toko", callback_data="prod_back_store")
    ])
    return InlineKeyboardMarkup(keyboard)

# Helper untuk build keyboard list produk
def build_product_keyboard(products: list, from_search: bool = False) -> InlineKeyboardMarkup:
    keyboard = []
    sorted_products = sorted(products, key=lambda x: x.get('pd_name', ''))
    for prod in sorted_products:
        pd_id = prod.get('pd_id')
        pd_name = prod.get('pd_name', f'ID {pd_id}')
        if pd_id:
            button_text = pd_name[:50] + '...' if len(pd_name) > 50 else pd_name
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"prod_select_{pd_id}")])

    back_callback = "prod_back_search" if from_search else "prod_back_cat"
    keyboard.append([InlineKeyboardButton("◀️ Kembali", callback_data=back_callback)])
    return InlineKeyboardMarkup(keyboard)


@check_access(permission_required="can_auto_order", credit_cost=0)
async def start_auto_order_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Fitur Auto Order memerlukan login.\n"
        "Masukkan Nomor HP Fore (tanpa +62 atau 0).\n\n"
        "Ketik /cancel untuk batal.",
        reply_markup=ReplyKeyboardRemove()
    )
    return ASK_LOGIN_PHONE_FOR_AUTO_ORDER

async def receive_auto_order_login_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone_input = update.message.text
    phone_cleaned = re.sub(r"[^0-9]", "", phone_input)
    if not phone_cleaned or len(phone_cleaned) < 9 or len(phone_cleaned) > 13:
        await update.message.reply_text("Format nomor HP tidak valid (9-13 digit angka). Coba lagi:")
        return ASK_LOGIN_PHONE_FOR_AUTO_ORDER
    if phone_cleaned.startswith("62"): phone_root = phone_cleaned[2:]
    elif phone_cleaned.startswith("0"): phone_root = phone_cleaned[1:]
    else: phone_root = phone_cleaned
    context.user_data["phone_root"] = phone_root
    await update.message.reply_text("Nomor HP diterima. Masukkan 6 digit PIN:");
    return ASK_LOGIN_PIN_FOR_AUTO_ORDER

async def receive_auto_order_login_pin_and_ask_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    pin_input = update.message.text
    phone = context.user_data.get("phone_root")
    user_id = update.effective_user.id
    markup = admin_reply_markup if is_admin_check(user_id) else user_reply_markup

    if not (pin_input.isdigit() and len(pin_input) == 6):
        await update.message.reply_text("PIN harus 6 digit angka. Coba lagi:")
        return ASK_LOGIN_PIN_FOR_AUTO_ORDER

    await update.message.reply_text("⏳ Mencoba login...")
    login_result = await api_login_order(phone, pin_input)

    if not login_result.get("success"):
        await update.message.reply_text(f"Login Gagal: {login_result.get('message')}\nAuto Order dibatalkan.", reply_markup=markup)
        context.user_data.clear(); return ConversationHandler.END

    context.user_data['auto_order_access_token'] = login_result['access_token']
    context.user_data['auto_order_refresh_token'] = login_result['refresh_token']
    context.user_data['auto_order_device_id'] = login_result['device_id']
    logger.info(f"User {user_id} berhasil login untuk Auto Order.")

    await update.message.reply_text(
        "✅ Login berhasil!\n"
        "Sekarang masukkan kata kunci nama/lokasi toko (contoh: `Sudirman`):",
         parse_mode=ParseMode.MARKDOWN
    )
    return ASK_STORE_KEYWORD

async def receive_store_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyword = update.message.text.strip()
    user_id = update.effective_user.id
    markup = admin_reply_markup if is_admin_check(user_id) else user_reply_markup
    if not keyword:
        await update.message.reply_text("Keyword tidak boleh kosong. Masukkan nama/lokasi toko:")
        return ASK_STORE_KEYWORD
    
    access_token = context.user_data.get('auto_order_access_token')
    refresh_token = context.user_data.get('auto_order_refresh_token')
    device_id = context.user_data.get('auto_order_device_id')
    if not all([access_token, refresh_token, device_id]):
        await update.message.reply_text("Sesi login tidak ditemukan. Aksi dibatalkan.", reply_markup=markup)
        context.user_data.clear(); return ConversationHandler.END
    
    await update.message.reply_text(f"🔍 Mencari toko dengan keyword: `{keyword}`...", parse_mode=ParseMode.MARKDOWN)
    search_result = await api_search_stores(access_token, refresh_token, device_id, keyword)
    if not search_result.get("success"):
        await update.message.reply_text(f"Gagal mencari toko: {search_result.get('message')}\nCoba lagi atau /cancel.", reply_markup=ReplyKeyboardRemove())
        return ASK_STORE_KEYWORD
    
    active_stores = search_result.get("stores", [])
    if not active_stores:
        await update.message.reply_text(f"Tidak ditemukan toko *aktif* dengan keyword `{keyword}`.\nSilakan coba keyword lain atau /cancel.", parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove())
        return ASK_STORE_KEYWORD
    
    context.user_data['found_stores'] = active_stores
    context.user_data['current_keyword'] = keyword
    keyboard, store_map = build_store_keyboard(active_stores, keyword)
    context.user_data['store_map'] = store_map
    await update.message.reply_text(
        f"✅ Ditemukan {len(active_stores)} toko aktif untuk `{keyword}`.\n"
        f"Silakan pilih salah satu:",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN
    )
    return SHOW_STORE_LIST

async def handle_store_selection_or_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    query_data = query.data; user_id = update.effective_user.id
    markup = admin_reply_markup if is_admin_check(user_id) else user_reply_markup

    access_token = context.user_data.get('auto_order_access_token')
    refresh_token = context.user_data.get('auto_order_refresh_token')
    device_id = context.user_data.get('auto_order_device_id')
    found_stores = context.user_data.get('found_stores', [])
    current_keyword = context.user_data.get('current_keyword', '')

    if query_data == "auto_order_cancel":
        await query.edit_message_text("❌ Auto Order dibatalkan.")
        if access_token and refresh_token and device_id:
            await api_logout(access_token, refresh_token, device_id)
        await context.bot.send_message(chat_id=user_id, text="Pilih aksi.", reply_markup=markup)
        context.user_data.clear(); return ConversationHandler.END

    elif query_data == "auto_order_search_again":
        await query.edit_message_text("🔄 Silakan masukkan keyword toko baru:")
        context.user_data.pop('found_stores', None); context.user_data.pop('store_map', None)
        context.user_data.pop('current_keyword', None)
        return ASK_STORE_KEYWORD

    elif query_data.startswith("auto_order_select_"):
        try:
            selected_store_id = int(query_data.split("_")[-1])
            selected_store_data = next((store for store in found_stores if store.get('st_id') == selected_store_id), None)
            
            if not selected_store_data:
                logger.warning(f"Store ID {selected_store_id} dipilih tapi tidak ditemukan di found_stores.")
                await query.edit_message_text("Terjadi kesalahan: Toko tidak ditemukan. Coba cari ulang.")
                keyboard, _ = build_store_keyboard(found_stores, current_keyword)
                await query.message.reply_text(f"Silakan pilih lagi:", reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
                return SHOW_STORE_LIST

            context.user_data['selected_store_data'] = selected_store_data
            store_name = selected_store_data.get('st_name', 'N/A')
            store_code = selected_store_data.get('st_code', 'N/A')
            address = selected_store_data.get('st_address', 'N/A')
            map_link = selected_store_data.get('st_dllink') or selected_store_data.get('st_direction_link')
            phone = selected_store_data.get('st_phone', 'N/A')
            open_hour = selected_store_data.get('st_open', 'N/A')
            close_hour = selected_store_data.get('st_close', 'N/A')
            
            image_url = None
            store_images = selected_store_data.get('store_image', [])
            if store_images and isinstance(store_images, list) and store_images[0].get('sti_img'):
                image_url = store_images[0]['sti_img']

            confirmation_text_list = [
                f"🏪 **Konfirmasi Toko Pilihan** 🏪\n",
                f"**Nama:** {store_name}", f"**Kode:** `{store_code}`", f"**Alamat:** {address}",
            ]
            if map_link: confirmation_text_list.append(f"**Peta:** [Lihat di Peta]({map_link})")
            confirmation_text_list.extend([
                f"**Telp:** `{phone}`", f"**Jam Buka:** {open_hour} - {close_hour}\n",
                f"Apakah ini toko yang benar?"
            ])
            confirmation_text = "\n".join(confirmation_text_list)

            confirm_keyboard = [
                [InlineKeyboardButton("✅ Ya, Konfirmasi Toko Ini", callback_data="auto_order_confirm")],
                [InlineKeyboardButton("❌ Tidak, Pilih Ulang", callback_data="auto_order_reselect")]
            ]
            
            await query.delete_message()
            
            if image_url:
                await context.bot.send_photo(
                    chat_id=user_id, photo=image_url, caption=confirmation_text,
                    reply_markup=InlineKeyboardMarkup(confirm_keyboard), parse_mode=ParseMode.MARKDOWN
                )
            else:
                await context.bot.send_message(
                    chat_id=user_id, text=confirmation_text,
                    reply_markup=InlineKeyboardMarkup(confirm_keyboard),
                    parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True
                )
            return CONFIRM_STORE_SELECTION

        except (ValueError, IndexError):
            logger.warning(f"Callback data pemilihan toko tidak valid: {query_data}")
            await query.edit_message_text("Pilihan toko tidak valid. Silakan coba lagi.")
            keyboard, _ = build_store_keyboard(found_stores, current_keyword)
            await query.message.reply_text(f"Silakan pilih lagi:", reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
            return SHOW_STORE_LIST
    else:
        logger.warning(f"Callback tidak dikenal di SHOW_STORE_LIST: {query_data}")
        return SHOW_STORE_LIST

async def handle_store_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    query_data = query.data; user_id = update.effective_user.id
    
    found_stores = context.user_data.get('found_stores', [])
    current_keyword = context.user_data.get('current_keyword', '')
    selected_store_data = context.user_data.get('selected_store_data')

    if query_data == "auto_order_reselect":
        if not found_stores:
            await query.edit_message_text("Data toko sebelumnya tidak ditemukan. Silakan /cancel dan coba lagi.")
            return CONFIRM_STORE_SELECTION
        
        keyboard, _ = build_store_keyboard(found_stores, current_keyword)
        try: await query.delete_message()
        except Exception: pass
        
        await context.bot.send_message(
             chat_id=user_id,
             text=f"Silakan pilih lagi toko untuk keyword `{current_keyword}`:",
             reply_markup=keyboard,
             parse_mode=ParseMode.MARKDOWN
        )
        context.user_data.pop('selected_store_data', None)
        return SHOW_STORE_LIST

    elif query_data == "auto_order_confirm":
        if not selected_store_data:
            await query.edit_message_text("Gagal mengonfirmasi, data toko tidak ditemukan. Silakan /cancel.")
            return CONFIRM_STORE_SELECTION
        
        store_id = selected_store_data.get('st_id')
        store_name = selected_store_data.get('st_name', 'N/A')
        
        access_token = context.user_data.get('auto_order_access_token')
        refresh_token = context.user_data.get('auto_order_refresh_token')
        device_id = context.user_data.get('auto_order_device_id')
        
        if not all([access_token, refresh_token, device_id, store_id]):
             await query.edit_message_text("Sesi tidak valid. Silakan /cancel dan mulai lagi.")
             return ConversationHandler.END

        await query.edit_message_text(f"✅ Toko **{store_name}** dikonfirmasi!\n\n⏳ Mengambil daftar produk...", parse_mode=ParseMode.MARKDOWN)
        
        # --- TAHAP 2: Ambil Produk ---
        product_result = await api_get_all_products(access_token, refresh_token, device_id, store_id)
        
        if not product_result.get("success"):
            await query.edit_message_text(f"Gagal mengambil produk: {product_result.get('message')}\nSilakan coba lagi atau /cancel.")
            confirm_keyboard = [
                [InlineKeyboardButton("✅ Ya, Konfirmasi Toko Ini", callback_data="auto_order_confirm")],
                [InlineKeyboardButton("❌ Tidak, Pilih Ulang", callback_data="auto_order_reselect")]
            ]
            await query.message.reply_text("Coba konfirmasi lagi?", reply_markup=InlineKeyboardMarkup(confirm_keyboard))
            return CONFIRM_STORE_SELECTION
            
        all_products = product_result.get("products", [])
        if not all_products:
            await query.edit_message_text(f"Toko **{store_name}** tidak memiliki produk aktif saat ini.\nSilakan pilih toko lain.", parse_mode=ParseMode.MARKDOWN)
            await context.bot.send_message(chat_id=user_id, text="Masukkan keyword toko baru:")
            # Hapus data toko terpilih
            context.user_data.pop('selected_store_data', None)
            context.user_data.pop('found_stores', None)
            return ASK_STORE_KEYWORD
            
        context.user_data['store_products'] = all_products
        categories = {}
        for prod in all_products:
            cat_name = prod.get('cat_name', 'Lain-lain')
            if cat_name not in categories:
                categories[cat_name] = []
            categories[cat_name].append(prod)
            
        context.user_data['product_categories'] = categories
        
        cat_keyboard = build_category_keyboard(categories)
        await query.edit_message_text(
            f"Produk untuk **{store_name}**:\nSilakan pilih kategori:",
            reply_markup=cat_keyboard,
            parse_mode=ParseMode.MARKDOWN
        )
        return SHOW_PRODUCT_CATEGORIES

    else:
        logger.warning(f"Callback tidak dikenal di CONFIRM_STORE_SELECTION: {query_data}")
        return CONFIRM_STORE_SELECTION

async def handle_category_or_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    query_data = query.data; user_id = update.effective_user.id
    
    categories = context.user_data.get('product_categories', {})
    
    if query_data == "prod_search":
        await query.edit_message_text("Silakan masukkan nama produk yang ingin Anda cari:")
        return ASK_PRODUCT_SEARCH
        
    elif query_data == "prod_back_store":
        context.user_data.pop('store_products', None)
        context.user_data.pop('product_categories', None)
        context.user_data.pop('selected_store_data', None)
        context.user_data.pop('found_stores', None)
        context.user_data.pop('store_map', None)
        context.user_data.pop('current_keyword', None)
        await query.edit_message_text("Silakan masukkan keyword toko baru:")
        return ASK_STORE_KEYWORD
        
    elif query_data.startswith("prod_cat_"):
        selected_cat_name_prefix = query_data[len("prod_cat_"):]
        
        full_cat_name = None
        for cat_name in categories.keys():
            # Perbandingan yang lebih aman untuk prefix
            if cat_name.startswith(selected_cat_name_prefix):
                full_cat_name = cat_name
                break
        
        if not full_cat_name or full_cat_name not in categories:
            await query.answer("Kategori tidak ditemukan.", show_alert=True)
            logger.warning(f"Kategori {selected_cat_name_prefix} tidak ditemukan di {categories.keys()}")
            cat_keyboard = build_category_keyboard(categories)
            await query.edit_message_text("Silakan pilih kategori:", reply_markup=cat_keyboard)
            return SHOW_PRODUCT_CATEGORIES
            
        products_in_cat = categories[full_cat_name]
        context.user_data['current_product_list'] = products_in_cat
        
        prod_keyboard = build_product_keyboard(products_in_cat, from_search=False)
        await query.edit_message_text(f"Produk dalam Kategori: **{full_cat_name}**", reply_markup=prod_keyboard)
        context.user_data['previous_state'] = SHOW_PRODUCT_CATEGORIES
        return SHOW_PRODUCT_LIST
        
    else:
        logger.warning(f"Callback tidak dikenal di SHOW_PRODUCT_CATEGORIES: {query_data}")
        return SHOW_PRODUCT_CATEGORIES

async def receive_product_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyword = update.message.text.strip()
    
    if not keyword:
        await update.message.reply_text("Keyword tidak boleh kosong. Masukkan nama produk:")
        return ASK_PRODUCT_SEARCH
        
    all_products = context.user_data.get('store_products', [])
    if not all_products:
        await update.message.reply_text("Data produk tidak ditemukan. Kembali ke menu kategori.")
        categories = context.user_data.get('product_categories', {})
        cat_keyboard = build_category_keyboard(categories)
        await update.message.reply_text("Silakan pilih kategori:", reply_markup=cat_keyboard)
        return SHOW_PRODUCT_CATEGORIES
        
    product_name_map = {prod.get('pd_name'): prod.get('pd_id') for prod in all_products if prod.get('pd_name') and prod.get('pd_id')}
    product_id_map = {prod.get('pd_id'): prod for prod in all_products if prod.get('pd_id')}

    results = process.extractBests(keyword, product_name_map.keys(), score_cutoff=60, limit=10)
    
    if not results:
        await update.message.reply_text(f"Tidak ditemukan produk yang mirip dengan '{keyword}'.\n\nMasukkan keyword lain, atau /cancel untuk kembali ke menu utama.")
        return ASK_PRODUCT_SEARCH
        
    matched_products = []
    for (name, score, *_) in results:
        pd_id = product_name_map.get(name)
        if pd_id and pd_id in product_id_map:
            if product_id_map[pd_id] not in matched_products:
                matched_products.append(product_id_map[pd_id])
            
    if not matched_products:
        await update.message.reply_text(f"Terjadi error saat mengambil detail produk hasil pencarian.\n\nMasukkan keyword lain, atau /cancel.")
        return ASK_PRODUCT_SEARCH

    context.user_data['search_results'] = matched_products
    
    prod_keyboard = build_product_keyboard(matched_products, from_search=True)
    await update.message.reply_text(f"Hasil pencarian untuk: '{keyword}'", reply_markup=prod_keyboard)
    context.user_data['previous_state'] = ASK_PRODUCT_SEARCH
    return SHOW_SEARCH_RESULTS
    
async def handle_product_list_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    query_data = query.data; user_id = update.effective_user.id
    markup = admin_reply_markup if is_admin_check(user_id) else user_reply_markup

    categories = context.user_data.get('product_categories', {})
    store_name = context.user_data.get('selected_store_data', {}).get('st_name', 'Toko')
    
    if query_data == "prod_back_cat":
        cat_keyboard = build_category_keyboard(categories)
        await query.edit_message_text(
            f"Produk untuk **{store_name}**:\nSilakan pilih kategori:",
            reply_markup=cat_keyboard
        )
        context.user_data.pop('current_product_list', None)
        return SHOW_PRODUCT_CATEGORIES
        
    elif query_data == "prod_back_search":
        await query.edit_message_text("Silakan masukkan nama produk yang ingin Anda cari:")
        context.user_data.pop('search_results', None)
        return ASK_PRODUCT_SEARCH
        
    elif query_data.startswith("prod_select_"):
        try:
            selected_pd_id = int(query_data.split("_")[-1])
            all_products = context.user_data.get('store_products', [])
            selected_product = next((prod for prod in all_products if prod.get('pd_id') == selected_pd_id), None)
            
            if not selected_product:
                await query.edit_message_text("Produk tidak ditemukan. Silakan pilih lagi.")
                return context.user_data.get('previous_state', SHOW_PRODUCT_CATEGORIES)

            context.user_data['selected_product'] = selected_product
            pd_name = selected_product.get('pd_name', 'N/A')
            
            logger.info(f"User {user_id} memilih produk: {pd_name} ({selected_pd_id})")
            
            await query.edit_message_text(f"✅ Produk dipilih: **{pd_name}**", parse_mode=ParseMode.MARKDOWN)
            await context.bot.send_message(
                chat_id=user_id,
                text="Tahap 2 (Pilih Produk) selesai. Fitur selanjutnya (Pilih Opsi & Bayar) belum siap.\nSesi Auto Order selesai.",
                reply_markup=markup
            )
            
            # Bersihkan data, TAPI SIMPAN token, toko, & produk
            context.user_data.pop('store_products', None)
            context.user_data.pop('product_categories', None)
            context.user_data.pop('current_product_list', None)
            context.user_data.pop('search_results', None)
            context.user_data.pop('found_stores', None)
            context.user_data.pop('store_map', None)
            context.user_data.pop('current_keyword', None)
            # Hapus juga data login Cek Akun Fore (jika ada)
            context.user_data.pop('phone_root', None)
            
            return ConversationHandler.END

        except (ValueError, IndexError):
            logger.warning(f"Callback data pemilihan produk tidak valid: {query_data}")
            await query.edit_message_text("Pilihan produk tidak valid. Silakan coba lagi.")
            return context.user_data.get('previous_state', SHOW_PRODUCT_CATEGORIES)

    else:
        logger.warning(f"Callback tidak dikenal di SHOW_PRODUCT_LIST/SHOW_SEARCH_RESULTS: {query_data}")
        return context.user_data.get('previous_state', SHOW_PRODUCT_CATEGORIES)

async def cancel_auto_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    markup = admin_reply_markup if is_admin_check(user_id) else user_reply_markup
    access_token = context.user_data.get('auto_order_access_token')
    refresh_token = context.user_data.get('auto_order_refresh_token')
    device_id = context.user_data.get('auto_order_device_id')
    
    if access_token and refresh_token and device_id:
        await api_logout(access_token, refresh_token, device_id)
    
    cancel_message = "❌ Auto Order dibatalkan."
    if update.callback_query:
        await update.callback_query.answer()
        try: await update.callback_query.edit_message_text(cancel_message)
        except Exception: pass
        await context.bot.send_message(chat_id=user_id, text="Pilih aksi.", reply_markup=markup)
    elif update.message:
        await update.message.reply_text(cancel_message, reply_markup=markup)
    else:
        if user_id:
            await context.bot.send_message(chat_id=user_id, text=cancel_message, reply_markup=markup)
            
    context.user_data.clear()
    return ConversationHandler.END

# --- 11. Fungsi Utama (Main) ---
def main() -> None:
    if not TOKEN: logger.critical("Error: TELEGRAM_TOKEN tidak ditemukan"); return
    
    persistence = PicklePersistence(filepath="bot_data.pkl")
    defaults = Defaults(parse_mode=ParseMode.MARKDOWN)
    
    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .persistence(persistence)
        .defaults(defaults)
        .build()
    )

    # --- Conversation Handler untuk Cek Akun Fore ---
    fore_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^Cek Akun Fore$"), start_fore_check)],
        states={
            ASK_FORE_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_fore_phone)],
            ASK_FORE_PIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_fore_pin)],
        },
        fallbacks=[CommandHandler("cancel", cancel_fore_check)],
        per_user=True, per_message=False,
        conversation_timeout=DEFAULT_TIMEOUT_SECONDS
    )

    # --- Conversation Handler untuk Admin ---
    admin_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & (filters.Regex("^Tambah Kredit$") | filters.Regex("^Beri Izin$") | filters.Regex("^Cabut Izin$")), start_admin_flow)],
        states={
            SELECTING_USER: [CallbackQueryHandler(admin_user_list_callback, pattern="^admin_")],
            ASKING_CREDIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_credit_amount)],
            # <<< PERBAIKAN: regex di admin_perm_.* >>>
            ASKING_PERMISSION_TYPE: [CallbackQueryHandler(receive_permission_type, pattern="^admin_perm_.*"), CallbackQueryHandler(cancel_admin_flow, pattern="^admin_cancel_perm$")],
        },
        fallbacks=[CommandHandler("cancel", cancel_admin_flow), CallbackQueryHandler(cancel_admin_flow, pattern="^admin_cancel$")],
        per_user=True, per_message=False,
        conversation_timeout=DEFAULT_TIMEOUT_SECONDS
    )

    # --- Conversation Handler untuk Cek Orderan ---
    order_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^Cek Orderan$"), start_order_check)],
        states={
            ASK_ORDER_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_order_phone)],
            ASK_ORDER_PIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_order_pin_and_get_list)],
            # <<< PERBAIKAN: regex di order_select_.* >>>
            SELECTING_ORDER: [CallbackQueryHandler(select_order_and_show_detail, pattern="^order_select_.*|^order_cancel$")],
            AWAITING_REFRESH: [CallbackQueryHandler(handle_refresh_or_finish, pattern="^order_action_")]
        },
        fallbacks=[CommandHandler("cancel", cancel_order_check)],
        per_user=True, per_message=False,
        conversation_timeout=DEFAULT_TIMEOUT_SECONDS
    )

    # --- Conversation Handler untuk Auto Order --- <<< PATTERN DIPERBAIKI >>>
    auto_order_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^Auto Order$"), start_auto_order_login)],
        states={
            # Tahap Login
            ASK_LOGIN_PHONE_FOR_AUTO_ORDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_auto_order_login_phone)],
            ASK_LOGIN_PIN_FOR_AUTO_ORDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_auto_order_login_pin_and_ask_keyword)],
            # Tahap 1: Pilih Toko
            ASK_STORE_KEYWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_store_keyword)],
            # <<< PERBAIKAN: regex di auto_order_select_.* >>>
            SHOW_STORE_LIST: [CallbackQueryHandler(handle_store_selection_or_action, pattern="^auto_order_(select_.*|search_again|cancel)$")],
            CONFIRM_STORE_SELECTION: [CallbackQueryHandler(handle_store_confirmation, pattern="^auto_order_(confirm|reselect)$")],
            # Tahap 2: Pilih Produk
            # <<< PERBAIKAN: regex di prod_cat_.* >>>
            SHOW_PRODUCT_CATEGORIES: [CallbackQueryHandler(handle_category_or_search, pattern="^prod_(cat_.*|search|back_store)$")],
            ASK_PRODUCT_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_product_search)],
            # <<< PERBAIKAN: regex di prod_select_.* >>>
            SHOW_PRODUCT_LIST: [CallbackQueryHandler(handle_product_list_action, pattern="^prod_(select_.*|back_cat)$")],
            SHOW_SEARCH_RESULTS: [CallbackQueryHandler(handle_product_list_action, pattern="^prod_(select_.*|back_search)$")]
        },
        fallbacks=[CommandHandler("cancel", cancel_auto_order), CallbackQueryHandler(cancel_auto_order, pattern="^auto_order_cancel$")],
        per_user=True, per_message=False,
        conversation_timeout=DEFAULT_TIMEOUT_SECONDS,
    )

    # Daftarkan semua handler
    application.add_handler(fore_conv_handler)
    application.add_handler(admin_conv_handler)
    application.add_handler(order_conv_handler)
    application.add_handler(auto_order_conv_handler)

    # Handler Perintah & Pesan Biasa
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("credit", check_credits_command))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex("^Cek Akun$"), cek_akun))
    
    logger.info("Bot siap dijalankan...")
    application.run_polling()

if __name__ == '__main__':
    main()