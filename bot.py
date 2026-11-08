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
import csv
import io
import matplotlib
matplotlib.use('Agg')  # Chế độ vẽ ngầm không cần màn hình (Headless)
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# ================= LẤY THÔNG SỐ BẢO MẬT =================
try:
    from config import BOT_TOKEN, MY_CHAT_ID, PC_MAC, PC_IP
except ImportError:
    print("LỖI: Không tìm thấy file config.py!")
    sys.exit(1)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "bot_activity.log")
METRICS_FILE = os.path.join(BASE_DIR, "system_metrics.csv")

# ================= CẤU HÌNH LOGGING =================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=2, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Cấu hình riêng cho Metrics (Chuẩn CSV)
metrics_logger = logging.getLogger('metrics')
metrics_logger.setLevel(logging.INFO)
metrics_logger.propagate = False  # Ngăn không cho đẩy log sang bot_activity.log

metrics_handler = RotatingFileHandler(METRICS_FILE, maxBytes=5*1024*1024, backupCount=2, encoding='utf-8')
metrics_handler.setFormatter(logging.Formatter('%(asctime)s,%(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
metrics_logger.addHandler(metrics_handler)

# Tạo dòng tiêu đề (Header) cho CSV nếu file chưa tồn tại hoặc đang trống
if not os.path.exists(METRICS_FILE) or os.path.getsize(METRICS_FILE) == 0:
    metrics_logger.info("cpu_pct,cpu_temp,ram_pct,disk_pct,bat_pct,bat_temp,bat_status")

# KHỞI TẠO BOT
bot = telebot.TeleBot(BOT_TOKEN)

# ================= MENU =================
def main_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    btn_wake = KeyboardButton("⚡ Bật máy tính")
    btn_status = KeyboardButton("🖥️ Trạng thái PC")
    btn_server_full = KeyboardButton("📊 Thông tin Máy chủ")
    btn_chart = KeyboardButton("📈 Biểu đồ 24h")
    btn_update = KeyboardButton("🔄 Cập nhật Code")
    markup.add(btn_wake, btn_status, btn_server_full, btn_chart, btn_update)
    return markup

# ================= KIỂM TRA TRẠNG THÁI PC ĐA TẦNG =================
def is_pc_online(ip, mac=None):
    """Kiểm tra trạng thái PC chuẩn xác trên môi trường Linux/Android + Windows"""
    # 1. Kiểm tra qua TCP Socket (Port 135 RPC, 445 SMB, 3389 RDP của Windows)
    # Kể cả bị từ chối kết nối (errno 111 - Connection refused) cũng chứng tỏ PC đang BẬT
    for port in (135, 445, 3389):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.0)
        try:
            result = s.connect_ex((ip, port))
            if result in (0, 111):
                return True
        except Exception:
            pass
        finally:
            s.close()

    # 2. Kiểm tra bằng sudo ping (tránh lỗi thiếu quyền raw socket trên Linux Deploy)
    try:
        res = subprocess.run(
            ["sudo", "ping", "-c", "1", "-W", "2", ip],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        if res.returncode == 0:
            return True
    except Exception:
        pass

    # 3. Quét bảng ARP theo địa chỉ MAC (Phát hiện được kể cả khi Windows chặn Ping & Port)
    if mac:
        try:
            # Gửi 1 gói UDP rỗng để kích thích cập nhật bảng ARP trước khi đọc
            udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp_sock.sendto(b"", (ip, 9))
            udp_sock.close()
            time.sleep(0.2)

            with open("/proc/net/arp", "r") as f:
                arp_table = f.read().lower()
                target_mac = mac.lower().replace("-", ":")
                for line in arp_table.splitlines():
                    # Cờ 0x2 trong /proc/net/arp nghĩa là thiết bị đang hiện diện (Complete)
                    if target_mac in line and "0x2" in line:
                        return True
        except Exception:
            pass

    return False

# ================= HÀM THU THẬP THÔNG SỐ HỆ THỐNG =================
def read_sys_file(path, scale=1.0, default=None):
    """Đọc file hệ thống an toàn, tự động chia tỉ lệ nếu là nhiệt độ"""
    try:
        with open(path, "r") as f:
            val = f.read().strip()
            return round(int(val) / scale, 1) if scale != 1.0 else val
    except Exception:
        return default

def get_system_metrics():
    """Thu thập toàn bộ thông số vào 1 dictionary duy nhất"""
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    return {
        "cpu_pct": psutil.cpu_percent(interval=1),
        "cpu_temp": read_sys_file("/sys/class/thermal/thermal_zone0/temp", scale=1000.0, default=-1.0),
        "ram_pct": ram.percent,
        "ram_used_gb": round(ram.used / (1024**3), 2),
        "ram_total_gb": round(ram.total / (1024**3), 2),
        "disk_pct": disk.percent,
        "disk_used_gb": round(disk.used / (1024**3), 2),
        "disk_total_gb": round(disk.total / (1024**3), 2),
        "bat_pct": read_sys_file("/sys/class/power_supply/battery/capacity", default="N/A"),
        "bat_temp": read_sys_file("/sys/class/power_supply/battery/temp", scale=10.0, default=-1.0),
        "bat_status": read_sys_file("/sys/class/power_supply/battery/status", default="Unknown")
    }

# ================= HÀM VẼ BIỂU ĐỒ 24H =================
def generate_24h_chart():
    """Đọc dữ liệu 24h qua từ CSV và trả về buffer ảnh PNG trong RAM"""
    cutoff_time = datetime.datetime.now() - datetime.timedelta(hours=24)
    timestamps = []
    cpu_pcts, ram_pcts = [], []
    cpu_temps, bat_temps, bat_pcts = [], [], []

    # Đọc cả file backup .1 (nếu vừa rotate) và file hiện tại theo thứ tự thời gian
    files_to_read = [f"{METRICS_FILE}.1", METRICS_FILE]

    for filepath in files_to_read:
        if not os.path.exists(filepath):
            continue
        with open(filepath, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            for row in reader:
                # Cấu trúc dòng: timestamp, cpu_pct, cpu_temp, ram_pct, disk_pct, bat_pct, bat_temp, bat_status
                if len(row) < 8 or row[1] == "cpu_pct":
                    continue
                try:
                    dt = datetime.datetime.strptime(row[0].strip(), "%Y-%m-%d %H:%M:%S")
                    if dt < cutoff_time:
                        continue

                    c_pct = float(row[1])
                    c_temp = float(row[2])
                    r_pct = float(row[3])
                    b_pct = float(row[5]) if row[5] != "N/A" else None
                    b_temp = float(row[6])

                    timestamps.append(dt)
                    cpu_pcts.append(c_pct)
                    ram_pcts.append(r_pct)
                    cpu_temps.append(c_temp if c_temp > 0 else None)
                    bat_temps.append(b_temp if b_temp > 0 else None)
                    bat_pcts.append(b_pct)
                except Exception:
                    continue

    if len(timestamps) < 2:
        return None

    # Khởi tạo khung biểu đồ (3 biểu đồ xếp dọc dùng chung trục thời gian)
    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(10, 8.5), sharex=True)
    fig.patch.set_facecolor('#1e1e2e')

    for ax in (ax1, ax2, ax3):
        ax.set_facecolor('#252538')
        ax.grid(True, linestyle='--', alpha=0.25, color='#ffffff')
        ax.tick_params(colors='#e0e0e0')
        for spine in ax.spines.values():
            spine.set_color('#444466')

    # --- Biểu đồ 1: Nhiệt độ CPU & Pin ---
    ax1.plot(timestamps, cpu_temps, color='#ff5555', linewidth=2, label='CPU Temp (°C)')
    ax1.plot(timestamps, bat_temps, color='#ffb86c', linewidth=1.5, linestyle='--', label='Battery Temp (°C)')
    ax1.axhline(y=50, color='#ff0000', linestyle=':', alpha=0.6, label='Ngưỡng cảnh báo (50°C)')
    ax1.set_ylabel('Nhiệt độ (°C)', color='#e0e0e0', fontweight='bold')
    ax1.set_title('THÔNG SỐ HỆ THỐNG SERVER (24 GIỜ QUA)', color='#ffffff', fontweight='bold', pad=12)
    ax1.legend(loc='upper left', facecolor='#252538', edgecolor='#444466', labelcolor='#ffffff')

    # --- Biểu đồ 2: Hiệu năng CPU (%) & RAM (%) ---
    ax2.plot(timestamps, cpu_pcts, color='#8be9fd', linewidth=1.8, label='CPU Usage (%)')
    ax2.plot(timestamps, ram_pcts, color='#bd93f9', linewidth=2, label='RAM Usage (%)')
    ax2.fill_between(timestamps, cpu_pcts, color='#8be9fd', alpha=0.1)
    ax2.set_ylabel('Tải (%)', color='#e0e0e0', fontweight='bold')
    ax2.set_ylim(0, 105)
    ax2.legend(loc='upper left', facecolor='#252538', edgecolor='#444466', labelcolor='#ffffff')

    # --- Biểu đồ 3: Phần trăm Pin ---
    ax3.plot(timestamps, bat_pcts, color='#50fa7b', linewidth=2, label='Dung lượng Pin (%)')
    valid_pcts = [p if p is not None else 0 for p in bat_pcts]
    ax3.fill_between(timestamps, valid_pcts, color='#50fa7b', alpha=0.15)
    ax3.set_ylabel('Pin (%)', color='#e0e0e0', fontweight='bold')
    ax3.set_ylim(0, 105)
    ax3.legend(loc='lower left', facecolor='#252538', edgecolor='#444466', labelcolor='#ffffff')

    # Định dạng trục thời gian Giờ:Phút + Ngày/Tháng
    ax3.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M\n%d/%m'))
    plt.xticks(rotation=0)
    plt.tight_layout()

    # Lưu ảnh vào bộ nhớ đệm RAM (BytesIO) thay vì ghi ra ổ cứng
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=130, facecolor=fig.get_facecolor())
    buf.seek(0)
    plt.close(fig)  # Giải phóng RAM ngay lập tức sau khi vẽ xong
    return buf

# ================= LUỒNG CHẠY NGẦM HỢP NHẤT =================
def unified_background_worker():
    logger.info("Đã khởi chạy luồng giám sát & ghi metrics hợp nhất (CSV).")
    last_power_status = None
    cpu_warning_sent = False
    cycle_count = 0

    while True:
        try:
            m = get_system_metrics()

            # 1. KIỂM TRA CẢNH BÁO MỖI 60 GIÂY
            current_status = m["bat_status"]
            if last_power_status is not None and current_status != last_power_status:
                if current_status == "Discharging":
                    bot.send_message(MY_CHAT_ID, "⚠️ **CẢNH BÁO MẤT ĐIỆN:**\nĐang chạy bằng pin dự trữ.", parse_mode="Markdown")
                elif current_status in ["Charging", "Full", "Not charging"] and last_power_status == "Discharging":
                    bot.send_message(MY_CHAT_ID, "🔌 **CÓ ĐIỆN TRỞ LẠI:**\nĐã nhận lại nguồn sạc.", parse_mode="Markdown")
            last_power_status = current_status

            cpu_temp_c = m["cpu_temp"]
            if cpu_temp_c > 50.0 and not cpu_warning_sent:
                bot.send_message(MY_CHAT_ID, f"🔥 **BÁO ĐỘNG QUÁ NHIỆT:** CPU đạt **{cpu_temp_c}°C**!", parse_mode="Markdown")
                cpu_warning_sent = True
            elif 0 < cpu_temp_c < 45.0 and cpu_warning_sent:
                bot.send_message(MY_CHAT_ID, f"❄️ **Nhiệt độ đã ổn định:** {cpu_temp_c}°C.", parse_mode="Markdown")
                cpu_warning_sent = False

            # 2. GHI LOG METRICS MỖI 5 PHÚT (5 chu kỳ x 60s)
            if cycle_count % 5 == 0:
                metrics_logger.info(
                    f"{m['cpu_pct']},{m['cpu_temp']},{m['ram_pct']},"
                    f"{m['disk_pct']},{m['bat_pct']},{m['bat_temp']},{m['bat_status']}"
                )

            cycle_count = (cycle_count + 1) % 10000

        except Exception as e:
            logger.error(f"Lỗi trong luồng giám sát ngầm: {e}")

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

    logger.info(f"Admin yêu cầu lệnh: '{text}'")
    
    # ----- 1. BẬT MÁY TÍNH -----
    if text == "⚡ Bật máy tính":
        bot.send_message(message.chat.id, "⏳ Đang kiểm tra...")
        if is_pc_online(PC_IP, PC_MAC):
            bot.send_message(message.chat.id, "🟢 Máy tính **ĐÃ BẬT SẴN**.", parse_mode="Markdown")
        else:
            subprocess.run(["wakeonlan", PC_MAC])
            bot.send_message(message.chat.id, "✅ Đã gửi gói tin kích nguồn!", parse_mode="Markdown")

    # ----- 2. TRẠNG THÁI PC -----
    elif text == "🖥️ Trạng thái PC":
        if is_pc_online(PC_IP, PC_MAC):
            bot.send_message(message.chat.id, "🟢 PC đang **ONLINE**!", parse_mode="Markdown")
        else:
            bot.send_message(message.chat.id, "🔴 PC đang **TẮT**.", parse_mode="Markdown")

    # ----- 3. THÔNG TIN MÁY CHỦ -----
    elif text == "📊 Thông tin Máy chủ":
        try:
            bot.send_message(message.chat.id, "⏳ Đang thu thập dữ liệu...")
            uptime_str = str(datetime.timedelta(seconds=int(time.time() - psutil.boot_time())))
            os_info = f"{platform.system()} {platform.release()}"
            
            m = get_system_metrics()
            cpu_temp_str = f"{m['cpu_temp']}°C" if m['cpu_temp'] != -1.0 else "N/A"
            bat_temp_str = f"{m['bat_temp']}°C" if m['bat_temp'] != -1.0 else "N/A"
            
            bat_st = m["bat_status"]
            if bat_st == "Charging":
                bat_st_vn = "Đang sạc ⚡"
            elif bat_st == "Discharging":
                bat_st_vn = "Dùng pin ⚠️"
            elif bat_st in ["Full", "Not charging"]:
                bat_st_vn = "Sạc đầy / Giữ nguồn 🔌"
            else:
                bat_st_vn = bat_st

            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(('10.255.255.255', 1))
                local_ip = s.getsockname()[0]
            except Exception:
                local_ip = '127.0.0.1'
            finally:
                s.close()

            net_io = psutil.net_io_counters()

            msg = (
                f"📱 **THÔNG SỐ SERVER TOÀN DIỆN** 📱\n\n"
                f"⏱ **Hệ thống**\n"
                f"• HĐH: `{os_info}`\n"
                f"• Uptime: `{uptime_str}`\n\n"
                f"🧠 **CPU & RAM**\n"
                f"• CPU: `{m['cpu_pct']}%` | Nhiệt độ: `{cpu_temp_str}`\n"
                f"• RAM: `{m['ram_used_gb']}GB / {m['ram_total_gb']}GB` ({m['ram_pct']}%)\n\n"
                f"💾 **Lưu trữ**\n"
                f"• Đã dùng: `{m['disk_used_gb']}GB / {m['disk_total_gb']}GB` ({m['disk_pct']}%)\n\n"
                f"🔋 **Pin & Nguồn**\n"
                f"• Dung lượng: `{m['bat_pct']}%` | Nhiệt độ: `{bat_temp_str}`\n"
                f"• Trạng thái: {bat_st_vn}\n\n"
                f"🌐 **Mạng**\n"
                f"• IP Local: `{local_ip}`\n"
                f"• Data Nhận: `{round(net_io.bytes_recv / (1024**2), 2)} MB`"
            )
            bot.send_message(message.chat.id, msg, parse_mode="Markdown")
        except Exception as e:
            bot.send_message(message.chat.id, f"⚠️ Lỗi: `{e}`", parse_mode="Markdown")

    # ----- 4. VẼ BIỂU ĐỒ 24H -----
    elif text == "📈 Biểu đồ 24h":
        wait_msg = bot.send_message(message.chat.id, "🎨 Đang trích xuất dữ liệu và vẽ biểu đồ...")
        try:
            chart_buf = generate_24h_chart()
            if chart_buf is None:
                bot.edit_message_text(
                    "⚠️ Chưa có đủ dữ liệu trong 24h qua (cần ít nhất 2 mốc ghi log, mỗi mốc cách nhau 5 phút).",
                    chat_id=message.chat.id,
                    message_id=wait_msg.message_id
                )
            else:
                bot.send_photo(
                    message.chat.id,
                    photo=chart_buf,
                    caption="📊 **Biểu đồ Nhiệt độ, Tải CPU/RAM & Pin trong 24 giờ qua**",
                    parse_mode="Markdown"
                )
                bot.delete_message(message.chat.id, wait_msg.message_id)
                chart_buf.close()
        except Exception as e:
            logger.error(f"Lỗi vẽ biểu đồ: {e}")
            bot.edit_message_text(
                f"⚠️ Lỗi khi tạo biểu đồ: `{e}`",
                chat_id=message.chat.id,
                message_id=wait_msg.message_id,
                parse_mode="Markdown"
            )

    # ----- 5. LỆNH CẬP NHẬT CODE TỪ GITHUB -----
    elif text == "🔄 Cập nhật Code":
        try:
            bot.send_message(message.chat.id, "⏳ Đang tải code mới từ GitHub...")

            subprocess.run(["sudo", "git", "config", "--global", "--add", "safe.directory", BASE_DIR])
            res = subprocess.run(
                ["sudo", "git", "pull"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                cwd=BASE_DIR
            )

            git_output = res.stdout.strip() or res.stderr.strip() or "Không có thay đổi."
            bot.send_message(
                message.chat.id,
                f"Trạng thái Git:\n`{git_output}`\n\n🔄 Code mới đã tải xong! Bot sẽ tự khởi động lại trong 3 giây...",
                parse_mode="Markdown"
            )

            def kill_bot():
                time.sleep(2)
                os._exit(0)

            threading.Thread(target=kill_bot, daemon=True).start()

        except Exception as e:
            bot.send_message(message.chat.id, f"⚠️ Lỗi cập nhật: {e}")

# ================= KHỞI CHẠY =================
if __name__ == '__main__':
    logger.info("Đang khởi động Bot...")
    threading.Thread(target=unified_background_worker, daemon=True).start()

    try:
        bot.send_message(
            MY_CHAT_ID,
            "🚀 **Hệ thống Server đã online!**",
            parse_mode="Markdown",
            reply_markup=main_menu()
        )
    except Exception as e:
        logger.error(f"Lỗi gửi thông báo đầu: {e}")

    while True:
        try:
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception as e:
            logger.error(f"Lỗi polling: {e}. Thử lại sau 5 giây...")
            time.sleep(5)