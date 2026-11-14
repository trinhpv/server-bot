import telebot
from telebot import apihelper
from telebot.types import ReplyKeyboardMarkup, KeyboardButton
import urllib3.connection
import subprocess
import socket
import threading
import time
import logging
from logging.handlers import RotatingFileHandler
from collections import deque
import html
import sys
import psutil
import datetime
import platform
import os
import csv
import io
import traceback
import matplotlib
matplotlib.use('Agg')  # Chế độ vẽ ngầm không cần màn hình (Headless)
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# ================= LẤY THÔNG SỐ BẢO MẬT =================
try:
    from config import BOT_TOKEN, MY_CHAT_ID, PC_MAC, PC_IP
    PC_IP = str(PC_IP).strip()
    PC_MAC = str(PC_MAC).strip()
except ImportError:
    print("LỖI: Không tìm thấy file config.py!")
    sys.exit(1)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "bot_activity.log")
METRICS_FILE = os.path.join(BASE_DIR, "system_metrics.csv")

# ================= CẤU HÌNH LOGGING CHI TIẾT =================
# Ghi rõ thêm tên luồng (threadName) để theo dõi chính xác WorkerThread nào đang xử lý
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s.%(msecs)03d | %(levelname)-7s | %(threadName)-14s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=2, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("ServerBot")

# Chặn hoàn toàn việc thư viện telebot tự xả Traceback dài dòng vào file log
telebot.logger.setLevel(logging.CRITICAL)

# ================= TỐI ƯU SOCKET & CHỐNG TREO KẾT NỐI SAU KHI NHÀN RỖI =================
def enable_tcp_keepalive():
    """Hạ thấp chu kỳ TCP Keep-Alive (10s) để giữ NAT Router và chip Wi-Fi luôn thức"""
    opts = list(urllib3.connection.HTTPConnection.default_socket_options)
    opts.append((socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1))
    if hasattr(socket, "TCP_KEEPIDLE"):
        opts.append((socket.SOL_TCP, socket.TCP_KEEPIDLE, 10))   # Bắn probe ngay sau 10s nhàn rỗi
    if hasattr(socket, "TCP_KEEPINTVL"):
        opts.append((socket.SOL_TCP, socket.TCP_KEEPINTVL, 5))   # Lặp lại mỗi 5s nếu chưa phản hồi
    if hasattr(socket, "TCP_KEEPCNT"):
        opts.append((socket.SOL_TCP, socket.TCP_KEEPCNT, 3))     # Đóng socket sau 3 lần thất bại
    urllib3.connection.HTTPConnection.default_socket_options = opts
    if hasattr(urllib3.connection, "HTTPSConnection"):
        urllib3.connection.HTTPSConnection.default_socket_options = opts

enable_tcp_keepalive()

# Cấu hình timeout cho TeleBot API (READ_TIMEOUT=30s lớn gấp đôi chu kỳ polling 15s)
apihelper.SESSION_TIME_TO_LIVE = 60
apihelper.CONNECT_TIMEOUT = 10
apihelper.READ_TIMEOUT = 30

# Bộ quản lý Session theo từng luồng (Thread-Local) & Đo độ trễ Telegram API
_thread_state = threading.local()
_original_make_request = apihelper._make_request
IDLE_SESSION_TTL = 45.0  # Tự động làm mới socket nếu luồng nghỉ quá 45 giây

def _reset_current_thread_session():
    """Đóng an toàn và ép tạo lại requests.Session của luồng hiện tại"""
    try:
        if hasattr(apihelper, '_get_req_session'):
            old_sess = apihelper._get_req_session(reset=False)
            if old_sess:
                old_sess.close()
            apihelper._get_req_session(reset=True)
    except Exception as e:
        logger.debug(f"Không thể đóng session cũ: {e}")

