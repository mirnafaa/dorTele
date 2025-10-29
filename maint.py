# ... (Impor dan konfigurasi awal tetap sama) ...
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

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode # <<< BARU (Import ParseMode)
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
from db_helpers import (
    register_user, get_user, update_credits, set_permission, get_users_paginated
)

# --- Logging, .env, Konstanta API (tetap sama) ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
try: ADMIN_ID = int(os.getenv("ADMIN_USER_ID"))
except (ValueError, TypeError): logger.error("ADMIN_USER_ID invalid"); exit()
APP_VERSION_FORE = '4.4.6'
SECRET_KEY_FORE = '0kFe6Oc3R1eEa2CpO2FeFdzElp'
PUSH_TOKEN_FORE = 'fGrAkMySNEQhq6MwdM1tCN:APA91bFf_R3hwVWd1HPU-CR17o4BV88zydGzs7FRS9RvBSZrOf7ghL194e3sWGV2koSLS5icz-FxHKUKjmL4neCiGnagTsV4cHfRJJsw2wOcM6fxZihfQZ4'
USER_AGENT_FORE = f'Fore Coffee/{APP_VERSION_FORE} (coffee.fore.fore; build:1459; iOS 18.5.0) Alamofire/4.9.1'
APP_VERSION_ORDER = '4.8.0'
SECRET_KEY_ORDER = '0kFe6Oc3R1eEa2CpO2FeFdzElp'
PUSH_TOKEN_ORDER = 'eR0EtNreq07htx3o06Hwwv:APA91bHqmhJLoFT0tAWBVGW0klBY-O3YmjHsGrQjFPlh4EvewiMzm8gBR422Ob6O9aMjH5n3cIXcF6-BGShYC6C7KC0Ymrxrkkp-bGe6fXsGNfsEZwjOuPk'
USER_AGENT_ORDER = f'Fore Coffee/{APP_VERSION_ORDER} (coffee.fore.fore; build:1553; iOS 18.5.0) Alamofire/4.9.1'
API_URL_STORE_SEARCH = "https://api.fore.coffee/store/all"
PLATFORM = 'ios'; OS_VERSION = '18.5'; DEVICE_MODEL = 'iPhone 12'
COUNTRY_ID = '1'; LANGUAGE = 'id'; DEFAULT_TIMEOUT_SECONDS = 600

# --- Decorators (tetap sama) ---
# ... (admin_only, check_access) ...
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
    """Decorator canggih: Cek Izin dan Kredit."""
    def decorator(func):
        @wraps(func)
        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            user_id = update.effective_user.id
            if user_id == ADMIN_ID: return await func(update, context, *args, **kwargs)
            user_data = get_user(user_id)
            if not user_data:
                reply_func = update.message.reply_text if update.message else context.bot.send_message
                chat_id = update.effective_chat.id
                await reply_func(chat_id=chat_id, text="Anda belum terdaftar. Silakan ketik /start dulu.")
                return
            if user_data['is_admin'] == 1: return await func(update, context, *args, **kwargs)
            if not user_data.get(permission_required, 0) == 1:
                 reply_func = update.message.reply_text if update.message else context.bot.send_message
                 chat_id = update.effective_chat.id
                 await reply_func(chat_id=chat_id, text="Anda tidak memiliki izin, hubungi admin untuk mendapatkan izin.")
                 return
            # Pengecekan kredit dipindahkan ke dalam fungsi handler yang relevan (misal: saat detail order ditampilkan)
            # if user_data['credits'] < credit_cost:
            #      reply_func = update.message.reply_text if update.message else context.bot.send_message
            #      chat_id = update.effective_chat.id
            #      await reply_func(chat_id=chat_id, text=f"Kredit Anda tidak mencukupi (Sisa: {user_data['credits']}, Butuh: {credit_cost})")
            #      return
            return await func(update, context, *args, **kwargs)
        return wrapped
    return decorator


# --- Definisi Keyboard & State --- <<< MODIFIKASI STATE
ASK_FORE_PHONE, ASK_FORE_PIN = range(2)
SELECTING_USER, ASKING_CREDIT_AMOUNT, ASKING_PERMISSION_TYPE = range(2, 5)
ASK_ORDER_PHONE, ASK_ORDER_PIN, SELECTING_ORDER, AWAITING_REFRESH = range(5, 9)
# State Auto Order <<< DIPERBARUI
ASK_LOGIN_PHONE_FOR_AUTO_ORDER, ASK_LOGIN_PIN_FOR_AUTO_ORDER, ASK_STORE_KEYWORD, SHOW_STORE_LIST, CONFIRM_STORE_SELECTION = range(9, 14)

PERMISSION_NAMES = ['can_cek_akun', 'can_cek_fore', 'is_admin', 'can_cek_order', 'can_auto_order']
USERS_PER_PAGE = 5
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

# --- API Functions (process_fore_check, api_login_order, api_get_ongoing_orders, api_get_order_detail, api_logout, api_search_stores tetap sama) ---
# ... (kode fungsi API yang sudah ada sebelumnya) ...
async def process_fore_check(phone_root: str, pin: str) -> dict:
    device_id = str(uuid.uuid4()).upper()
    async with httpx.AsyncClient() as client:
        try:
            headers_step1={'User-Agent':USER_AGENT_FORE,'device-id':device_id,'app-version':APP_VERSION_FORE,'secret-key':SECRET_KEY_FORE,'push-token':PUSH_TOKEN_FORE,'platform':PLATFORM,'os-version':OS_VERSION,'device-model':DEVICE_MODEL,'country-id':COUNTRY_ID,'language':LANGUAGE}
            resp1=await client.get('https://api.fore.coffee/auth/get-token',headers=headers_step1);resp1.raise_for_status();token_data=resp1.json()
            token_payload = token_data.get('payload', {})
            access_token = token_payload.get('access_token')
            refresh_token = token_payload.get('refresh_token')
            if not access_token: raise ValueError("Gagal ambil token awal") # Lebih spesifik
            headers_step2={'User-Agent':USER_AGENT_FORE,'Content-Type':'application/json','access-token':access_token,'secret-key':SECRET_KEY_FORE,'app-version':APP_VERSION_FORE,'device-id':device_id,'push-token':PUSH_TOKEN_FORE,'platform':PLATFORM,'os-version':OS_VERSION,'device-model':DEVICE_MODEL,'country-id':COUNTRY_ID,'language':LANGUAGE}
            payload_step2={"phone":f"+62{phone_root}","pin":pin}
            resp2=await client.post('https://api.fore.coffee/auth/login/pin',headers=headers_step2,json=payload_step2);resp2.raise_for_status();login_data=resp2.json()
            if login_data.get('payload',{}).get('code')!='success': raise ValueError("Gagal login, cek nomor atau PIN")
            # Ambil token baru setelah login
            access_token = login_data.get('payload',{}).get('access_token', access_token)
            refresh_token = login_data.get('payload',{}).get('refresh_token', refresh_token)
            # Dapatkan profile
            headers_step3={'User-Agent':USER_AGENT_FORE,'Content-Type':'application/json','access-token':access_token,'app-version':APP_VERSION_FORE,'platform':PLATFORM,'os-version':OS_VERSION,'device-id':device_id,'secret-key':SECRET_KEY_FORE}
            resp3=await client.get('https://api.fore.coffee/user/profile/detail',headers=headers_step3);resp3.raise_for_status();profile_data=resp3.json()
            profile_payload=profile_data.get('payload',{});reff=profile_payload.get('user_code','-');nama=profile_payload.get('user_name','Tidak Diketahui')
            # Dapatkan Poin
            headers_step4={'access-token':access_token,'device-id':device_id,'app-version':APP_VERSION_FORE,'secret-key':SECRET_KEY_FORE,'platform':PLATFORM,'user-agent':USER_AGENT_FORE}
            resp4=await client.get('https://api.fore.coffee/loyalty/history',headers=headers_step4);resp4.raise_for_status();points_data=resp4.json();history=points_data.get('payload',[]);total_poin=0
            # Kalkulasi poin (contoh sederhana, sesuaikan jika perlu)
            current_points_payload = profile_payload.get('loyalty', {})
            total_poin = current_points_payload.get('ulyl_total_point', 0) if isinstance(current_points_payload, dict) else 0

            # Dapatkan Voucher
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
                "access_token": access_token, "refresh_token": refresh_token, "device_id": device_id # Kembalikan token terbaru
            }
        except httpx.HTTPStatusError as e:error_message=f"Gagal API ({e.response.status_code})";logger.error(f"HTTP Error {e.request.url}: {e.response.status_code} - {e.response.text}");return{"success":False,"message":error_message}
        except ValueError as e:logger.error(f"Value Error: {e}");return{"success":False,"message":str(e)}
        except Exception as e:logger.exception(f"General Error in process_fore_check:");return{"success":False,"message":f"Terjadi error tidak terduga: {e}"}

