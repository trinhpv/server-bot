import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton
import subprocess
import socket
import threading
import time
import logging
from logging.handlers import RotatingFileHandler
import sys
import psutil
import datetime
import platform
import os

# ================= LẤY THÔNG SỐ BẢO MẬT =================
try:
    from config import BOT_TOKEN, MY_CHAT_ID, PC_MAC, PC_IP
except ImportError:
    print("LỖI: Không tìm thấy file config.py!")
    sys.exit(1)

# ================= CẤU HÌNH LOGGING =================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        RotatingFileHandler("bot_activity.log", maxBytes=5*1024*1024, backupCount=2, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

metrics_logger = logging.getLogger('metrics')
metrics_logger.setLevel(logging.INFO)
metrics_handler = RotatingFileHandler("system_metrics.txt", maxBytes=5*1024*1024, backupCount=2, encoding='utf-8')
metrics_handler.setFormatter(logging.Formatter('%(asctime)s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
metrics_logger.addHandler(metrics_handler)

# KHỞI TẠO BOT
bot = telebot.TeleBot(BOT_TOKEN)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ================= MENU =================
def main_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    btn_wake = KeyboardButton("⚡ Bật máy tính")
    btn_status = KeyboardButton("🖥️ Trạng thái PC")
    btn_server_full = KeyboardButton("📊 Thông tin Máy chủ")
    btn_update = KeyboardButton("🔄 Cập nhật Code")
    markup.add(btn_wake, btn_status, btn_server_full, btn_update)
    return markup

# ================= LUỒNG CHẠY NGẦM =================
def log_system_metrics_background():
    logger.info("Đã chạy luồng ghi system_metrics.txt")
    while True:
        try:
            with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
                cpu_temp = round(int(f.read().strip()) / 1000, 1)
            with open("/sys/class/power_supply/battery/temp", "r") as f:
                bat_temp = round(int(f.read().strip()) / 10, 1)
            with open("/sys/class/power_supply/battery/capacity", "r") as f:
                bat_pct = f.read().strip()
            with open("/sys/class/power_supply/battery/status", "r") as f:
                bat_status = f.read().strip()
            
            cpu_pct = psutil.cpu_percent(interval=1)
            ram_pct = psutil.virtual_memory().percent
            metrics_logger.info(f"CPU: {cpu_pct}% ({cpu_temp}°C) | RAM: {ram_pct}% | PIN: {bat_pct}% ({bat_status}, {bat_temp}°C)")
        except Exception:
            pass
        time.sleep(300)

def monitor_server_background():
    last_power_status = None
    cpu_warning_sent = False
    while True:
        try:
            with open("/sys/class/power_supply/battery/status", "r") as f:
                current_status = f.read().strip()
            
            if last_power_status is not None and current_status != last_power_status:
                if current_status == "Discharging":
                    bot.send_message(MY_CHAT_ID, "⚠️ **CẢNH BÁO MẤT ĐIỆN:**\nĐang chạy bằng pin dự trữ.", parse_mode="Markdown")
                elif current_status == "Charging" and last_power_status == "Discharging":
                    bot.send_message(MY_CHAT_ID, "🔌 **CÓ ĐIỆN TRỞ LẠI:**\nĐã nhận lại nguồn sạc.", parse_mode="Markdown")
            last_power_status = current_status

            with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
                cpu_temp_c = round(int(f.read().strip()) / 1000, 1)
            
            if cpu_temp_c > 50.0 and not cpu_warning_sent:
                bot.send_message(MY_CHAT_ID, f"🔥 **BÁO ĐỘNG QUÁ NHIỆT:** CPU đạt **{cpu_temp_c}°C**!", parse_mode="Markdown")
                cpu_warning_sent = True
            elif cpu_temp_c < 45.0 and cpu_warning_sent:
                bot.send_message(MY_CHAT_ID, f"❄️ **Nhiệt độ đã ổn định:** {cpu_temp_c}°C.", parse_mode="Markdown")
                cpu_warning_sent = False
        except Exception:
            pass
        time.sleep(60)

# ================= XỬ LÝ LỆNH =================
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if message.chat.id == MY_CHAT_ID:
        bot.send_message(message.chat.id, "Bảng điều khiển đã sẵn sàng!", reply_markup=main_menu())

@bot.message_handler(func=lambda message: True)
def handle_buttons(message):
    if message.chat.id != MY_CHAT_ID:
        return
    text = message.text

    if text == "⚡ Bật máy tính":
        bot.send_message(message.chat.id, "⏳ Đang kiểm tra...")
        if subprocess.run(["ping", "-c", "1", "-W", "2", PC_IP], stdout=subprocess.DEVNULL).returncode == 0:
            bot.send_message(message.chat.id, "🟢 Máy tính **ĐÃ BẬT SẴN**.", parse_mode="Markdown")
        else:
            subprocess.run(["wakeonlan", PC_MAC])
            bot.send_message(message.chat.id, "✅ Đã gửi gói tin kích nguồn!", parse_mode="Markdown")

    elif text == "🖥️ Trạng thái PC":
        if subprocess.run(["ping", "-c", "1", "-W", "2", PC_IP], stdout=subprocess.DEVNULL).returncode == 0:
            bot.send_message(message.chat.id, "🟢 PC đang **ONLINE**!", parse_mode="Markdown")
        else:
            bot.send_message(message.chat.id, "🔴 PC đang **TẮT**.", parse_mode="Markdown")

    elif text == "📊 Thông tin Máy chủ":
        try:
            bot.send_message(message.chat.id, "⏳ Đang thu thập dữ liệu...")
            uptime_str = str(datetime.timedelta(seconds=int(time.time() - psutil.boot_time())))
            os_info = f"{platform.system()} {platform.release()}"
            cpu_usage = psutil.cpu_percent(interval=1)
            
            try:
                with open("/sys/class/thermal/thermal_zone0/temp", "r") as f: cpu_temp = f"{round(int(f.read().strip())/1000,1)}°C"
            except: cpu_temp = "N/A"
            
            ram = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
            
            try:
                with open("/sys/class/power_supply/battery/capacity", "r") as f: bat_pct = f.read().strip()
                with open("/sys/class/power_supply/battery/status", "r") as f: bat_st = f.read().strip()
                with open("/sys/class/power_supply/battery/temp", "r") as f: bat_temp = f"{round(int(f.read().strip())/10,1)}°C"
                bat_st_vn = "Đang sạc ⚡" if bat_st == "Charging" else "Dùng pin ⚠️" if bat_st == "Discharging" else "Sạc đầy 🔌" if bat_st == "Full" else bat_st
            except:
                bat_pct, bat_st_vn, bat_temp = "N/A", "N/A", "N/A"

            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try: s.connect(('10.255.255.255', 1)); local_ip = s.getsockname()[0]
            except: local_ip = '127.0.0.1'
            finally: s.close()
                
            net_io = psutil.net_io_counters()
            
            msg = f"""📱 **THÔNG SỐ SERVER TOÀN DIỆN** 📱\n\n⏱ **Hệ thống**\n• HĐH: `{os_info}`\n• Uptime: `{uptime_str}`\n\n🧠 **CPU & RAM**\n• CPU: `{cpu_usage}%` | Nhiệt độ: `{cpu_temp}`\n• RAM: `{round(ram.used/(1024**3),2)}GB / {round(ram.total/(1024**3),2)}GB` ({ram.percent}%)\n\n💾 **Lưu trữ**\n• Đã dùng: `{round(disk.used/(1024**3),2)}GB / {round(disk.total/(1024**3),2)}GB` ({disk.percent}%)\n\n🔋 **Pin & Nguồn**\n• Dung lượng: `{bat_pct}%` | Nhiệt độ: `{bat_temp}`\n• Trạng thái: {bat_st_vn}\n\n🌐 **Mạng**\n• IP Local: `{local_ip}`\n• Data Nhận: `{round(net_io.bytes_recv/(1024**2),2)} MB`"""
            bot.send_message(message.chat.id, msg, parse_mode="Markdown")
        except Exception as e:
            bot.send_message(message.chat.id, f"⚠️ Lỗi: `{e}`", parse_mode="Markdown")

   # ----- 4. LỆNH CẬP NHẬT CODE TỪ GITHUB -----
    elif text == "🔄 Cập nhật Code":
        try:
            bot.send_message(message.chat.id, "⏳ Đang tải code mới từ GitHub...")
            
            subprocess.run(["sudo", "git", "config", "--global", "--add", "safe.directory", BASE_DIR])
            
            # ĐÃ SỬA: Dùng cú pháp tương thích với Python 3.6
            res = subprocess.run(["sudo", "git", "pull"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, cwd=BASE_DIR)
            
            bot.send_message(message.chat.id, f"Trạng thái Git:\n`{res.stdout}`\n\n🔄 Code mới đã tải xong! Bot sẽ tự khởi động lại trong 3 giây...", parse_mode="Markdown")
            
            def kill_bot():
                time.sleep(2) 
                os._exit(0)   
                
            threading.Thread(target=kill_bot, daemon=True).start()
            
        except Exception as e:
            bot.send_message(message.chat.id, f"⚠️ Lỗi cập nhật: {e}")

if __name__ == '__main__':
    logger.info("Đang khởi động Bot...")
    threading.Thread(target=log_system_metrics_background, daemon=True).start()
    threading.Thread(target=monitor_server_background, daemon=True).start()
    
    try:
        bot.send_message(MY_CHAT_ID, "🚀 **Hệ thống Server đã online!**", parse_mode="Markdown", reply_markup=main_menu())
    except Exception as e:
        logger.error(f"Lỗi gửi thông báo đầu: {e}")
    
    try:
        bot.infinity_polling(timeout=10, long_polling_timeout=5)
    except Exception as e:
        logger.error(f"Lỗi polling: {e}")