def _monitored_make_request(token, method_name, method='get', params=None, files=None, **kwargs):
    """Middleware bọc quanh mọi request tới Telegram: Chống kẹt socket + Ghi log thời gian phản hồi API"""
    now = time.time()
    last_used = getattr(_thread_state, 'last_used', 0.0)
    idle_sec = now - last_used if last_used > 0 else 0.0

    # Nếu luồng WorkerThread đã nghỉ quá 45s, chủ động hủy socket cũ để tránh lỗi kẹt lệnh đầu
    if method_name != 'getUpdates' and last_used > 0 and idle_sec > IDLE_SESSION_TTL:
        logger.info(f"[CONN] Luồng đã nghỉ {idle_sec:.1f}s (> {IDLE_SESSION_TTL}s) -> Làm mới phiên kết nối HTTP.")
        _reset_current_thread_session()

    t_start = time.perf_counter()
    try:
        result = _original_make_request(token, method_name, method=method, params=params, files=files, **kwargs)
        api_ms = (time.perf_counter() - t_start) * 1000.0
        _thread_state.last_used = time.time()

        # Lưu lại thời gian gọi API gần nhất của luồng này để cộng dồn/hiển thị
        _thread_state.last_api_ms = api_ms

        if method_name != 'getUpdates':
            logger.info(f"[API_OK] Method={method_name} | API_Latency={api_ms:.1f}ms")
        elif result:
            logger.info(f"[POLL_IN] Nhận {len(result)} sự kiện mới từ Telegram | Poll_Wait={api_ms:.1f}ms")
        return result

    except Exception as e:
        api_ms = (time.perf_counter() - t_start) * 1000.0
        _reset_current_thread_session()
        _thread_state.last_used = time.time()

        if method_name != 'getUpdates':
            logger.warning(f"[API_ERR] Method={method_name} lỗi sau {api_ms:.1f}ms ({type(e).__name__}: {e}) -> Đang thử lại tức thì...")
            # Tự động thử lại ngay 1 lần với Session mới tinh để không bị rớt lệnh của người dùng
            t_retry = time.perf_counter()
            result = _original_make_request(token, method_name, method=method, params=params, files=files, **kwargs)
            retry_ms = (time.perf_counter() - t_retry) * 1000.0
            _thread_state.last_api_ms = retry_ms
            logger.info(f"[API_RETRY_OK] Method={method_name} thành công ở lần thử 2 | Retry_Latency={retry_ms:.1f}ms")
            return result
        else:
            # Bắt gọn lỗi chập chờn mạng của getUpdates, chỉ ghi 1 dòng gọn gàng và trả về [] để PollingThread không bị văng Traceback
            logger.info(f"[POLL_REFRESH] Mạng chập chờn nhẹ sau {api_ms:.0f}ms ({type(e).__name__}) -> Đã làm mới kết nối chờ tin nhắn.")
            time.sleep(1)
            return []

# Kích hoạt Middleware giám sát API
apihelper._make_request = _monitored_make_request

# ================= CẤU HÌNH LOG METRICS (CSV) =================
metrics_logger = logging.getLogger('metrics')
metrics_logger.setLevel(logging.INFO)
metrics_logger.propagate = False