async def api_login_order(phone_root: str, pin: str) -> dict:
    device_id=str(uuid.uuid4()).upper()
    async with httpx.AsyncClient() as client:
        try:
            # Step 1: Get Initial Token (using ORDER constants)
            headers_step1={
                'User-Agent':USER_AGENT_ORDER,'device-id':device_id,'app-version':APP_VERSION_ORDER,
                'secret-key':SECRET_KEY_ORDER,'push-token':PUSH_TOKEN_ORDER,'platform':PLATFORM,
                'os-version':OS_VERSION,'device-model':DEVICE_MODEL,'country-id':COUNTRY_ID,'language':LANGUAGE
            }
            resp1=await client.get('https://api.fore.coffee/auth/get-token',headers=headers_step1);resp1.raise_for_status();token_data=resp1.json()
            access_token=token_data.get('payload',{}).get('access_token');refresh_token=token_data.get('payload',{}).get('refresh_token')
            if not access_token:raise ValueError("Gagal ambil token awal")

            # Step 2: Login with PIN
            headers_step2=headers_step1.copy();headers_step2['Content-Type']='application/json';headers_step2['access-token']=access_token
            payload_step2={"phone":f"+62{phone_root}","pin":pin}
            resp2=await client.post('https://api.fore.coffee/auth/login/pin',headers=headers_step2,json=payload_step2);resp2.raise_for_status();login_data=resp2.json()

            # Check login success and get new tokens
            if login_data.get('payload',{}).get('code')!='success':
                 # Coba parse pesan error dari API jika ada
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
            except Exception: pass # Abaikan jika response bukan JSON / tidak ada message
            return {"success": False, "message": error_msg}
        except ValueError as e:
            logger.error(f"Value Error during login: {e}")
            return{"success":False,"message":str(e)}
        except Exception as e:
            logger.exception(f"General Error in api_login_order:")
            return{"success":False,"message":f"Terjadi error: {e}"}

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
    # Hapus header yang tidak perlu/berpotensi error untuk GET request sederhana ini
    headers.pop('Content-Type', None)
    headers.pop('push-token', None) # Mungkin tidak perlu untuk search
    headers.pop('sentry-trace', None) # Header debugging, hapus saja
    headers.pop('baggage', None)      # Header debugging, hapus saja
    headers.pop('appsflyer-id', None) # Mungkin tidak perlu
    headers.pop('appsflyer-advertising-id', None) # Mungkin tidak perlu

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

# --- Bot Command Handlers (start, check_credits_command, cek_akun tetap sama) ---
# ... (kode handler start, credit, cek_akun) ...
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    try: register_user(user.id, user.first_name)
    except Exception as e: logger.error(f"Gagal mendaftarkan user {user.id}: {e}")
    markup = admin_reply_markup if is_admin_check(user.id) else user_reply_markup
    await update.message.reply_text(f"Selamat datang, {user.first_name}!", reply_markup=markup)

async def check_credits_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id; user_name = update.effective_user.first_name
    user_data = get_user(user_id)
    if not user_data and user_id == ADMIN_ID: # Jika admin belum ada di DB
        try: register_user(user_id, user_name); user_data = get_user(user_id)
        except Exception as e: logger.error(f"Gagal mendaftarkan admin {user_id}: {e}"); await update.message.reply_text("Error DB."); return
    elif not user_data: await update.message.reply_text("Anda belum terdaftar. /start dulu."); return
    sisa_kredit_display = "∞ (Admin)" if is_admin_check(user_id) else user_data.get('credits', 0) # Handle jika 'credits' belum ada
    await update.message.reply_text(f"👤 **Nama:** {user_data['user_name']}\n🆔 **User ID:** `{user_id}`\n💳 **Sisa kredit:** {sisa_kredit_display}", parse_mode=ParseMode.MARKDOWN)

