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
from PIL import Image  # Untuk QR Code kustom

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ConversationHandler,
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

# --- Konstanta API BARU (CEK ORDER) ---
APP_VERSION_ORDER = '4.8.0'
SECRET_KEY_ORDER = '0kFe6Oc3R1eEa2CpO2FeFdzElp'
PUSH_TOKEN_ORDER = 'eR0EtNreq07htx3o06Hwwv:APA91bHqmhJLoFT0tAWBVGW0klBY-O3YmjHsGrQjFPlh4EvewiMzm8gBR422Ob6O9aMjH5n3cIXcF6-BGShYC6C7KC0Ymrxrkkp-bGe6fXsGNfsEZwjOuPk'
USER_AGENT_ORDER = f'Fore Coffee/{APP_VERSION_ORDER} (coffee.fore.fore; build:1553; iOS 18.5.0) Alamofire/4.9.1'

# Konstanta Umum
PLATFORM = 'ios'
OS_VERSION = '18.5'
DEVICE_MODEL = 'iPhone 12'
COUNTRY_ID = '1'
LANGUAGE = 'id'

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
    """Decorator canggih: Cek Izin dan Kredit."""
    def decorator(func):
        @wraps(func)
        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            user_id = update.effective_user.id
            if user_id == ADMIN_ID: return await func(update, context, *args, **kwargs)
            user_data = get_user(user_id)
            if not user_data:
                await update.message.reply_text("Anda belum terdaftar. Silakan ketik /start dulu."); return
            if user_data['is_admin'] == 1: return await func(update, context, *args, **kwargs)
            if not user_data[permission_required] == 1:
                await update.message.reply_text("Anda tidak memiliki izin, hubungi admin untuk mendapatkan izin."); return
            if user_data['credits'] < credit_cost:
                await update.message.reply_text(f"Kredit Anda tidak mencukupi (Sisa: {user_data['credits']}, Butuh: {credit_cost})"); return
            return await func(update, context, *args, **kwargs)
        return wrapped
    return decorator
# -----------------------------

# --- 3. Definisi Keyboard & State ---
ASK_FORE_PHONE, ASK_FORE_PIN = range(2)
SELECTING_USER, ASKING_CREDIT_AMOUNT, ASKING_PERMISSION_TYPE = range(2, 5)
ASK_ORDER_PHONE, ASK_ORDER_PIN, SELECTING_ORDER = range(5, 8)
AWAITING_REFRESH = range(8, 9) # State untuk Refresh
PERMISSION_NAMES = ['can_cek_akun', 'can_cek_fore', 'is_admin', 'can_cek_order']
USERS_PER_PAGE = 5
user_keyboard = [[KeyboardButton("Cek Akun"), KeyboardButton("Cek Akun Fore")], [KeyboardButton("Cek Orderan")]]
user_reply_markup = ReplyKeyboardMarkup(user_keyboard, resize_keyboard=True)
admin_keyboard = [[KeyboardButton("Cek Akun"), KeyboardButton("Cek Akun Fore")], [KeyboardButton("Cek Orderan")], [KeyboardButton("Tambah Kredit"), KeyboardButton("Beri Izin"), KeyboardButton("Cabut Izin")]]
admin_reply_markup = ReplyKeyboardMarkup(admin_keyboard, resize_keyboard=True)
def is_admin_check(user_id: int) -> bool:
    if user_id == ADMIN_ID: return True
    user_data = get_user(user_id)
    if user_data and user_data['is_admin'] == 1: return True
    return False

# --- 4. Fungsi Logika API (Cek Akun Fore) ---
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
            headers_step4={'access-token':access_token,'device-id':device_id,'app-version':APP_VERSION_FORE,'secret-key':SECRET_KEY_FORE,'platform':PLATFORM,'user-agent':USER_AGENT_FORE}
            resp4=await client.get('https://api.fore.coffee/loyalty/history',headers=headers_step4);resp4.raise_for_status();points_data=resp4.json();history=points_data.get('payload',[]);total_poin=0
            for item in history:
                jenis=item.get('lylhis_type_remarks','');jumlah=item.get('ulylhis_amount',0)
                if jenis in ['Poin Didapat','Bonus Poin','Poin Ditukar']:total_poin+=jumlah
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