metrics_handler = RotatingFileHandler(METRICS_FILE, maxBytes=5*1024*1024, backupCount=2, encoding='utf-8')
metrics_handler.setFormatter(logging.Formatter('%(asctime)s,%(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
metrics_logger.addHandler(metrics_handler)

if not os.path.exists(METRICS_FILE) or os.path.getsize(METRICS_FILE) == 0:
    metrics_logger.info("cpu_pct,cpu_temp,ram_pct,disk_pct,bat_pct,bat_temp,bat_status")

# KHỞI TẠO BOT
bot = telebot.TeleBot(BOT_TOKEN, threaded=True, num_threads=4)
chart_lock = threading.Lock()

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

# ================= KIỂM TRA TRẠNG THÁI PC (ĐO THỜI GIAN PING) =================
def check_pc_online():
    """Kiểm tra ping tới PC và ghi log chi tiết thời gian phản hồi mạng LAN"""
    t0 = time.perf_counter()
    for attempt in range(1, 3):
        t_ping = time.perf_counter()
        # Giảm timeout mỗi lần ping xuống 1s (tối đa 2s cho 2 lần thử khi PC tắt)
        res = subprocess.run(
            ["ping", "-c", "1", "-W", "1", PC_IP],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        ping_ms = (time.perf_counter() - t_ping) * 1000.0
        if res.returncode == 0:
            logger.info(f"[LAN_PING] PC ({PC_IP}) ONLINE ở lần thử {attempt} | Ping_Time={ping_ms:.1f}ms")
            return True
        logger.debug(f"[LAN_PING] Lần thử {attempt} tới {PC_IP} thất bại ({ping_ms:.1f}ms)")

    total_ping_ms = (time.perf_counter() - t0) * 1000.0
    logger.info(f"[LAN_PING] PC ({PC_IP}) OFFLINE sau 2 lần thử | Total_Wait={total_ping_ms:.1f}ms")
    return False

# ================= HÀM THU THẬP THÔNG SỐ HỆ THỐNG =================
def read_sys_file(path, scale=1.0, default=None):
    try:
        with open(path, "r") as f:
            val = f.read().strip()
            return round(int(val) / scale, 1) if scale != 1.0 else val
    except Exception:
        return default

def get_system_metrics(cpu_interval=0.5):
    """Thu thập thông số hệ thống và đo thời gian đọc cảm biến"""
    t0 = time.perf_counter()
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    data = {
        "cpu_pct": psutil.cpu_percent(interval=cpu_interval),
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
    read_ms = (time.perf_counter() - t0) * 1000.0
    logger.debug(f"[METRICS_READ] Thu thập cảm biến hoàn tất trong {read_ms:.1f}ms (interval={cpu_interval}s)")
    return data

# ================= HÀM VẼ BIỂU ĐỒ 24H =================
def generate_24h_chart():
    t0 = time.perf_counter()
    cutoff_time = datetime.datetime.now() - datetime.timedelta(hours=24)
    timestamps = []
    cpu_pcts, ram_pcts = [], []
    cpu_temps, bat_temps, bat_pcts = [], [], []

    files_to_read = [f"{METRICS_FILE}.1", METRICS_FILE]

    for filepath in files_to_read:
        if not os.path.exists(filepath):
            continue
        with open(filepath, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) < 8 or row[1] == "cpu_pct":
                    continue
                try:
                    dt = datetime.datetime.strptime(row[0].strip(), "%Y-%m-%d %H:%M:%S")
                    if dt < cutoff_time:
                        continue

                    c_pct = float(row[1])
                    c_temp = float(row[2])
                    r_pct = float(row[3])
                    b_pct = float(row[5]) if row[5] != "N/A" else 0.0
                    b_temp = float(row[6])

                    timestamps.append(dt)
                    cpu_pcts.append(c_pct)
                    ram_pcts.append(r_pct)
                    cpu_temps.append(c_temp if c_temp > 0 else 0.0)
                    bat_temps.append(b_temp if b_temp > 0 else 0.0)
                    bat_pcts.append(b_pct)
                except Exception:
                    continue

    if len(timestamps) < 2:
        logger.warning(f"[CHART] Không đủ điểm dữ liệu ({len(timestamps)} điểm).")
        return None

    with chart_lock:
        x_dates = mdates.date2num(timestamps)
        fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(10, 8.5), sharex=True)
        try:
            fig.patch.set_facecolor('#1e1e2e')

            for ax in (ax1, ax2, ax3):
                ax.set_facecolor('#252538')
                ax.grid(True, linestyle='--', alpha=0.25, color='#ffffff')
                ax.tick_params(colors='#e0e0e0')
                for spine in ax.spines.values():
                    spine.set_color('#444466')

            ax1.plot_date(x_dates, cpu_temps, '-', color='#ff5555', linewidth=2, label='CPU Temp (°C)')
            ax1.plot_date(x_dates, bat_temps, '--', color='#ffb86c', linewidth=1.5, label='Battery Temp (°C)')
            ax1.axhline(y=50, color='#ff0000', linestyle=':', alpha=0.6, label='Ngưỡng cảnh báo (50°C)')
            ax1.set_ylabel('Nhiệt độ (°C)', color='#e0e0e0', fontweight='bold')
            ax1.set_title('THÔNG SỐ HỆ THỐNG SERVER (24 GIỜ QUA)', color='#ffffff', fontweight='bold')
            lg1 = ax1.legend(loc='upper left', facecolor='#252538', edgecolor='#444466')
            for text in lg1.get_texts():
                text.set_color('#ffffff')

            ax2.plot_date(x_dates, cpu_pcts, '-', color='#8be9fd', linewidth=1.8, label='CPU Usage (%)')
            ax2.plot_date(x_dates, ram_pcts, '-', color='#bd93f9', linewidth=2, label='RAM Usage (%)')
            ax2.fill_between(x_dates, cpu_pcts, 0, color='#8be9fd', alpha=0.1)
            ax2.set_ylabel('Tải (%)', color='#e0e0e0', fontweight='bold')
            ax2.set_ylim(0, 105)
            lg2 = ax2.legend(loc='upper left', facecolor='#252538', edgecolor='#444466')
            for text in lg2.get_texts():
                text.set_color('#ffffff')

            ax3.plot_date(x_dates, bat_pcts, '-', color='#50fa7b', linewidth=2, label='Dung lượng Pin (%)')
            ax3.fill_between(x_dates, bat_pcts, 0, color='#50fa7b', alpha=0.15)
            ax3.set_ylabel('Pin (%)', color='#e0e0e0', fontweight='bold')
            ax3.set_ylim(0, 105)
            lg3 = ax3.legend(loc='lower left', facecolor='#252538', edgecolor='#444466')
            for text in lg3.get_texts():
                text.set_color('#ffffff')

            ax3.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M\n%d/%m'))
            plt.xticks(rotation=0)
            plt.tight_layout()

            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=130, facecolor=fig.get_facecolor())
            buf.seek(0)
            render_ms = (time.perf_counter() - t0) * 1000.0
            logger.info(f"[CHART] Đã vẽ xong {len(timestamps)} điểm dữ liệu | Render_Time={render_ms:.1f}ms")
            return buf
        finally:
            plt.close(fig)

# ================= LUỒNG CHẠY NGẦM HỢP NHẤT =================
def unified_background_worker():
    logger.info("Đã khởi chạy luồng giám sát & ghi metrics hợp nhất (CSV).")
    last_power_status = None
    cpu_warning_sent = False
    cycle_count = 0

    while True:
        try:
            m = get_system_metrics(cpu_interval=1.0)

            current_status = m["bat_status"]
            if last_power_status is not None and current_status != last_power_status:
                logger.warning(f"[POWER_EVENT] Trạng thái nguồn thay đổi: {last_power_status} -> {current_status}")
                if current_status == "Discharging":
                    bot.send_message(MY_CHAT_ID, "⚠️ **CẢNH BÁO MẤT ĐIỆN:**\nĐang chạy bằng pin dự trữ.", parse_mode="Markdown")
                elif current_status in ["Charging", "Full", "Not charging"] and last_power_status == "Discharging":
                    bot.send_message(MY_CHAT_ID, "🔌 **CÓ ĐIỆN TRỞ LẠI:**\nĐã nhận lại nguồn sạc.", parse_mode="Markdown")
            last_power_status = current_status

            cpu_temp_c = m["cpu_temp"]
            if cpu_temp_c > 50.0 and not cpu_warning_sent:
                logger.warning(f"[THERMAL_ALERT] Nhiệt độ CPU vượt ngưỡng: {cpu_temp_c}°C")
                bot.send_message(MY_CHAT_ID, f"🔥 **BÁO ĐỘNG QUÁ NHIỆT:** CPU đạt **{cpu_temp_c}°C**!", parse_mode="Markdown")
                cpu_warning_sent = True
            elif 0 < cpu_temp_c < 45.0 and cpu_warning_sent:
                logger.info(f"[THERMAL_NORMAL] Nhiệt độ CPU đã hạ xuống: {cpu_temp_c}°C")
                bot.send_message(MY_CHAT_ID, f"❄️ **Nhiệt độ đã ổn định:** {cpu_temp_c}°C.", parse_mode="Markdown")
                cpu_warning_sent = False

            if cycle_count % 5 == 0:
                metrics_logger.info(
                    f"{m['cpu_pct']},{m['cpu_temp']},{m['ram_pct']},"
                    f"{m['disk_pct']},{m['bat_pct']},{m['bat_temp']},{m['bat_status']}"
                )

            cycle_count = (cycle_count + 1) % 10000

        except Exception as e:
            logger.error(f"Lỗi trong luồng giám sát ngầm: {e}\n{traceback.format_exc()}")

        time.sleep(60)

# ================= XỬ LÝ LỆNH VÀ ĐO THỜI GIAN PHẢN HỒI SERVER =================
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if message.chat.id != MY_CHAT_ID:
        logger.warning(f"[UNAUTHORIZED] Chặn truy cập /start từ chat_id={message.chat.id}")
        return
    t0 = time.perf_counter()
    bot.send_message(message.chat.id, "Bảng điều khiển đã sẵn sàng!", reply_markup=main_menu())
    total_ms = (time.perf_counter() - t0) * 1000.0
    logger.info(f"[CMD_DONE] Cmd='/start' | Total_Response={total_ms:.1f}ms")

@bot.message_handler(commands=['log'])
def send_recent_logs(message):
    """Lệnh ngầm /log (hoặc /log N): In ra 15 dòng cuối của file bot_activity.log"""
    if message.chat.id != MY_CHAT_ID:
        logger.warning(f"[UNAUTHORIZED] Chặn truy cập /log từ chat_id={message.chat.id}")
        return

    t_cmd_start = time.perf_counter()
    parts = message.text.strip().split()
    num_lines = 15
    if len(parts) > 1 and parts[1].isdigit():
        num_lines = max(1, min(int(parts[1]), 50))

    try:
        t_proc = time.perf_counter()
        if not os.path.exists(LOG_FILE):
            bot.send_message(message.chat.id, "⚠️ Chưa tìm thấy file `bot_activity.log`.", parse_mode="Markdown")
            return

        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            last_lines = list(deque(f, maxlen=num_lines))

        proc_ms = (time.perf_counter() - t_proc) * 1000.0
        raw_log = "".join(last_lines).strip() or "File log hiện đang trống."

        # Giới hạn an toàn 3800 ký tự để không vượt ngưỡng 4096 ký tự của Telegram
        if len(raw_log) > 3800:
            raw_log = "...(đã cắt bớt)...\n" + raw_log[-3800:]

        escaped_log = html.escape(raw_log)
        reply_html = (
            f"📜 <b>{len(last_lines)} dòng log mới nhất (bot_activity.log):</b>\n"
            f"<pre>{escaped_log}</pre>\n"
            f"⏱ <i>Đọc file: {proc_ms:.1f}ms</i>"
        )
        bot.send_message(message.chat.id, reply_html, parse_mode="HTML")

        total_ms = (time.perf_counter() - t_cmd_start) * 1000.0
        last_api_ms = getattr(_thread_state, 'last_api_ms', 0.0)
        logger.info(
            f"[CMD_DONE] MsgID={message.message_id} | Cmd='{message.text}' | "
            f"Server_Proc={proc_ms:.1f}ms | Last_API={last_api_ms:.1f}ms | Total_Time={total_ms:.1f}ms"
        )
    except Exception as e:
        logger.error(f"[CMD_FAIL] Lỗi khi đọc file log (/log): {e}")
        bot.send_message(message.chat.id, f"⚠️ Lỗi khi đọc log: {e}")

@bot.message_handler(func=lambda message: True)
def handle_buttons(message):
    t_cmd_start = time.perf_counter()
    now_ts = time.time()

    if message.chat.id != MY_CHAT_ID:
        logger.warning(f"[UNAUTHORIZED] Chặn tin nhắn '{message.text}' từ chat_id={message.chat.id}")
        return

    text = message.text
    # Độ trễ từ lúc người dùng bấm nút trên Telegram đến khi Server nhận được
    net_transit_sec = max(0.0, now_ts - float(message.date))

    logger.info(
        f"[CMD_RECV] MsgID={message.message_id} | Cmd='{text}' | "
        f"Transit_Delay={net_transit_sec:.2f}s"
    )

    proc_ms = 0.0  # Thời gian Server xử lý nội bộ (không tính thời gian gửi API Telegram)

    try:
        # ----- 1. BẬT MÁY TÍNH -----
        if text == "⚡ Bật máy tính":
            wait_msg = bot.send_message(message.chat.id, "⏳ Đang kiểm tra trạng thái PC...")
            t_proc = time.perf_counter()
            is_online = check_pc_online()
            if not is_online:
                subprocess.run(["wakeonlan", PC_MAC])
            proc_ms = (time.perf_counter() - t_proc) * 1000.0

            if is_online:
                reply_text = f"🟢 Máy tính **ĐÃ BẬT SẴN**.\n⏱ _Xử lý: {proc_ms:.0f}ms_"
            else:
                reply_text = f"✅ Đã gửi gói tin kích nguồn (WOL) tới `{PC_MAC}`!\n⏱ _Xử lý: {proc_ms:.0f}ms_"

            bot.edit_message_text(reply_text, chat_id=message.chat.id, message_id=wait_msg.message_id, parse_mode="Markdown")

        # ----- 2. TRẠNG THÁI PC -----
        elif text == "🖥️ Trạng thái PC":
            wait_msg = bot.send_message(message.chat.id, "🔍 Đang ping kiểm tra PC...")
            t_proc = time.perf_counter()
            is_online = check_pc_online()
            proc_ms = (time.perf_counter() - t_proc) * 1000.0

            status_str = "🟢 PC đang **ONLINE**!" if is_online else "🔴 PC đang **TẮT**."
            bot.edit_message_text(
                f"{status_str}\n⏱ _Phản hồi Server: {proc_ms:.0f}ms_",
                chat_id=message.chat.id,
                message_id=wait_msg.message_id,
                parse_mode="Markdown"
            )

        # ----- 3. THÔNG TIN MÁY CHỦ -----
        elif text == "📊 Thông tin Máy chủ":
            wait_msg = bot.send_message(message.chat.id, "⏳ Đang thu thập dữ liệu...")
            t_proc = time.perf_counter()

            uptime_str = str(datetime.timedelta(seconds=int(time.time() - psutil.boot_time())))
            os_info = f"{platform.system()} {platform.release()}"

            m = get_system_metrics(cpu_interval=0.5)
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
            proc_ms = (time.perf_counter() - t_proc) * 1000.0
            api_last_ms = getattr(_thread_state, 'last_api_ms', 0.0)

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
                f"🌐 **Mạng & Độ trễ**\n"
                f"• IP Local: `{local_ip}`\n"
                f"• Data Nhận: `{round(net_io.bytes_recv / (1024**2), 2)} MB`\n"
                f"• Xử lý nội bộ: `{proc_ms:.0f}ms` | Telegram API: `{api_last_ms:.0f}ms`"
            )
            bot.edit_message_text(msg, chat_id=message.chat.id, message_id=wait_msg.message_id, parse_mode="Markdown")

        # ----- 4. VẼ BIỂU ĐỒ 24H -----
        elif text == "📈 Biểu đồ 24h":
            wait_msg = bot.send_message(message.chat.id, "🎨 Đang trích xuất dữ liệu và vẽ biểu đồ...")
            t_proc = time.perf_counter()
            chart_buf = generate_24h_chart()
            proc_ms = (time.perf_counter() - t_proc) * 1000.0

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
                    caption=f"📊 **Biểu đồ Nhiệt độ, Tải CPU/RAM & Pin (24h qua)**\n⏱ _Thời gian vẽ: {proc_ms:.0f}ms_",
                    parse_mode="Markdown"
                )
                bot.delete_message(message.chat.id, wait_msg.message_id)
                chart_buf.close()

        # ----- 5. LỆNH CẬP NHẬT CODE TỪ GITHUB -----
        elif text == "🔄 Cập nhật Code":
            bot.send_message(message.chat.id, "⏳ Đang tải code mới từ GitHub...")
            t_proc = time.perf_counter()

            subprocess.run(["sudo", "git", "config", "--global", "--add", "safe.directory", BASE_DIR])
            res = subprocess.run(
                ["sudo", "git", "pull"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                cwd=BASE_DIR
            )
            proc_ms = (time.perf_counter() - t_proc) * 1000.0

            git_output = res.stdout.strip() or res.stderr.strip() or "Không có thay đổi."
            logger.info(f"[GIT_PULL] Kết quả: {git_output} | Git_Time={proc_ms:.1f}ms")

            bot.send_message(
                message.chat.id,
                f"Trạng thái Git (`{proc_ms:.0f}ms`):\n`{git_output}`\n\n🔄 Code mới đã tải xong! Bot sẽ tự khởi động lại trong 3 giây...",
                parse_mode="Markdown"
            )

            def kill_bot():
                time.sleep(2)
                logger.info("Đang thoát tiến trình để systemd khởi động lại bot...")
                os._exit(0)

            threading.Thread(target=kill_bot, daemon=True).start()

        else:
            bot.send_message(message.chat.id, "❓ Lệnh không hợp lệ. Vui lòng chọn trên menu.", reply_markup=main_menu())

        # Ghi log tổng kết thời gian phản hồi của toàn bộ chu trình xử lý lệnh
        total_ms = (time.perf_counter() - t_cmd_start) * 1000.0
        last_api_ms = getattr(_thread_state, 'last_api_ms', 0.0)
        logger.info(
            f"[CMD_DONE] MsgID={message.message_id} | Cmd='{text}' | "
            f"Server_Proc={proc_ms:.1f}ms | Last_API={last_api_ms:.1f}ms | Total_Time={total_ms:.1f}ms"
        )

    except Exception as e:
        total_ms = (time.perf_counter() - t_cmd_start) * 1000.0
        logger.error(
            f"[CMD_FAIL] MsgID={message.message_id} | Cmd='{text}' | "
            f"Failed_After={total_ms:.1f}ms | Error: {e}\n{traceback.format_exc()}"
        )
        try:
            bot.send_message(message.chat.id, f"⚠️ Lỗi thực thi lệnh: `{e}`", parse_mode="Markdown")
        except Exception:
            pass

# ================= KHỞI CHẠY =================
if __name__ == '__main__':
    logger.info("==================================================")
    logger.info("Đang khởi động Hệ thống Bot Giám sát Server...")
    threading.Thread(target=unified_background_worker, name="BgMetricsWorker", daemon=True).start()

    try:
        bot.send_message(
            MY_CHAT_ID,
            "🚀 **Hệ thống Server đã online!**",
            parse_mode="Markdown",
            reply_markup=main_menu()
        )
    except Exception as e:
        logger.error(f"Lỗi gửi thông báo khởi động: {e}")

    while True:
        try:
            # timeout=15s để Telegram trả kết nối sớm hơn hẳn mức READ_TIMEOUT=30s của urllib3
            logger.info("[POLLING] Bắt đầu vòng lặp infinity_polling (timeout=15s, read_timeout=30s)...")
            bot.infinity_polling(timeout=15, long_polling_timeout=15, skip_pending=False)
        except Exception as e:
            logger.warning(f"[POLLING_DROP] Mất kết nối tới Telegram ({e}). Đang làm mới session và kết nối lại sau 3 giây...")
            _reset_current_thread_session()
            time.sleep(3)