@check_access(permission_required="can_cek_akun", credit_cost=0) # Asumsi cek akun gratis
async def cek_akun(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_data = get_user(user_id)
    if not user_data: await update.message.reply_text("Data tidak ditemukan. /start dulu."); return # Safety check
    sisa_kredit_display = "∞ (Admin)" if is_admin_check(user_id) else user_data.get('credits', 0)
    markup = admin_reply_markup if is_admin_check(user_id) else user_reply_markup
    await update.message.reply_text(f"👤 **Nama:** {user_data['user_name']}\n🆔 **User ID:** `{user_id}`\n💳 **Sisa kredit:** {sisa_kredit_display}", parse_mode=ParseMode.MARKDOWN, reply_markup=markup)


# --- Conversation Handler Cek Akun Fore (tetap sama) ---
# ... (kode start_fore_check, receive_fore_phone, receive_fore_pin, cancel_fore_check) ...
@check_access(permission_required="can_cek_fore", credit_cost=1)
async def start_fore_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Masukkan Nomor HP Fore (tanpa +62 atau 0).\n\nKetik /cancel untuk batal.", reply_markup=ReplyKeyboardRemove()); return ASK_FORE_PHONE

async def receive_fore_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone_input = update.message.text
    phone_cleaned = re.sub(r"[^0-9]", "", phone_input)
    # Validasi input HP
    if not phone_cleaned or len(phone_cleaned) < 9 or len(phone_cleaned) > 13: # Contoh validasi panjang
        await update.message.reply_text("Format nomor HP tidak valid (9-13 digit angka). Coba lagi:")
        return ASK_FORE_PHONE
    # Normalisasi nomor (simpan tanpa 62 atau 0 di depan)
    if phone_cleaned.startswith("62"): phone_root = phone_cleaned[2:]
    elif phone_cleaned.startswith("0"): phone_root = phone_cleaned[1:]
    else: phone_root = phone_cleaned # Anggap sudah format root
    context.user_data["phone_root"] = phone_root
    await update.message.reply_text("Nomor HP diterima. Masukkan 6 digit PIN:"); return ASK_FORE_PIN

async def receive_fore_pin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    pin_input = update.message.text; phone = context.user_data.get("phone_root"); user_id = update.effective_user.id
    # Validasi input PIN
    if not (pin_input.isdigit() and len(pin_input) == 6):
        await update.message.reply_text("PIN harus 6 digit angka. Coba lagi:")
        return ASK_FORE_PIN
    await update.message.reply_text("⏳ Memproses cek akun Fore...")
    data = await process_fore_check(phone, pin_input)
    markup = admin_reply_markup if is_admin_check(user_id) else user_reply_markup
    if data.get("success"):
        try:
            if not is_admin_check(user_id):
                 # Cek kredit sebelum mengurangi
                 user_data_before = get_user(user_id)
                 if user_data_before and user_data_before.get('credits', 0) >= 1:
                     update_credits(user_id, -1)
                     logger.info(f"User {user_id}: 1 kredit dipotong (Cek Akun Fore)")
                 else:
                     logger.warning(f"User {user_id} tidak punya cukup kredit untuk Cek Akun Fore.")
                     # Opsional: Beri tahu user jika kredit habis setelah cek
                     # await update.message.reply_text("Kredit Anda habis setelah melakukan pengecekan ini.", reply_markup=markup)
        except Exception as e: logger.error(f"Gagal potong kredit {user_id} untuk Cek Akun Fore: {e}")

        nama=data.get("nama","N/A"); reff=data.get("reff","N/A"); total_poin=data.get("total_points",0); voucher_list=data.get("vouchers",[])
        voucher_display = "Tidak ada voucher aktif." if not voucher_list else "\n\n".join([f"  • *{v['name']}*\n    (Exp: `{v['end']}`)" for v in voucher_list])
        hasil_teks = (f"☕️ **Hasil Cek Akun Fore** ☕️\n\n"
                      f"👤 Nama: `{nama}`\n"
                      f"🎟️ Kode Reff: `{reff}`\n"
                      f"✨ Poin Tersedia: `{total_poin}`\n\n"
                      f"---\n🏷️ **Voucher Aktif**\n---\n{voucher_display}")
        await update.message.reply_text(hasil_teks, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
        # Auto-logout setelah cek berhasil
        at = data.get("access_token"); rt = data.get("refresh_token"); did = data.get("device_id")
        if at and rt and did:
            await api_logout(at, rt, did) # Logout di background
    else:
        await update.message.reply_text(f"❌ Gagal: {data.get('message', 'Terjadi error tidak diketahui')}", reply_markup=markup)
    context.user_data.clear(); return ConversationHandler.END

async def cancel_fore_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id; markup = admin_reply_markup if is_admin_check(user_id) else user_reply_markup
    await update.message.reply_text("Cek Akun Fore dibatalkan.", reply_markup=markup)
    context.user_data.clear(); return ConversationHandler.END


# --- Conversation Handler Cek Orderan (tetap sama) ---
# ... (kode start_order_check, receive_order_phone, receive_order_pin_and_get_list, _send_formatted_order_detail, select_order_and_show_detail, handle_refresh_or_finish, cancel_order_check) ...
@check_access(permission_required="can_cek_order", credit_cost=5) # Kredit dipotong saat detail ditampilkan
async def start_order_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Masukkan Nomor HP Fore (tanpa +62 atau 0).\n\nKetik /cancel untuk batal.", reply_markup=ReplyKeyboardRemove()); return ASK_ORDER_PHONE

async def receive_order_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone_input = update.message.text
    phone_cleaned = re.sub(r"[^0-9]", "", phone_input)
    # Validasi input HP
    if not phone_cleaned or len(phone_cleaned) < 9 or len(phone_cleaned) > 13:
        await update.message.reply_text("Format nomor HP tidak valid (9-13 digit angka). Coba lagi:")
        return ASK_ORDER_PHONE
    # Normalisasi nomor
    if phone_cleaned.startswith("62"): phone_root = phone_cleaned[2:]
    elif phone_cleaned.startswith("0"): phone_root = phone_cleaned[1:]
    else: phone_root = phone_cleaned
    context.user_data["phone_root"] = phone_root
    await update.message.reply_text("Nomor HP diterima. Masukkan 6 digit PIN:"); return ASK_ORDER_PIN

async def receive_order_pin_and_get_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    pin_input = update.message.text; phone = context.user_data.get("phone_root"); user_id = update.effective_user.id
    markup = admin_reply_markup if is_admin_check(user_id) else user_reply_markup
    # Validasi input PIN
    if not (pin_input.isdigit() and len(pin_input) == 6):
        await update.message.reply_text("PIN harus 6 digit angka. Coba lagi:")
        return ASK_ORDER_PIN
    await update.message.reply_text("⏳ Login & mencari orderan aktif...")
    login_result = await api_login_order(phone, pin_input)
    if not login_result.get("success"): await update.message.reply_text(f"Login Gagal: {login_result.get('message')}", reply_markup=markup); context.user_data.clear(); return ConversationHandler.END
    # Simpan kredensial sesi
    context.user_data['access_token']=login_result['access_token']; context.user_data['refresh_token']=login_result['refresh_token']; context.user_data['device_id']=login_result['device_id']
    orders_result = await api_get_ongoing_orders(login_result['access_token'], login_result['refresh_token'], login_result['device_id'])
    if not orders_result.get("success"):
        await update.message.reply_text(f"Gagal ambil daftar order: {orders_result.get('message')}", reply_markup=markup)
        await api_logout(login_result['access_token'], login_result['refresh_token'], login_result['device_id'])
        context.user_data.clear(); return ConversationHandler.END
    ongoing_orders = orders_result.get("data", [])
    if not ongoing_orders:
        await update.message.reply_text("Tidak ditemukan orderan yang sedang berjalan.", reply_markup=markup)
        await api_logout(login_result['access_token'], login_result['refresh_token'], login_result['device_id'])
        context.user_data.clear(); return ConversationHandler.END

    # Buat tombol inline untuk setiap order
    keyboard = []
    if len(ongoing_orders) > 1:
        summary_lines = ["🔍 **Ditemukan Beberapa Orderan Aktif:**"]
        for order in ongoing_orders:
             order_id = order.get('uor_id', 'N/A')
             queue_num = order.get('uor_queue', 'N/A')
             store_name = order.get('store', {}).get('sto_name', 'Outlet Tidak Dikenal')
             summary_lines.append(f"\n- ID: `{order_id}` | Antrian: `{queue_num}` | Outlet: `{store_name}`")
             keyboard.append([InlineKeyboardButton(f"{store_name} (Antrian {queue_num})", callback_data=f"order_select_{order_id}")])
        await update.message.reply_text("\n".join(summary_lines), parse_mode=ParseMode.MARKDOWN)
    else: # Hanya 1 order
         order = ongoing_orders[0]
         order_id = order.get('uor_id', 'N/A')
         queue_num = order.get('uor_queue', 'N/A')
         store_name = order.get('store', {}).get('sto_name', 'Outlet Tidak Dikenal')
         keyboard.append([InlineKeyboardButton(f"{store_name} (Antrian {queue_num})", callback_data=f"order_select_{order_id}")])

    keyboard.append([InlineKeyboardButton("❌ Batalkan Cek Order", callback_data="order_cancel")])
    await update.message.reply_text("Silakan pilih orderan di bawah untuk melihat detail & QR Code:", reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECTING_ORDER

async def _send_formatted_order_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict, user_id: int):
    """Fungsi helper untuk memformat, mengirim detail order, dan mengirim tombol Aksi."""
    try:
        # 1. Format Pesan Teks
        status_code = data.get('uor_status')
        if status_code == 'in_process': status = "Dalam Pembuatan 👨‍🍳"
        elif status_code == 'ready_for_pickup': status = "Ready For PickUp 🥤"
        else: status = status_code.replace('_', ' ').title() if status_code else "Status Tidak Diketahui"

        antrian = data.get('uor_queue', 'N/A')
        receipt_url = data.get('url_webview_e_receipt', None) # Jadi None jika tidak ada
        qr_hash = data.get('uorsh_hash') # Hash untuk QR code

        nama_user = data.get('user_name', 'N/A')
        nama_outlet = data.get('st_name', 'N/A')
        # Ambil st_code dari dalam 'address' jika ada
        kode_outlet = data.get('address', {}).get('st_code', 'N/A') if isinstance(data.get('address'), dict) else 'N/A'

        # Pesan estimasi atau custom
        pesan_dict = data.get('estimated_time_seconds', {})
        pesan_custom = pesan_dict.get('title_message', "Tidak ada pesan") if isinstance(pesan_dict, dict) else "Tidak ada pesan"

        produk_list = data.get('product', [])
        orderan_lines = []
        if not produk_list:
            orderan_lines.append("• Gagal memuat daftar produk")
        else:
            for prod in produk_list:
                qty = prod.get('uorpd_qty', 1)
                nama_prod = prod.get('uorpd_name', 'Produk tidak diketahui')
                orderan_lines.append(f"• {qty}x {nama_prod}")
        daftar_orderan = "\n".join(orderan_lines)

        hasil_teks_list = [
            "✅ **Detail Orderan Fore** ✅\n",
            f"👤 Nama: `{nama_user}`",
            f"🏢 Outlet: `{nama_outlet} ({kode_outlet})`",
            f"🔢 No Antrian: **{antrian}**",
            f"📊 Status: **{status}**\n",
            "--- **Pesanan Anda** ---",
            daftar_orderan,
            "\n--- **Pesan Tambahan** ---",
            f"`{pesan_custom}`\n",
        ]
        if receipt_url: # Hanya tampilkan jika URL ada
            hasil_teks_list.append(f"🧾 E-Receipt: [Lihat Struk]({receipt_url})")

        hasil_teks = "\n".join(hasil_teks_list)

        # 2. Kirim Pesan (Foto QR atau Teks saja)
        target_chat_id = update.effective_chat.id # Bisa jadi update dari callback

        if not qr_hash:
            await context.bot.send_message(chat_id=target_chat_id, text=hasil_teks, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        else:
            try:
                # Membuat QR Code dengan logo
                qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=4)
                qr.add_data(qr_hash); qr.make(fit=True)
                img = qr.make_image(fill_color="black", back_color="white").convert('RGB')

                logo_path = 'logo.png' # Pastikan file logo.png ada
                if os.path.exists(logo_path):
                    try:
                        logo = Image.open(logo_path)
                        # Resize logo agar tidak terlalu besar
                        qr_width, qr_height = img.size; logo_max_size = qr_height // 4 # Buat logo lebih kecil
                        logo.thumbnail((logo_max_size, logo_max_size), Image.Resampling.LANCZOS)
                        # Hitung posisi tengah
                        pos = ((qr_width - logo.width) // 2, (qr_height - logo.height) // 2)
                        # Tempel logo (handle transparansi jika ada)
                        if logo.mode == 'RGBA':
                             # Buat mask dari alpha channel logo untuk transparansi
                             mask = logo.split()[3]
                             img.paste(logo, pos, mask=mask)
                        else:
                             img.paste(logo, pos)
                    except Exception as logo_err:
                        logger.warning(f"Gagal menambahkan logo ke QR: {logo_err}. Mengirim QR standar.")
                else:
                    logger.warning("logo.png tidak ditemukan. Mengirim QR standar.")

                bio = io.BytesIO(); img.save(bio, 'PNG'); bio.seek(0)
                await context.bot.send_photo(chat_id=target_chat_id, photo=bio, caption=hasil_teks, parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                logger.error(f"Gagal generate/send QR code: {e}. Mengirim teks saja.")
                await context.bot.send_message(chat_id=target_chat_id, text=f"{hasil_teks}\n\n(Gagal generate QR Code)", parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

        # 3. Kirim Tombol Aksi (Refresh/Selesai)
        refresh_keyboard = [[
            InlineKeyboardButton("🔄 Refresh Status", callback_data="order_action_refresh"),
            InlineKeyboardButton("✅ Selesai & Logout", callback_data="order_action_finish")
        ]]
        refresh_markup = InlineKeyboardMarkup(refresh_keyboard)
        # Pastikan tombol dikirim sebagai pesan baru jika pesan sebelumnya adalah foto
        if qr_hash:
             await context.bot.send_message(chat_id=target_chat_id, text="Gunakan tombol di bawah:", reply_markup=refresh_markup)
        else: # Jika pesan sebelumnya teks, bisa edit reply_markupnya (opsional)
             # Cari pesan terakhir bot di chat ini (agak kompleks, lebih aman kirim pesan baru)
             await context.bot.send_message(chat_id=target_chat_id, text="Gunakan tombol di bawah:", reply_markup=refresh_markup)


    except Exception as e:
        logger.exception(f"Gagal memformat/mengirim detail order:")
        target_chat_id = update.effective_chat.id
        await context.bot.send_message(chat_id=target_chat_id, text=f"Terjadi error saat menampilkan detail order: {e}")


async def select_order_and_show_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer() # Jawab callback dulu
    query_data = query.data; user_id = update.effective_user.id
    markup = admin_reply_markup if is_admin_check(user_id) else user_reply_markup

    access_token=context.user_data.get('access_token'); refresh_token=context.user_data.get('refresh_token'); device_id=context.user_data.get('device_id')

    # Handle Batal
    if query_data == "order_cancel":
        await query.edit_message_text("Cek order dibatalkan.")
        if access_token and refresh_token and device_id:
            await api_logout(access_token, refresh_token, device_id) # Logout
        await context.bot.send_message(chat_id=user_id, text="Pilih aksi selanjutnya.", reply_markup=markup)
        context.user_data.clear(); return ConversationHandler.END

    # Ekstrak uor_id
    try: uor_id = query_data.split("_")[-1]
    except Exception:
        logger.warning(f"Callback data order tidak valid: {query_data}")
        await query.edit_message_text("Data order tidak valid. Dibatalkan.")
        if access_token and refresh_token and device_id: await api_logout(access_token, refresh_token, device_id)
        context.user_data.clear(); return ConversationHandler.END

    # Cek kredensial sesi
    if not all([access_token, refresh_token, device_id]):
        await query.edit_message_text("Sesi login Anda telah berakhir. Silakan ulangi dari awal.")
        context.user_data.clear(); return ConversationHandler.END

    await query.edit_message_text(f"⏳ Mengambil detail orderan `{uor_id}`...", parse_mode=ParseMode.MARKDOWN)
    context.user_data['uor_id'] = uor_id # Simpan uor_id untuk refresh

    detail_result = await api_get_order_detail(access_token, refresh_token, device_id, uor_id)

    if not detail_result.get("success"):
        await context.bot.send_message(chat_id=user_id, text=f"Gagal mengambil detail: {detail_result.get('message')}", reply_markup=markup)
        await api_logout(access_token, refresh_token, device_id)
        context.user_data.clear(); return ConversationHandler.END

    data = detail_result.get("data", {})
    if not data:
        await context.bot.send_message(chat_id=user_id, text="Tidak ada data detail untuk orderan ini.", reply_markup=markup)
        await api_logout(access_token, refresh_token, device_id)
        context.user_data.clear(); return ConversationHandler.END

    # Potong kredit HANYA jika belum pernah dipotong untuk order ini
    cost = 5
    credit_deducted_key = f'credit_deducted_for_{uor_id}' # Buat key unik per order
    if not is_admin_check(user_id) and not context.user_data.get(credit_deducted_key, False):
        user_data_before = get_user(user_id) # Cek kredit sebelum potong
        if user_data_before and user_data_before.get('credits', 0) >= cost:
            try:
                update_credits(user_id, -cost);
                context.user_data[credit_deducted_key] = True # Tandai sudah potong untuk order ini
                logger.info(f"User {user_id}: {cost} kredit dipotong (Cek Order {uor_id})")
            except Exception as e:
                logger.error(f"Gagal potong kredit {user_id} untuk Cek Order {uor_id}: {e}");
                await context.bot.send_message(chat_id=user_id, text="❌ Terjadi error saat memotong kredit. Silakan hubungi admin.", reply_markup=markup)
                await api_logout(access_token, refresh_token, device_id)
                context.user_data.clear(); return ConversationHandler.END
        else: # Kredit tidak cukup saat mau potong
             await context.bot.send_message(chat_id=user_id, text=f"❌ Kredit Anda ({user_data_before.get('credits', 0)}) tidak mencukupi untuk melihat detail (butuh {cost}).", reply_markup=markup)
             await api_logout(access_token, refresh_token, device_id)
             context.user_data.clear(); return ConversationHandler.END


    # Kirim detail yang diformat
    await _send_formatted_order_detail(update, context, data, user_id)
    return AWAITING_REFRESH # Pindah ke state menunggu aksi refresh/finish

async def handle_refresh_or_finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer(); query_data = query.data
    user_id = update.effective_user.id
    markup = admin_reply_markup if is_admin_check(user_id) else user_reply_markup

    access_token=context.user_data.get('access_token'); refresh_token=context.user_data.get('refresh_token'); device_id=context.user_data.get('device_id'); uor_id=context.user_data.get('uor_id')

    # Cek jika sesi masih valid
    if not all([access_token, refresh_token, device_id, uor_id]):
        await query.edit_message_text("Sesi Anda telah berakhir atau data tidak lengkap. Silakan ulangi.")
        context.user_data.clear(); return ConversationHandler.END

    if query_data == "order_action_finish":
        await query.edit_message_text("✅ Selesai. Menutup sesi cek order...")
        await api_logout(access_token, refresh_token, device_id) # Logout
        await context.bot.send_message(chat_id=user_id, text="Sesi cek order ditutup. Pilih aksi selanjutnya.", reply_markup=markup)
        context.user_data.clear()
        return ConversationHandler.END

    elif query_data == "order_action_refresh":
        # Hapus pesan tombol refresh/selesai yang lama
        try: await query.delete_message()
        except Exception as e: logger.debug(f"Gagal hapus pesan tombol refresh: {e}") # Tidak kritis

        await context.bot.send_message(chat_id=user_id, text="🔄 Meresfresh status orderan...") # Kirim pesan baru

        detail_result = await api_get_order_detail(access_token, refresh_token, device_id, uor_id)

        if not detail_result.get("success") or not detail_result.get("data"):
            # Informasikan user gagal refresh, tapi JANGAN tutup sesi
            await context.bot.send_message(chat_id=user_id, text=f"Gagal refresh data: {detail_result.get('message', 'Data kosong')}.")
            # Kirim ulang tombol refresh/selesai agar user bisa coba lagi atau selesai
            refresh_keyboard = [[
                 InlineKeyboardButton("🔄 Refresh Status", callback_data="order_action_refresh"),
                 InlineKeyboardButton("✅ Selesai & Logout", callback_data="order_action_finish")
             ]]
            refresh_markup = InlineKeyboardMarkup(refresh_keyboard)
            await context.bot.send_message(chat_id=user_id, text="Gunakan tombol di bawah:", reply_markup=refresh_markup)
            return AWAITING_REFRESH # Tetap di state ini

        # Kirim ulang detail yang sudah di-refresh
        data = detail_result.get("data", {})
        await _send_formatted_order_detail(update, context, data, user_id) # Fungsi ini sudah termasuk kirim tombol lagi
        return AWAITING_REFRESH # Tetap di state menunggu aksi

    else: # Callback tidak dikenal
        logger.warning(f"Callback tidak dikenal di AWAITING_REFRESH: {query_data}")
        return AWAITING_REFRESH

async def cancel_order_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id; markup = admin_reply_markup if is_admin_check(user_id) else user_reply_markup
    access_token=context.user_data.get('access_token'); refresh_token=context.user_data.get('refresh_token'); device_id=context.user_data.get('device_id')
    if access_token and refresh_token and device_id:
        await api_logout(access_token, refresh_token, device_id) # Logout jika ada sesi
    await update.message.reply_text("Cek orderan dibatalkan.", reply_markup=markup)
    context.user_data.clear(); return ConversationHandler.END


# --- Conversation Handler Admin (tetap sama) ---
# ... (kode build_user_list_keyboard, start_admin_flow, admin_user_list_callback, receive_credit_amount, receive_permission_type, cancel_admin_flow) ...
def build_user_list_keyboard(users: list, total_pages: int, current_page: int, action: str) -> InlineKeyboardMarkup:
    keyboard = []
    # Baris data user
    for user in users:
        keyboard.append([InlineKeyboardButton(f"{user['user_name']} ({user['user_id']})", callback_data=f"admin_{action}_select_{user['user_id']}")])
    # Baris navigasi
    nav_row = []
    if current_page > 1: nav_row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"admin_{action}_page_{current_page - 1}"))
    nav_row.append(InlineKeyboardButton(f"Hal {current_page}/{total_pages}", callback_data="admin_nop")) # Tombol no-op
    if current_page < total_pages: nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"admin_{action}_page_{current_page + 1}"))
    if nav_row: keyboard.append(nav_row)
    # Tombol batal
    keyboard.append([InlineKeyboardButton("❌ Batalkan Aksi", callback_data="admin_cancel")])
    return InlineKeyboardMarkup(keyboard)

@admin_only
async def start_admin_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    action_text = update.message.text
    if action_text == "Tambah Kredit": action = "credit"; prompt = "Pilih user untuk ditambah/dikurangi kreditnya:"
    elif action_text == "Beri Izin": action = "grant"; prompt = "Pilih user untuk diberi izin:"
    elif action_text == "Cabut Izin": action = "revoke"; prompt = "Pilih user untuk dicabut izinnya:"
    else: return ConversationHandler.END # Jika teks tidak cocok

    context.user_data['admin_action'] = action
    users, total_pages = get_users_paginated(page=1, per_page=USERS_PER_PAGE, exclude_admin_id=ADMIN_ID) # Exclude admin utama

    if not users:
        await update.message.reply_text("Tidak ada user non-admin lain yang terdaftar.", reply_markup=admin_reply_markup)
        return ConversationHandler.END

    keyboard = build_user_list_keyboard(users, total_pages, 1, action)
    await update.message.reply_text(f"{prompt}\n(Halaman 1/{total_pages})", reply_markup=keyboard)
    return SELECTING_USER

async def admin_user_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    query_data = query.data

    if query_data == "admin_nop": return SELECTING_USER # Abaikan klik pada nomor halaman
    if query_data == "admin_cancel":
        await query.edit_message_text("Aksi admin dibatalkan.")
        context.user_data.clear(); return ConversationHandler.END

    try:
        parts = query_data.split("_")
        if len(parts) < 3: raise ValueError("Format callback tidak lengkap")
        prefix, action, command = parts[0], parts[1], parts[2]
        value = parts[3] if len(parts) > 3 else None
        if prefix != 'admin': raise ValueError("Bukan callback admin")
    except ValueError as e:
        logger.warning(f"Callback data admin tidak valid: {query_data} - Error: {e}")
        await query.edit_message_text("Terjadi kesalahan data callback.")
        context.user_data.clear(); return ConversationHandler.END

    context.user_data['admin_action'] = action # Simpan/update action

    if command == "page":
        try:
            page = int(value)
            users, total_pages = get_users_paginated(page=page, per_page=USERS_PER_PAGE, exclude_admin_id=ADMIN_ID)
            keyboard = build_user_list_keyboard(users, total_pages, page, action)
            await query.edit_message_text(f"Pilih user:\n(Halaman {page}/{total_pages})", reply_markup=keyboard)
            return SELECTING_USER
        except ValueError:
             logger.warning(f"Nilai halaman tidak valid: {value}")
             await query.edit_message_text("Halaman tidak valid.")
             return SELECTING_USER # Kembali ke state seleksi
        except Exception as e:
             logger.error(f"Error saat paginasi user: {e}")
             await query.edit_message_text("Gagal memuat halaman user.")
             return SELECTING_USER

    elif command == "select":
        try:
            target_user_id = int(value)
            target_user = get_user(target_user_id)
            if not target_user:
                await query.edit_message_text("User tidak ditemukan di database.")
                context.user_data.clear(); return ConversationHandler.END

            context.user_data['target_user_id'] = target_user_id
            context.user_data['target_user_name'] = target_user['user_name']

            await query.delete_message() # Hapus pesan daftar user

            if action == "credit":
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"Target: **{target_user['user_name']}** (`{target_user_id}`)\n"
                         f"Masukkan jumlah kredit yang ingin ditambahkan (angka positif) atau dikurangi (angka negatif, misal `-10`).\n\nKetik /cancel untuk batal.",
                    parse_mode=ParseMode.MARKDOWN
                )
                return ASKING_CREDIT_AMOUNT

            elif action in ("grant", "revoke"):
                perm_keyboard = [[InlineKeyboardButton(p.replace('_', ' ').title(), callback_data=f"admin_perm_{p}")] for p in PERMISSION_NAMES if p != 'is_admin'] # Jangan tampilkan is_admin
                perm_keyboard.append([InlineKeyboardButton("❌ Batalkan", callback_data="admin_cancel_perm")]) # Callback batal khusus
                verb = "diberikan" if action == "grant" else "dicabut"
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"Target: **{target_user['user_name']}** (`{target_user_id}`)\n"
                         f"Pilih izin yang ingin **{verb}**:\n\nKetik /cancel untuk batal.",
                    reply_markup=InlineKeyboardMarkup(perm_keyboard),
                    parse_mode=ParseMode.MARKDOWN
                )
                return ASKING_PERMISSION_TYPE
            else: # Action tidak dikenal
                 await context.bot.send_message(chat_id=update.effective_chat.id, text="Aksi tidak dikenal.", reply_markup=admin_reply_markup)
                 context.user_data.clear(); return ConversationHandler.END

        except ValueError:
            logger.warning(f"User ID tidak valid: {value}")
            await query.edit_message_text("User ID tidak valid.")
            return SELECTING_USER
        except Exception as e:
            logger.error(f"Error saat memilih user: {e}")
            await query.edit_message_text("Gagal memilih user.")
            context.user_data.clear(); return ConversationHandler.END
    else: # Command tidak dikenal
         logger.warning(f"Admin command tidak dikenal: {command}")
         return SELECTING_USER