# --- 5. Fungsi Logika API (Cek Order & Logout) ---
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
            if login_data.get('payload',{}).get('code')!='success':raise ValueError("Gagal login, cek nomor atau PIN")
            new_access_token=login_data.get('payload',{}).get('access_token',access_token);new_refresh_token=login_data.get('payload',{}).get('refresh_token',refresh_token)
            return{"success":True,"access_token":new_access_token,"refresh_token":new_refresh_token,"device_id":device_id}
        except Exception as e:logger.error(f"Gagal api_login_order: {e}");return{"success":False,"message":str(e)}

async def api_get_ongoing_orders(access_token: str, refresh_token: str, device_id: str) -> dict:
    async with httpx.AsyncClient() as client:
        try:
            headers={'Host':'api.fore.coffee','language':'id','User-Agent':USER_AGENT_ORDER,'push-token':PUSH_TOKEN_ORDER,'secret-key':SECRET_KEY_ORDER,'refresh-token':refresh_token,'device-id':device_id,'country-id':COUNTRY_ID,'platform':PLATFORM,'Connection':'keep-alive','access-token':access_token,'app-version':APP_VERSION_ORDER,'os-version':OS_VERSION,'Content-Type':'application/json','device-model':DEVICE_MODEL,'timezone':'+07:00'}
            resp=await client.get('https://api.fore.coffee/order/ongoing/all',headers=headers);resp.raise_for_status()
            return{"success":True,"data":resp.json().get('payload',[])}
        except Exception as e:logger.error(f"Gagal api_get_ongoing_orders: {e}");return{"success":False,"message":str(e)}

async def api_get_order_detail(access_token: str, refresh_token: str, device_id: str, uor_id: str) -> dict:
    async with httpx.AsyncClient() as client:
        try:
            headers={'Host':'api.fore.coffee','language':'id','User-Agent':USER_AGENT_ORDER,'push-token':PUSH_TOKEN_ORDER,'secret-key':SECRET_KEY_ORDER,'refresh-token':refresh_token,'device-id':device_id,'country-id':COUNTRY_ID,'platform':PLATFORM,'Connection':'keep-alive','access-token':access_token,'app-version':APP_VERSION_ORDER,'os-version':OS_VERSION,'Content-Type':'application/json','device-model':DEVICE_MODEL,'timezone':'+07:00'}
            url=f'https://api.fore.coffee/order/{uor_id}'
            resp=await client.get(url,headers=headers);resp.raise_for_status()
            return{"success":True,"data":resp.json().get('payload',{})}
        except Exception as e:logger.error(f"Gagal api_get_order_detail: {e}");return{"success":False,"message":str(e)}

async def api_logout(access_token: str, refresh_token: str, device_id: str):
    headers = {
        'language': LANGUAGE, 'User-Agent': USER_AGENT_ORDER, 'push-token': PUSH_TOKEN_ORDER,
        'secret-key': SECRET_KEY_ORDER, 'refresh-token': refresh_token, 'device-id': device_id,
        'country-id': COUNTRY_ID, 'platform': PLATFORM, 'access-token': access_token,
        'app-version': APP_VERSION_ORDER, 'os-version': OS_VERSION, 'Content-Type': 'application/json',
        'device-model': DEVICE_MODEL, 'timezone': '+07:00'
    }
    url = 'https://api.fore.coffee/auth/logout'
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=headers, json={})
            resp.raise_for_status()
        logger.info(f"Berhasil auto-logout dari sesi device {device_id}")
    except Exception as e:
        logger.warning(f"Gagal melakukan auto-logout: {e}")

# --- 6. Perintah Bot (Handlers Async) ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    try: register_user(user.id, user.first_name)
    except Exception as e: logger.error(f"Gagal mendaftarkan user {user.id}: {e}")
    if is_admin_check(user.id): await update.message.reply_text(f"Selamat datang, Admin {user.first_name}!", reply_markup=admin_reply_markup)
    else: await update.message.reply_text(f"Selamat datang, {user.first_name}!", reply_markup=user_reply_markup)

async def check_credits_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id; user_name = update.effective_user.first_name
    user_data = get_user(user_id)
    if not user_data and user_id == ADMIN_ID: register_user(user_id, user_name); user_data = get_user(user_id)
    elif not user_data: await update.message.reply_text("Anda belum terdaftar. /start dulu."); return
    sisa_kredit_display = "∞ (Admin)" if is_admin_check(user_id) else user_data['credits']
    await update.message.reply_text(f"👤 **Nama:** {user_data['user_name']}\n🆔 **User ID:** `{user_id}`\n💳 **Sisa kredit:** {sisa_kredit_display}", parse_mode='Markdown')