async def receive_credit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try: amount = int(update.message.text)
    except ValueError: await update.message.reply_text("Jumlah harus berupa angka bulat (positif atau negatif). Coba lagi:"); return ASKING_CREDIT_AMOUNT

    target_user_id = context.user_data.get('target_user_id')
    target_user_name = context.user_data.get('target_user_name', 'N/A')

    if target_user_id is None: # Safety check
         await update.message.reply_text("User target tidak ditemukan. Aksi dibatalkan.", reply_markup=admin_reply_markup)
         context.user_data.clear(); return ConversationHandler.END

    try:
        update_credits(target_user_id, amount); new_data = get_user(target_user_id)
        if not new_data: raise Exception("User tidak ditemukan setelah update") # Handle jika user hilang
        operation = "ditambahkan" if amount >= 0 else "dikurangi"
        await update.message.reply_text(
            f"✅ Berhasil! **{abs(amount)}** kredit telah **{operation}**.\n"
            f"User: {target_user_name} (`{target_user_id}`)\n"
            f"Total kredit sekarang: **{new_data.get('credits', 'Error')}**", # Ambil kredit terbaru
            parse_mode=ParseMode.MARKDOWN, reply_markup=admin_reply_markup
        )
    except Exception as e:
        logger.error(f"Gagal update kredit untuk {target_user_id}: {e}")
        await update.message.reply_text(f"❌ Gagal update database: {e}", reply_markup=admin_reply_markup)

    context.user_data.clear(); return ConversationHandler.END


async def receive_permission_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    query_data = query.data

    # Handle batal dari tombol inline
    if query_data == "admin_cancel_perm":
        await query.edit_message_text("Aksi izin dibatalkan.")
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Pilih aksi admin.", reply_markup=admin_reply_markup) # Kirim ulang menu utama admin
        context.user_data.clear(); return ConversationHandler.END

    # Ekstrak nama permission
    try:
        parts = query_data.split("_")
        if len(parts) < 3 or parts[0] != 'admin' or parts[1] != 'perm': raise ValueError
        permission_name = "_".join(parts[2:]) # Handle permission_name dengan underscore
        if permission_name not in PERMISSION_NAMES or permission_name == 'is_admin': raise ValueError # Jangan izinkan ubah is_admin
    except ValueError:
        logger.warning(f"Callback permission tidak valid: {query_data}")
        await query.edit_message_text("Pilihan izin tidak valid. Dibatalkan.")
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Pilih aksi admin.", reply_markup=admin_reply_markup)
        context.user_data.clear(); return ConversationHandler.END

    target_user_id = context.user_data.get('target_user_id')
    target_user_name = context.user_data.get('target_user_name', 'N/A')
    action = context.user_data.get('admin_action') # 'grant' or 'revoke'

    if target_user_id is None or action not in ('grant', 'revoke'): # Safety check
        await query.edit_message_text("Data aksi tidak lengkap. Dibatalkan.")
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Pilih aksi admin.", reply_markup=admin_reply_markup)
        context.user_data.clear(); return ConversationHandler.END

    action_value = 1 if action == 'grant' else 0
    action_text = "DIBERIKAN" if action == 'grant' else "DICABUT"

    try:
        set_permission(target_user_id, permission_name, action_value)
        perm_display_name = permission_name.replace('_', ' ').title() # Buat nama lebih mudah dibaca
        await query.edit_message_text(
            f"✅ Berhasil!\n"
            f"User: {target_user_name} (`{target_user_id}`)\n"
            f"Izin '**{perm_display_name}**' telah **{action_text}**.",
            parse_mode=ParseMode.MARKDOWN
        )
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Pilih aksi admin selanjutnya.", reply_markup=admin_reply_markup) # Kirim ulang menu
    except Exception as e:
        logger.error(f"Gagal update izin {permission_name} untuk {target_user_id}: {e}")
        await query.edit_message_text(f"❌ Gagal update database: {e}")
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Pilih aksi admin.", reply_markup=admin_reply_markup)

    context.user_data.clear(); return ConversationHandler.END