@check_access(permission_required="can_cek_akun", credit_cost=0)
async def cek_akun(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_data = get_user(user_id)
    if not user_data and user_id == ADMIN_ID: register_user(user_id, update.effective_user.first_name); user_data = get_user(user_id)
    sisa_kredit_display = "∞ (Admin)" if is_admin_check(user_id) else user_data['credits']
    await update.message.reply_text(f"👤 **Nama:** {user_data['user_name']}\n🆔 **User ID:** `{user_id}`\n💳 **Sisa kredit:** {sisa_kredit_display}", parse_mode='Markdown', reply_markup=update.message.reply_markup)

# --- 7. Alur Percakapan (User) "Cek Akun Fore" ---
@check_access(permission_required="can_cek_fore", credit_cost=1)
async def start_fore_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Izin OK. Masukkan Nomor HP.\n\n/cancel batal.", reply_markup=ReplyKeyboardRemove()); return ASK_FORE_PHONE

async def receive_fore_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone_input = update.message.text
    phone_cleaned = re.sub(r"[^0-9]", "", phone_input)
    
    # --- VALIDASI INPUT HP ---
    if not phone_cleaned or len(phone_cleaned) < 7:
        await update.message.reply_text("Format nomor HP tidak valid. Harap masukkan nomor HP yang benar (hanya angka). Coba lagi:")
        return ASK_FORE_PHONE # Tetap di state ini
    # --- AKHIR VALIDASI ---

    if phone_cleaned.startswith("62"): phone_root = phone_cleaned[2:]
    elif phone_cleaned.startswith("0"): phone_root = phone_cleaned[1:]
    else: phone_root = phone_cleaned
    
    context.user_data["phone_root"] = phone_root
    await update.message.reply_text("HP OK. Masukkan 6 digit PIN:"); return ASK_FORE_PIN

async def receive_fore_pin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    pin_input = update.message.text; phone = context.user_data.get("phone_root"); user_id = update.effective_user.id
    
    # --- VALIDASI INPUT PIN ---
    if not (pin_input.isdigit() and len(pin_input) == 6): 
        await update.message.reply_text("PIN salah format. Harap masukkan 6 digit angka. Coba lagi:")
        return ASK_FORE_PIN # Tetap di state ini
    # --- AKHIR VALIDASI ---

    await update.message.reply_text("PIN OK. Memproses Akun Fore...")
    data = await process_fore_check(phone, pin_input)
    markup = admin_reply_markup if is_admin_check(user_id) else user_reply_markup
    if data.get("success"):
        try:
            if not is_admin_check(user_id): update_credits(user_id, -1); logger.info(f"1 kredit dipotong (Cek Akun Fore)")
        except Exception as e: logger.error(f"Gagal potong kredit {user_id}: {e}")
        nama=data.get("nama","N/A"); reff=data.get("reff","N/A"); total_poin=data.get("total_points",0); voucher_list=data.get("vouchers",[])
        voucher_display = "Tidak ada voucher." if not voucher_list else "\n\n".join([f"  • *{v['name']}*\n    (Exp: `{v['end']}`)" for v in voucher_list])
        hasil_teks = (f"☕️ **Hasil Cek Akun Fore** ☕️\n\n👤 Nama: `{nama}`\n🎟️ Reff: `{reff}`\n✨ Poin: `{total_poin}`\n\n---\n🏷️ **Voucher**\n---\n{voucher_display}")
        await update.message.reply_text(hasil_teks, parse_mode='Markdown', reply_markup=markup)
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
    await update.message.reply_text("Izin OK. Masukkan Nomor HP.\n\n/cancel batal.", reply_markup=ReplyKeyboardRemove()); return ASK_ORDER_PHONE

async def receive_order_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone_input = update.message.text
    phone_cleaned = re.sub(r"[^0-9]", "", phone_input)
    
    # --- VALIDASI INPUT HP ---
    if not phone_cleaned or len(phone_cleaned) < 7:
        await update.message.reply_text("Format nomor HP tidak valid. Harap masukkan nomor HP yang benar (hanya angka). Coba lagi:")
        return ASK_ORDER_PHONE # Tetap di state ini
    # --- AKHIR VALIDASI ---

    if phone_cleaned.startswith("62"): phone_root = phone_cleaned[2:]
    elif phone_cleaned.startswith("0"): phone_root = phone_cleaned[1:]
    else: phone_root = phone_cleaned
    
    context.user_data["phone_root"] = phone_root
    await update.message.reply_text("HP OK. Masukkan PIN:"); return ASK_ORDER_PIN

async def receive_order_pin_and_get_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    pin_input = update.message.text; phone = context.user_data.get("phone_root"); user_id = update.effective_user.id
    markup = admin_reply_markup if is_admin_check(user_id) else user_reply_markup

    # --- VALIDASI INPUT PIN ---
    if not (pin_input.isdigit() and len(pin_input) == 6): 
        await update.message.reply_text("PIN salah format. Harap masukkan 6 digit angka. Coba lagi:")
        return ASK_ORDER_PIN # Tetap di state ini
    # --- AKHIR VALIDASI ---

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

    if len(ongoing_orders) > 1:
        summary_lines = ["🔍 **Ditemukan Beberapa Orderan** 🔍"]
        for order in ongoing_orders:
            summary_lines.append(
                f"\nID Orderan: `{order.get('uor_id', 'N/A')}`\n"
                f"Nomor Antrian: `{order.get('uor_queue', 'N/A')}`\n"
                f"Outlet: `{order.get('store', {}).get('sto_name', 'N/A')}`"
            )
        await update.message.reply_text("\n".join(summary_lines), parse_mode='Markdown')
    
    keyboard = [[InlineKeyboardButton(order.get('store',{}).get('sto_name', f"Order {order.get('uor_id')}"), callback_data=f"order_select_{order.get('uor_id')}")] for order in ongoing_orders]
    keyboard.append([InlineKeyboardButton("Batalkan", callback_data="order_cancel")])
    await update.message.reply_text("Pilih orderan di bawah untuk detail & QR Code:", reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECTING_ORDER

async def _send_formatted_order_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict, user_id: int):
    """Fungsi helper untuk memformat, mengirim detail, dan mengirim tombol Aksi."""
    try:
        # 1. Format Pesan Teks
        status = "Dalam Pembuatan 👨‍🍳" if data.get('uor_status') == 'in_process' else "Ready For PickUp 픽업 준비 완료 🥤"
        antrian = data.get('uor_queue', 'N/A')
        receipt_url = data.get('url_webview_e_receipt', 'N/A')
        qr_hash = data.get('uorsh_hash')
        
        # Perbaikan berdasarkan JSON
        nama_user = data.get('user_name', 'N/A') 
        nama_outlet = data.get('st_name', 'N/A') 
        kode_outlet = data.get('address', {}).get('st_code', 'N/A')
        pesan_custom = data.get('estimated_time_seconds', {}).get('title_message') or "Tidak ada pesan" 

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
            "✅ **Detail Order** ✅\n",
            f"Nama: `{nama_user}`",
            f"Outlet: `{nama_outlet} ({kode_outlet})`",
            f"No Antrian: **{antrian}**",
            f"Status: **{status}**\n",
            "--- **Orderan** ---",
            daftar_orderan,
            "\n--- **Pesan** ---",
            f"`{pesan_custom}`\n",
            f"E-Receipt: [Klik di sini]({receipt_url})"
        ]
        hasil_teks = "\n".join(hasil_teks_list)

        # 2. Kirim Pesan (Foto atau Teks)
        if not qr_hash: 
            await context.bot.send_message(chat_id=user_id, text=hasil_teks, parse_mode='Markdown', disable_web_page_preview=True)
        else:
            try:
                qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=4)
                qr.add_data(qr_hash); qr.make(fit=True)
                img = qr.make_image(fill_color="black", back_color="white").convert('RGB')
                logo = Image.open('logo.png')
                qr_width, qr_height = img.size; logo_max_size = qr_height // 5
                logo.thumbnail((logo_max_size, logo_max_size), Image.Resampling.LANCZOS)
                pos = ((qr_width - logo.width) // 2, (qr_height - logo.height) // 2)
                if logo.mode == 'RGBA': img.paste(logo, pos, mask=logo)
                else: img.paste(logo, pos)
                bio = io.BytesIO(); img.save(bio, 'PNG'); bio.seek(0)
                await context.bot.send_photo(chat_id=user_id, photo=bio, caption=hasil_teks, parse_mode='Markdown')
            except FileNotFoundError:
                logger.warning("logo.png tidak ditemukan. Mengirim QR standar.")
                qr_fallback = qrcode.QRCode(version=1, box_size=10, border=4); qr_fallback.add_data(qr_hash); qr_fallback.make(fit=True)
                img_fallback = qr_fallback.make_image(fill_color="black", back_color="white"); bio = io.BytesIO(); img_fallback.save(bio, 'PNG'); bio.seek(0)
                await context.bot.send_photo(chat_id=user_id, photo=bio, caption=hasil_teks, parse_mode='Markdown')
            except Exception as e:
                logger.error(f"Gagal buat QR kustom: {e}. Mengirim QR standar.")
                qr_fallback = qrcode.QRCode(version=1, box_size=10, border=4); qr_fallback.add_data(qr_hash); qr_fallback.make(fit=True)
                img_fallback = qr_fallback.make_image(fill_color="black", back_color="white"); bio = io.BytesIO(); img_fallback.save(bio, 'PNG'); bio.seek(0)
                await context.bot.send_photo(chat_id=user_id, photo=bio, caption=hasil_teks, parse_mode='Markdown')

        # 3. Kirim Tombol Aksi
        refresh_keyboard = [[
            InlineKeyboardButton("🔄 Refresh", callback_data="order_action_refresh"),
            InlineKeyboardButton("✅ Selesai", callback_data="order_action_finish")
        ]]
        refresh_markup = InlineKeyboardMarkup(refresh_keyboard)
        await context.bot.send_message(chat_id=user_id, text="Refresh status atau selesaikan sesi?", reply_markup=refresh_markup)

    except Exception as e:
        logger.error(f"Gagal memformat/mengirim detail order: {e}")
        await context.bot.send_message(chat_id=user_id, text=f"Terjadi error saat menampilkan data: {e}")

async def select_order_and_show_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = await update.callback_query.answer(); query_data = update.callback_query.data; user_id = update.effective_user.id
    markup = admin_reply_markup if is_admin_check(user_id) else user_reply_markup
    
    access_token=context.user_data.get('access_token'); refresh_token=context.user_data.get('refresh_token'); device_id=context.user_data.get('device_id')
    
    if query_data == "order_cancel":
        await update.callback_query.edit_message_text("Cek order dibatalkan.")
        if access_token and refresh_token and device_id:
            await api_logout(access_token, refresh_token, device_id)
        await context.bot.send_message(chat_id=user_id, text="Pilih aksi.", reply_markup=markup); context.user_data.clear(); return ConversationHandler.END
    
    try: uor_id = query_data.split("_")[-1]
    except Exception: 
        await update.callback_query.edit_message_text("Data salah. Dibatalkan.", reply_markup=markup)
        if access_token and refresh_token and device_id: await api_logout(access_token, refresh_token, device_id)
        context.user_data.clear(); return ConversationHandler.END

    if not all([access_token, refresh_token, device_id]): 
        await update.callback_query.edit_message_text("Sesi habis. Ulangi.", reply_markup=markup); context.user_data.clear(); return ConversationHandler.END
    
    await update.callback_query.edit_message_text(f"Ambil detail order {uor_id}...")
    
    context.user_data['uor_id'] = uor_id # Simpan uor_id untuk refresh
    
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

    cost = 5
    if not is_admin_check(user_id):
        try: 
            if context.user_data.get('credit_deducted_for_' + uor_id) is not True:
                update_credits(user_id, -cost); 
                context.user_data['credit_deducted_for_' + uor_id] = True # Tandai
                logger.info(f"{cost} kredit dipotong (Cek Order {uor_id})")
        except Exception as e: 
            logger.error(f"Gagal potong kredit {user_id}: {e}"); await context.bot.send_message(chat_id=user_id, text="Error potong kredit.", reply_markup=markup)
            if access_token and refresh_token and device_id: await api_logout(access_token, refresh_token, device_id)
            context.user_data.clear(); return ConversationHandler.END

    await _send_formatted_order_detail(update, context, data, user_id)
    return AWAITING_REFRESH # Pindah ke state refresh

async def handle_refresh_or_finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = await update.callback_query.answer(); query_data = update.callback_query.data
    user_id = update.effective_user.id
    
    access_token=context.user_data.get('access_token')
    refresh_token=context.user_data.get('refresh_token')
    device_id=context.user_data.get('device_id')
    uor_id=context.user_data.get('uor_id')
    markup = admin_reply_markup if is_admin_check(user_id) else user_reply_markup

    if not all([access_token, refresh_token, device_id, uor_id]):
        await update.callback_query.edit_message_text("Sesi Anda telah berakhir. Silakan ulangi.")
        context.user_data.clear(); return ConversationHandler.END

    if query_data == "order_action_finish":
        await update.callback_query.edit_message_text("✅ Selesai. Menutup sesi...")
        await api_logout(access_token, refresh_token, device_id)
        await context.bot.send_message(chat_id=user_id, text="Sesi ditutup. Pilih aksi.", reply_markup=markup)
        context.user_data.clear()
        return ConversationHandler.END

    if query_data == "order_action_refresh":
        await update.callback_query.edit_message_text("🔄 Meresfresh status order...")
        
        detail_result = await api_get_order_detail(access_token, refresh_token, device_id, uor_id)
        
        if not detail_result.get("success") or not detail_result.get("data"):
            await context.bot.send_message(chat_id=user_id, text=f"Gagal refresh data: {detail_result.get('message', 'Data kosong')}. Sesi ditutup.", reply_markup=markup)
            await api_logout(access_token, refresh_token, device_id)
            context.user_data.clear()
            return ConversationHandler.END
        
        data = detail_result.get("data", {})
        await _send_formatted_order_detail(update, context, data, user_id)
        return AWAITING_REFRESH # Tetap di state ini

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
    query = await update.callback_query.answer(); query_data = update.callback_query.data
    if query_data == "admin_nop": return SELECTING_USER
    if query_data == "admin_cancel": await update.callback_query.edit_message_text("Aksi dibatalkan."); return ConversationHandler.END
    try: _, action, command, value = query_data.split("_")
    except ValueError: logger.warning(f"Callback data tidak valid: {query_data}"); return SELECTING_USER
    context.user_data['admin_action'] = action
    if command == "page":
        page = int(value); users, total_pages = get_users_paginated(page=page, per_page=USERS_PER_PAGE, exclude_admin_id=ADMIN_ID)
        keyboard = build_user_list_keyboard(users, total_pages, page, action)
        await update.callback_query.edit_message_text(f"Pilih user:\n(Hal {page}/{total_pages})", reply_markup=keyboard)
        return SELECTING_USER
    if command == "select":
        target_user_id = int(value); target_user = get_user(target_user_id)
        if not target_user: await update.callback_query.edit_message_text("User tidak ditemukan.", reply_markup=None); return ConversationHandler.END
        context.user_data['target_user_id'] = target_user_id; context.user_data['target_user_name'] = target_user['user_name']
        await update.callback_query.delete_message()
        if action == "credit":
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Target: {target_user['user_name']}\nJumlah kredit (misal: `100` atau `-10`):", parse_mode='Markdown')
            return ASKING_CREDIT_AMOUNT
        if action in ("grant", "revoke"):
            perm_keyboard = [[InlineKeyboardButton(p, callback_data=f"admin_perm_{p}")] for p in PERMISSION_NAMES]
            perm_keyboard.append([InlineKeyboardButton("Batalkan", callback_data="admin_cancel")])
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Target: {target_user['user_name']}\nPilih izin:", reply_markup=InlineKeyboardMarkup(perm_keyboard))
            return ASKING_PERMISSION_TYPE
    return ConversationHandler.END
async def receive_credit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try: amount = int(update.message.text)
    except ValueError: await update.message.reply_text("Jumlah harus angka. Coba lagi:"); return ASKING_CREDIT_AMOUNT
    target_user_id = context.user_data['target_user_id']; target_user_name = context.user_data['target_user_name']
    try:
        update_credits(target_user_id, amount); new_data = get_user(target_user_id)
        await update.message.reply_text(f"✅ Berhasil!\nUser: {target_user_name}\nKredit sekarang: **{new_data['credits']}**", parse_mode='Markdown', reply_markup=admin_reply_markup)
    except Exception as e: await update.message.reply_text(f"Gagal update DB: {e}", reply_markup=admin_reply_markup)
    context.user_data.clear(); return ConversationHandler.END
async def receive_permission_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = await update.callback_query.answer(); query_data = update.callback_query.data
    if query_data == "admin_cancel":
        await update.callback_query.edit_message_text("Aksi dibatalkan.")
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Pilih aksi.", reply_markup=admin_reply_markup)
        context.user_data.clear(); return ConversationHandler.END
    parts = query_data.split("_"); permission_name = "_".join(parts[2:])
    if permission_name not in PERMISSION_NAMES:
        await update.callback_query.edit_message_text("Izin tidak valid. Dibatalkan.", reply_markup=None)
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Pilih aksi.", reply_markup=admin_reply_markup)
        context.user_data.clear(); return ConversationHandler.END
    target_user_id = context.user_data['target_user_id']; target_user_name = context.user_data['target_user_name']
    action = context.user_data['admin_action']; action_value = 1 if action == 'grant' else 0
    action_text = "DIBERIKAN" if action == 'grant' else "DICABUT"
    try:
        set_permission(target_user_id, permission_name, action_value)
        await update.callback_query.edit_message_text(f"✅ Berhasil!\nUser: {target_user_name}\nIzin '{permission_name}' **{action_text}**.", parse_mode='Markdown')
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Pilih aksi.", reply_markup=admin_reply_markup)
    except Exception as e:
        await update.callback_query.edit_message_text(f"Gagal update DB: {e}", reply_markup=None)
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Pilih aksi.", reply_markup=admin_reply_markup)
    context.user_data.clear(); return ConversationHandler.END
async def cancel_admin_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer(); await update.callback_query.edit_message_text("Aksi dibatalkan.")
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Menu Admin", reply_markup=admin_reply_markup)
    else: await update.message.reply_text("Aksi admin dibatalkan.", reply_markup=admin_reply_markup)
    context.user_data.clear(); return ConversationHandler.END

# --- 10. Fungsi Utama (Main) ---
def main() -> None:
    if not TOKEN: logger.critical("Error: TELEGRAM_TOKEN tidak ditemukan"); return
    application = ApplicationBuilder().token(TOKEN).build()
    fore_conv_handler = ConversationHandler(entry_points=[MessageHandler(filters.TEXT & filters.Regex("^Cek Akun Fore$"), start_fore_check)], states={ASK_FORE_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_fore_phone)], ASK_FORE_PIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_fore_pin)],}, fallbacks=[CommandHandler("cancel", cancel_fore_check)], per_user=True, per_message=False)
    admin_conv_handler = ConversationHandler(entry_points=[MessageHandler(filters.TEXT & (filters.Regex("^Tambah Kredit$") | filters.Regex("^Beri Izin$") | filters.Regex("^Cabut Izin$")), start_admin_flow)], states={SELECTING_USER: [CallbackQueryHandler(admin_user_list_callback, pattern="^admin_")], ASKING_CREDIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_credit_amount)], ASKING_PERMISSION_TYPE: [CallbackQueryHandler(receive_permission_type, pattern="^admin_")],}, fallbacks=[CommandHandler("cancel", cancel_admin_flow), CallbackQueryHandler(cancel_admin_flow, pattern="^admin_cancel$")], per_user=True, per_message=False)
    
    order_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("^Cek Orderan$"), start_order_check)], 
        states={
            ASK_ORDER_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_order_phone)], 
            ASK_ORDER_PIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_order_pin_and_get_list)], 
            SELECTING_ORDER: [CallbackQueryHandler(select_order_and_show_detail, pattern="^order_")],
            AWAITING_REFRESH: [CallbackQueryHandler(handle_refresh_or_finish, pattern="^order_action_")]
        }, 
        fallbacks=[CommandHandler("cancel", cancel_order_check)], 
        per_user=True, 
        per_message=False
    )

    application.add_handler(fore_conv_handler); application.add_handler(admin_conv_handler); application.add_handler(order_conv_handler)
    application.add_handler(CommandHandler("start", start)); application.add_handler(CommandHandler("credit", check_credits_command)); application.add_handler(MessageHandler(filters.TEXT & filters.Regex("^Cek Akun$"), cek_akun))
    logger.info("Bot berjalan...")
    application.run_polling()

if __name__ == '__main__':
    main()