async def cancel_admin_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        try: await update.callback_query.edit_message_text("Aksi admin dibatalkan.")
        except Exception: pass
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Menu Admin:", reply_markup=admin_reply_markup)
    elif update.message:
        await update.message.reply_text("Aksi admin dibatalkan.", reply_markup=admin_reply_markup)
    else:
        logger.warning("Cancel admin dipanggil tanpa callback atau message.")
        # Coba kirim ke user_id jika ada di context
        user_id = context.user_data.get('user_id') or getattr(update.effective_user, 'id', None)
        if user_id:
            await context.bot.send_message(chat_id=user_id, text="Aksi dibatalkan.", reply_markup=admin_reply_markup)

    context.user_data.clear(); return ConversationHandler.END

# --- Auto Order Conversation Handlers ---

# <<< BARU: Helper untuk build keyboard store list >>>
def build_store_keyboard(stores: list, keyword: str) -> tuple[InlineKeyboardMarkup, dict]:
    """Membangun keyboard inline untuk daftar toko dan map ID ke nama."""
    keyboard = []
    store_map = {}
    for store in stores:
        st_id = store.get('st_id')
        st_name = store.get('st_name', f'ID: {st_id}')
        if st_id:
            button_text = st_name[:50] + '...' if len(st_name) > 50 else st_name
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"auto_order_select_{st_id}")])
            store_map[str(st_id)] = st_name # Simpan nama lengkap
    # Tambah tombol aksi
    keyboard.append([
        InlineKeyboardButton("🔄 Cari Ulang", callback_data="auto_order_search_again"),
        InlineKeyboardButton("❌ Batal", callback_data="auto_order_cancel")
    ])
    return InlineKeyboardMarkup(keyboard), store_map

# @check_access(permission_required="can_auto_order", credit_cost=0) # Aktifkan jika perlu permission
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

    # Simpan kredensial sesi untuk Auto Order
    context.user_data['auto_order_access_token'] = login_result['access_token']
    context.user_data['auto_order_refresh_token'] = login_result['refresh_token']
    context.user_data['auto_order_device_id'] = login_result['device_id']
    logger.info(f"User {user_id} berhasil login untuk Auto Order.")

    await update.message.reply_text(
        "✅ Login berhasil!\n"
        "Sekarang masukkan kata kunci nama/lokasi toko yang ingin dicari (contoh: `Sudirman`, `Bandung`, `Grand Indonesia`):",
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

    # Simpan hasil pencarian dan keyword
    context.user_data['found_stores'] = active_stores
    context.user_data['current_keyword'] = keyword

    # Bangun keyboard dan map
    keyboard, store_map = build_store_keyboard(active_stores, keyword)
    context.user_data['store_map'] = store_map

    await update.message.reply_text(
        f"✅ Ditemukan {len(active_stores)} toko aktif untuk `{keyword}`.\n"
        f"Silakan pilih salah satu:",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN
    )
    return SHOW_STORE_LIST

# <<< MODIFIKASI: Handler untuk Pemilihan Toko >>>
async def handle_store_selection_or_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    query_data = query.data
    user_id = update.effective_user.id
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
        # Hapus data pencarian lama
        context.user_data.pop('found_stores', None)
        context.user_data.pop('store_map', None)
        context.user_data.pop('current_keyword', None)
        return ASK_STORE_KEYWORD

    elif query_data.startswith("auto_order_select_"):
        try:
            selected_store_id = int(query_data.split("_")[-1])
            selected_store_data = None
            for store in found_stores:
                if store.get('st_id') == selected_store_id:
                    selected_store_data = store
                    break

            if not selected_store_data:
                logger.warning(f"Store ID {selected_store_id} dipilih tapi tidak ditemukan di found_stores.")
                await query.edit_message_text("Terjadi kesalahan: Toko yang dipilih tidak ditemukan. Coba cari ulang.")
                # Bangun ulang keyboard jika masih ada data
                if found_stores:
                    keyboard, _ = build_store_keyboard(found_stores, current_keyword)
                    await query.message.reply_text(
                        f"Silakan pilih lagi dari daftar untuk `{current_keyword}`:",
                        reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN
                    )
                return SHOW_STORE_LIST # Kembali ke list

            # Simpan data lengkap toko yang dipilih
            context.user_data['selected_store_data'] = selected_store_data

            # Format detail untuk verifikasi
            store_name = selected_store_data.get('st_name', 'N/A')
            store_code = selected_store_data.get('st_code', 'N/A')
            address = selected_store_data.get('st_address', 'N/A')
            # Coba ambil detail link dari 'st_dllink', jika tidak ada, coba 'st_direction_link'
            map_link = selected_store_data.get('st_dllink') or selected_store_data.get('st_direction_link')
            phone = selected_store_data.get('st_phone', 'N/A')
            open_hour = selected_store_data.get('st_open', 'N/A')
            close_hour = selected_store_data.get('st_close', 'N/A')
            # sti_img tidak ada di respons /store/all, jadi kita tidak bisa menampilkannya

            confirmation_text_list = [
                f"🏪 **Konfirmasi Toko Pilihan** 🏪\n",
                f"**Nama:** {store_name}",
                f"**Kode:** `{store_code}`",
                f"**Alamat:** {address}",
            ]
            if map_link: # Tampilkan link jika ada
                 confirmation_text_list.append(f"**Peta:** [Lihat di Peta]({map_link})")

            confirmation_text_list.extend([
                f"**Telp:** `{phone}`",
                f"**Jam Buka:** {open_hour} - {close_hour}\n",
                f"Apakah ini toko yang benar?"
            ])
            confirmation_text = "\n".join(confirmation_text_list)

            confirm_keyboard = [
                [InlineKeyboardButton("✅ Ya, Konfirmasi Toko Ini", callback_data="auto_order_confirm")],
                [InlineKeyboardButton("❌ Tidak, Pilih Ulang", callback_data="auto_order_reselect")]
            ]

            await query.edit_message_text(
                confirmation_text,
                reply_markup=InlineKeyboardMarkup(confirm_keyboard),
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True # Hindari preview besar dari link peta
            )
            return CONFIRM_STORE_SELECTION # Pindah ke state konfirmasi

        except (ValueError, IndexError):
            logger.warning(f"Callback data pemilihan toko tidak valid: {query_data}")
            await query.edit_message_text("Pilihan toko tidak valid. Silakan coba lagi.")
            keyboard, _ = build_store_keyboard(found_stores, current_keyword)
            await query.message.reply_text(
                f"Silakan pilih lagi dari daftar untuk `{current_keyword}`:",
                 reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN
            )
            return SHOW_STORE_LIST

    else:
        logger.warning(f"Callback tidak dikenal di SHOW_STORE_LIST: {query_data}")
        return SHOW_STORE_LIST

# <<< BARU: Handler untuk Konfirmasi Toko >>>
async def handle_store_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    query_data = query.data
    user_id = update.effective_user.id
    markup = admin_reply_markup if is_admin_check(user_id) else user_reply_markup

    found_stores = context.user_data.get('found_stores', [])
    current_keyword = context.user_data.get('current_keyword', '')
    selected_store_data = context.user_data.get('selected_store_data')

    if query_data == "auto_order_reselect":
        # Kembali ke daftar toko
        if not found_stores: # Jika data hilang (seharusnya tidak terjadi)
            await query.edit_message_text("Data toko sebelumnya tidak ditemukan. Silakan /cancel dan coba lagi.")
            return CONFIRM_STORE_SELECTION # Tetap di state ini

        keyboard, _ = build_store_keyboard(found_stores, current_keyword)
        await query.edit_message_text(
             f"Silakan pilih lagi toko untuk keyword `{current_keyword}`:",
             reply_markup=keyboard,
             parse_mode=ParseMode.MARKDOWN
        )
        # Hapus data toko terpilih sebelumnya
        context.user_data.pop('selected_store_data', None)
        return SHOW_STORE_LIST # Kembali ke state daftar

    elif query_data == "auto_order_confirm":
        if not selected_store_data:
            await query.edit_message_text("Gagal mengonfirmasi, data toko tidak ditemukan. Silakan /cancel.")
            return CONFIRM_STORE_SELECTION

        store_name = selected_store_data.get('st_name', 'N/A')
        store_id = selected_store_data.get('st_id', 'N/A')

        logger.info(f"User {user_id} mengonfirmasi toko: {store_name} ({store_id})")
        await query.edit_message_text(f"✅ Toko **{store_name}** dikonfirmasi!", parse_mode=ParseMode.MARKDOWN)

        # --- Akhir Tahap Verifikasi ---
        await context.bot.send_message(
            chat_id=user_id,
            text="Selanjutnya adalah pemilihan produk (Tahap 2 - belum diimplementasikan). Sesi Auto Order selesai untuk saat ini.",
            reply_markup=markup
        )

        # Bersihkan data pencarian toko, tapi simpan token & toko terpilih
        context.user_data.pop('found_stores', None)
        context.user_data.pop('store_map', None)
        context.user_data.pop('current_keyword', None)
        # context.user_data['tahap_selanjutnya'] = 'pilih_produk' # Tandai state logis berikutnya
        return ConversationHandler.END # Akhiri conversation handler ini

    else:
        logger.warning(f"Callback tidak dikenal di CONFIRM_STORE_SELECTION: {query_data}")
        return CONFIRM_STORE_SELECTION

# <<< BARU: Fungsi Cancel untuk Auto Order >>>
async def cancel_auto_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    markup = admin_reply_markup if is_admin_check(user_id) else user_reply_markup
    access_token = context.user_data.get('auto_order_access_token')
    refresh_token = context.user_data.get('auto_order_refresh_token')
    device_id = context.user_data.get('auto_order_device_id')

    # Logout jika ada sesi aktif
    if access_token and refresh_token and device_id:
        await api_logout(access_token, refresh_token, device_id)

    # Kirim pesan pembatalan
    cancel_message = "❌ Auto Order dibatalkan."
    if update.callback_query:
        await update.callback_query.answer()
        try: await update.callback_query.edit_message_text(cancel_message)
        except Exception: pass # Abaikan jika pesan sudah tidak ada
        # Kirim pesan baru untuk menu utama
        await context.bot.send_message(chat_id=user_id, text="Pilih aksi.", reply_markup=markup)
    elif update.message:
        await update.message.reply_text(cancel_message, reply_markup=markup)
    else: # Fallback
        if user_id:
            await context.bot.send_message(chat_id=user_id, text=cancel_message, reply_markup=markup)

    context.user_data.clear() # Bersihkan semua data sesi
    return ConversationHandler.END


# --- Fungsi Utama (Main) --- <<< MODIFIKASI Conversation Handler Auto Order >>>
def main() -> None:
    if not TOKEN: logger.critical("Error: TELEGRAM_TOKEN tidak ditemukan"); return
    persistence = PicklePersistence(filepath="bot_data.pkl")
    application = ApplicationBuilder().token(TOKEN).persistence(persistence).build()

    # --- Handler Cek Akun Fore ---
    fore_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^Cek Akun Fore$"), start_fore_check)],
        states={
            ASK_FORE_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_fore_phone)],
            ASK_FORE_PIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_fore_pin)],
        },
        fallbacks=[CommandHandler("cancel", cancel_fore_check)],
        per_user=True, per_message=False, conversation_timeout=DEFAULT_TIMEOUT_SECONDS
    )

    # --- Handler Admin ---
    admin_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & (filters.Regex("^Tambah Kredit$") | filters.Regex("^Beri Izin$") | filters.Regex("^Cabut Izin$")), start_admin_flow)],
        states={
            SELECTING_USER: [CallbackQueryHandler(admin_user_list_callback, pattern="^admin_")],
            ASKING_CREDIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_credit_amount)],
            ASKING_PERMISSION_TYPE: [CallbackQueryHandler(receive_permission_type, pattern="^admin_perm_"), CallbackQueryHandler(cancel_admin_flow, pattern="^admin_cancel_perm$")],
        },
        fallbacks=[CommandHandler("cancel", cancel_admin_flow), CallbackQueryHandler(cancel_admin_flow, pattern="^admin_cancel$")],
        per_user=True, per_message=False, conversation_timeout=DEFAULT_TIMEOUT_SECONDS
    )

    # --- Handler Cek Orderan ---
    order_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^Cek Orderan$"), start_order_check)],
        states={
            ASK_ORDER_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_order_phone)],
            ASK_ORDER_PIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_order_pin_and_get_list)],
            SELECTING_ORDER: [CallbackQueryHandler(select_order_and_show_detail, pattern="^order_select_|^order_cancel$")],
            AWAITING_REFRESH: [CallbackQueryHandler(handle_refresh_or_finish, pattern="^order_action_")]
        },
        fallbacks=[CommandHandler("cancel", cancel_order_check)],
        per_user=True, per_message=False, conversation_timeout=DEFAULT_TIMEOUT_SECONDS
    )

    # --- Handler Auto Order --- <<< DIPERBARUI >>>
    auto_order_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^Auto Order$"), start_auto_order_login)],
        states={
            ASK_LOGIN_PHONE_FOR_AUTO_ORDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_auto_order_login_phone)],
            ASK_LOGIN_PIN_FOR_AUTO_ORDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_auto_order_login_pin_and_ask_keyword)],
            ASK_STORE_KEYWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_store_keyword)],
            SHOW_STORE_LIST: [CallbackQueryHandler(handle_store_selection_or_action, pattern="^auto_order_")],
            CONFIRM_STORE_SELECTION: [CallbackQueryHandler(handle_store_confirmation, pattern="^auto_order_(confirm|reselect)$")], # <<< BARU
        },
        fallbacks=[CommandHandler("cancel", cancel_auto_order), CallbackQueryHandler(cancel_auto_order, pattern="^auto_order_cancel$")], # <<< MODIFIKASI Fallback
        per_user=True, per_message=False,
        conversation_timeout=DEFAULT_TIMEOUT_SECONDS,
    )

    # Daftarkan semua handler
    application.add_handler(fore_conv_handler)
    application.add_handler(admin_conv_handler)
    application.add_handler(order_conv_handler)
    application.add_handler(auto_order_conv_handler) # Ditambahkan

    # Handler Perintah & Pesan Biasa
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("credit", check_credits_command))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex("^Cek Akun$"), cek_akun))

    logger.info("Bot siap dijalankan...")
    application.run_polling()

if __name__ == '__main__':
    main()