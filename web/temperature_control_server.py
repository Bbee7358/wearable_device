from datetime import datetime, timedelta
import csv
import os
import re
import threading
import time

from flask import Flask, jsonify, render_template, request, send_file

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None


app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

# Arduino側のSerial.begin(115200)と合わせます。
BAUD_RATE = 115200

# PC側で許可する温度範囲です。安全停止の最終判断はArduino側でも行ってください。
TEMP_MIN = 10.0
TEMP_MAX = 37.0

# Webグラフには直近100件だけ返します。CSVには全件保存します。
HISTORY_LIMIT = 720
DEBUG_LOG_LIMIT = 200
SERVER_HOST = "127.0.0.1"
SERVER_PORT = int(os.environ.get("TEMP_CONTROL_PORT", "5050"))
CSV_HEADER = ["timestamp", "peltier_temp", "heart_rate"]

SAFETY_ERROR_CODES = {"TEMP_UNDER", "TEMP_OVER"}


# Flaskは複数リクエストを同時に処理します。
# さらにシリアル受信スレッドも動くため、共有データはLockで守ります。
state_lock = threading.Lock()
serial_lock = threading.Lock()
csv_lock = threading.Lock()

serial_port = None
reader_thread = None
reader_running = False

schedule_thread = None
schedule_cancel_event = threading.Event()

history = []
debug_log = []

csv_file_path = None
csv_file_initialized = False
serial_receive_buffer = ""


app_state = {
    "pc_state": "DISCONNECTED",
    "connected": False,
    "port": "",
    "current_temp": None,
    "target_temp": None,
    "heart_rate": None,
    "pwm_value": None,
    "arduino_state": "",
    "error": "NONE",
    "last_received": "",
    "logging": False,
    "csv_file": "",
    "schedule_state": "未設定",
    "scheduled_start": "",
    "scheduled_end": "",
    "message": "",
    "warning": "",
    "warning_persistent": False,
    "last_sent_command": "",
    "last_sent_time": "",
    "last_raw_received": "",
    "last_raw_received_time": "",
    "status_count": 0,
    "parse_error_count": 0,
    "waiting_for_status": False,
    "last_ack": "",
    "last_command_error": "",
}


def now_text():
    """CSVや画面表示で使う現在時刻文字列を返します。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def set_message(message, warning=None):
    """Web画面に表示する通常メッセージと警告を更新します。"""
    with state_lock:
        app_state["message"] = message
        if warning is not None:
            app_state["warning"] = warning


def error_response(message, status_code=400):
    """APIエラーを返しつつ、次の画面更新でも消えない警告として残します。"""
    with state_lock:
        app_state["message"] = message
        app_state["warning"] = message
        app_state["warning_persistent"] = True
    return jsonify({"error": message}), status_code


def update_pc_state(pc_state):
    """PC側で管理する状態を更新します。"""
    with state_lock:
        app_state["pc_state"] = pc_state


def reset_connection_observability():
    """新しいシリアル接続の表示用カウンタを初期化します。"""
    with state_lock:
        app_state["last_received"] = ""
        app_state["last_raw_received"] = ""
        app_state["last_raw_received_time"] = ""
        app_state["last_ack"] = ""
        app_state["last_command_error"] = ""
        app_state["status_count"] = 0
        app_state["parse_error_count"] = 0
        app_state["waiting_for_status"] = False


def add_debug(direction, text):
    """画面に出す送受信デバッグログを追加します。"""
    item = {
        "timestamp": now_text(),
        "direction": direction,
        "text": text,
    }
    with state_lock:
        debug_log.append(item)
        del debug_log[:-DEBUG_LOG_LIMIT]


def list_serial_ports():
    """PCに接続されているシリアルポート一覧を返します。"""
    if list_ports is None:
        return []

    ports = []
    for port in list_ports.comports():
        ports.append({
            "device": port.device,
            "description": port.description,
            "hwid": port.hwid,
        })
    return ports


def choose_auto_connect_port(ports):
    """
    自動接続に使うポートを選びます。
    ArduinoらしいUSBシリアルを優先し、見つからなければ先頭のポートを使います。
    """
    if not ports:
        return None

    keywords = [
        "arduino",
        "usbmodem",
        "usbserial",
        "ttyacm",
        "ch340",
        "cp210",
        "wch",
    ]

    for port in ports:
        text = f"{port['device']} {port['description']} {port['hwid']}".lower()
        if any(keyword in text for keyword in keywords):
            return port["device"]

    return ports[0]["device"]


def close_serial_connection():
    """シリアル接続、受信スレッド、スケジュール状態をまとめて停止します。"""
    global serial_port, reader_running

    schedule_cancel_event.set()
    reader_running = False

    with serial_lock:
        if serial_port is not None:
            try:
                if serial_port.is_open:
                    serial_port.close()
            finally:
                serial_port = None

    with state_lock:
        app_state["pc_state"] = "DISCONNECTED"
        app_state["connected"] = False
        app_state["port"] = ""
        app_state["current_temp"] = None
        app_state["target_temp"] = None
        app_state["heart_rate"] = None
        app_state["pwm_value"] = None
        app_state["arduino_state"] = ""
        app_state["error"] = "NONE"
        app_state["last_received"] = ""
        app_state["schedule_state"] = "未設定"
        app_state["scheduled_start"] = ""
        app_state["scheduled_end"] = ""
        app_state["logging"] = False
        app_state["warning"] = ""
        app_state["warning_persistent"] = False
        app_state["waiting_for_status"] = False
        app_state["last_ack"] = ""
        app_state["last_command_error"] = ""


def sanitize_csv_filename(filename):
    """
    CSVファイル名を安全な形に整えます。
    パス区切りを含む入力はファイル名部分だけを使います。
    """
    filename = (filename or "").strip()
    if not filename:
        filename = datetime.now().strftime("log_%Y%m%d_%H%M%S.csv")

    filename = os.path.basename(filename)
    filename = re.sub(r"[^A-Za-z0-9_.-]", "_", filename)

    if not filename.lower().endswith(".csv"):
        filename += ".csv"

    return filename


def configure_csv_file(filename):
    """Web画面で指定されたCSVファイル名を保存先として設定します。"""
    global csv_file_path, csv_file_initialized

    safe_name = sanitize_csv_filename(filename)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    logs_dir = os.path.join(base_dir, "logs")
    os.makedirs(logs_dir, exist_ok=True)

    with csv_lock:
        csv_file_path = os.path.join(logs_dir, safe_name)
        if os.path.exists(csv_file_path) and os.path.getsize(csv_file_path) > 0:
            with open(csv_file_path, newline="", encoding="utf-8") as file:
                first_row = next(csv.reader(file), [])

            if first_row != CSV_HEADER:
                stem, extension = os.path.splitext(safe_name)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                csv_file_path = os.path.join(logs_dir, f"{stem}_simple_{timestamp}{extension}")

        csv_file_initialized = os.path.exists(csv_file_path) and os.path.getsize(csv_file_path) > 0

    with state_lock:
        app_state["csv_file"] = csv_file_path
        app_state["logging"] = True

    return csv_file_path


def append_csv(row):
    """STATUSを受信するたびに、時刻・ペルチェ温度・心拍数だけをCSVへ追記します。"""
    global csv_file_initialized

    if csv_file_path is None:
        configure_csv_file("")

    with csv_lock:
        needs_header = not csv_file_initialized
        with open(csv_file_path, "a", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            if needs_header:
                writer.writerow(CSV_HEADER)
                csv_file_initialized = True

            writer.writerow([
                row["timestamp"],
                row["current_temp"],
                row["heart_rate"],
            ])

    with state_lock:
        app_state["logging"] = True


def validate_temperature_value(value, name):
    """10.0〜37.0℃の範囲か確認してfloatに変換します。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}は数値で入力してください。")

    if number < TEMP_MIN or number > TEMP_MAX:
        raise ValueError(f"{name}は{TEMP_MIN:.1f}〜{TEMP_MAX:.1f}℃の範囲で入力してください。")

    return number


def validate_temperature_set(target_temp, min_temp, max_temp):
    """最低温度 <= 目標温度 <= 最高温度の関係を確認します。"""
    target = validate_temperature_value(target_temp, "目標温度")
    minimum = validate_temperature_value(min_temp, "最低温度")
    maximum = validate_temperature_value(max_temp, "最高温度")

    if minimum > maximum:
        raise ValueError("最低温度は最高温度以下になるように設定してください。")

    if not (minimum <= target <= maximum):
        raise ValueError("目標温度は最低温度〜最高温度の範囲内にしてください。")

    return target, minimum, maximum


def make_set_command_from_payload(data):
    """画面入力からSETコマンドを作ります。"""
    target, minimum, maximum = validate_temperature_set(
        data.get("target_temp"),
        data.get("min_temp"),
        data.get("max_temp"),
    )
    return f"SET,{target:.1f},{minimum:.1f},{maximum:.1f}", target, minimum, maximum


def send_command(command):
    """
    Arduinoへ1行コマンドを送信します。
    Arduino側では改行までを1コマンドとして読む想定です。
    """
    with serial_lock:
        if serial_port is None or not serial_port.is_open:
            raise RuntimeError("Arduinoに接続されていません。")

        serial_port.write((command + "\n").encode("utf-8"))
        serial_port.flush()

    with state_lock:
        app_state["last_sent_command"] = command
        app_state["last_sent_time"] = now_text()

    add_debug("TX", command)


def parse_status(line):
    """
    Arduinoから来たSTATUS行を解析します。
    形式: STATUS,currentTemp,targetTemp,heartRate,pwmValue,state,error
    互換形式: STATUS,currentTemp,heartRate,pwmValue,state,error
    """
    parts = line.strip().split(",")
    if parts[0] != "STATUS":
        raise ValueError("STATUSの形式が正しくありません。")

    if len(parts) == 7:
        target_temp = float(parts[2])
        heart_rate = int(float(parts[3]))
        pwm_value = int(float(parts[4]))
        state = parts[5].strip()
        error = parts[6].strip()
    elif len(parts) == 6:
        with state_lock:
            target_temp = app_state["target_temp"]
        heart_rate = int(float(parts[2]))
        pwm_value = int(float(parts[3]))
        state = parts[4].strip()
        error = parts[5].strip()
    else:
        raise ValueError(f"STATUSの項目数が違います: {len(parts)}項目")

    return {
        "timestamp": now_text(),
        "current_temp": float(parts[1]),
        "target_temp": target_temp,
        "heart_rate": heart_rate,
        "pwm_value": pwm_value,
        "state": state,
        "error": error,
    }


def handle_status(line):
    """STATUS受信後、状態更新、履歴追加、CSV追記をまとめて実行します。"""
    row = parse_status(line)

    with state_lock:
        app_state["current_temp"] = row["current_temp"]
        app_state["target_temp"] = row["target_temp"]
        app_state["heart_rate"] = row["heart_rate"]
        app_state["pwm_value"] = row["pwm_value"]
        app_state["arduino_state"] = row["state"]
        app_state["error"] = row["error"]
        app_state["last_received"] = row["timestamp"]
        app_state["status_count"] += 1
        app_state["waiting_for_status"] = False
        if row["state"] == "ERROR" and row["error"] == "NONE":
            app_state["warning"] = "ArduinoはERROR状態です。RESETしてから開始してください。"
            app_state["warning_persistent"] = False
        elif row["error"] == "NONE":
            if not app_state["warning_persistent"]:
                app_state["warning"] = ""
        else:
            app_state["warning"] = f"Arduino警告: {row['error']}"
            app_state["warning_persistent"] = False
        app_state["message"] = f"STATUS受信OK: {row['state']} / PWM {row['pwm_value']}"

        if row["error"] != "NONE" or row["state"] == "ERROR":
            app_state["pc_state"] = "ERROR"
        elif row["state"] == "RUNNING":
            app_state["pc_state"] = "RUNNING"
            app_state["schedule_state"] = "Arduino RUNNING確認"
        elif row["state"] == "STOPPED":
            app_state["pc_state"] = "STOPPED"
        elif app_state["pc_state"] not in ("SCHEDULED", "DISCONNECTED"):
            app_state["pc_state"] = "CONNECTED"

        history.append(row)

    append_csv(row)


def handle_ready():
    """Arduino起動時のREADYを受け取り、接続済みとして表示します。"""
    with state_lock:
        app_state["pc_state"] = "CONNECTED"
        app_state["connected"] = True
        app_state["arduino_state"] = "READY"
        app_state["last_received"] = now_text()
        app_state["warning"] = ""
        app_state["message"] = "Arduino READY を受信しました。"


def handle_ack(line):
    """OK行を正常応答として扱い、解析失敗には数えません。"""
    with state_lock:
        app_state["last_ack"] = line.strip()
        app_state["last_received"] = now_text()
        app_state["message"] = f"Arduino応答: {line.strip()}"
        app_state["warning"] = ""
        app_state["warning_persistent"] = False
        app_state["last_command_error"] = ""


def handle_error(line):
    """ERROR行を受け取り、安全エラーとコマンドエラーを分けて表示します。"""
    parts = line.strip().split(",", 1)
    error_code = parts[1].strip() if len(parts) == 2 else "UNKNOWN"

    with state_lock:
        app_state["last_received"] = now_text()
        app_state["waiting_for_status"] = False

        if error_code in SAFETY_ERROR_CODES:
            app_state["pc_state"] = "ERROR"
            app_state["arduino_state"] = "ERROR"
            app_state["error"] = error_code
            app_state["warning"] = f"Arduino緊急停止: {error_code}"
            app_state["warning_persistent"] = False
            app_state["message"] = f"安全エラーを受信しました: {error_code}"
        else:
            app_state["last_command_error"] = error_code
            app_state["warning"] = f"Arduinoコマンドエラー: {error_code}"
            app_state["warning_persistent"] = False
            app_state["message"] = f"Arduinoコマンドエラー: {error_code}"

    add_debug("RX_ERROR", line.strip())


def serial_reader_loop():
    """別スレッドでArduinoからの受信を監視し続けます。"""
    global reader_running, serial_receive_buffer

    while reader_running:
        try:
            with serial_lock:
                port = serial_port

            if port is None or not port.is_open:
                time.sleep(0.2)
                continue

            raw = port.readline()
            if not raw:
                continue

            raw_text = raw.decode("utf-8", errors="replace")
            with state_lock:
                app_state["last_raw_received"] = raw_text.strip()
                app_state["last_raw_received_time"] = now_text()

            add_debug("RX_RAW", raw_text.rstrip("\r\n"))

            starts_new_message = raw_text.startswith(("READY", "OK,", "STATUS,", "ERROR,"))
            if serial_receive_buffer and starts_new_message:
                add_debug("RX_RESYNC", f"途中受信を破棄して再同期: {serial_receive_buffer!r}")
                serial_receive_buffer = ""

            if not raw.endswith((b"\n", b"\r")):
                serial_receive_buffer += raw_text
                if len(serial_receive_buffer) > 300:
                    add_debug("RX_DROP", f"改行なし受信が長すぎるため破棄: {serial_receive_buffer!r}")
                    serial_receive_buffer = ""
                else:
                    add_debug("RX_WAIT", f"改行待ち: {serial_receive_buffer!r}")
                continue

            line = (serial_receive_buffer + raw_text).strip()
            serial_receive_buffer = ""
            if not line:
                continue

            if line == "READY":
                add_debug("RX", line)
                handle_ready()
            elif line.startswith("OK,"):
                add_debug("RX", line)
                handle_ack(line)
            elif line.startswith("STATUS,"):
                add_debug("RX", line)
                handle_status(line)
            elif line.startswith("ERROR,"):
                add_debug("RX", line)
                handle_error(line)
            else:
                with state_lock:
                    app_state["parse_error_count"] += 1
                set_message(f"未対応の受信データ: {line}")
                add_debug("RX_UNPARSED", line)

        except Exception as exc:
            if not reader_running:
                break
            with state_lock:
                app_state["parse_error_count"] += 1
                message = str(exc)
                if "read failed" in message or "device reports readiness" in message or "Bad file descriptor" in message:
                    app_state["pc_state"] = "DISCONNECTED"
                    app_state["connected"] = False
                    app_state["port"] = ""
            set_message(f"シリアル受信エラー: {exc}", warning=f"シリアル受信エラー: {exc}")
            add_debug("RX_ERROR", str(exc))
            time.sleep(1)


def ensure_reader_thread():
    """シリアル受信用スレッドを必要に応じて起動します。"""
    global reader_thread, reader_running

    if reader_thread is not None and reader_thread.is_alive():
        if reader_running:
            return
        reader_thread.join(timeout=1)

    reader_running = True
    reader_thread = threading.Thread(target=serial_reader_loop, daemon=True)
    reader_thread.start()


def parse_start_datetime(start_time_text):
    """
    HH:MM形式の開始時刻をdatetimeに変換します。
    今日の時刻をすでに過ぎている場合は、翌日の同じ時刻にします。
    """
    try:
        hour_text, minute_text = start_time_text.split(":")
        hour = int(hour_text)
        minute = int(minute_text)
    except (ValueError, AttributeError):
        raise ValueError("開始時刻はHH:MM形式で入力してください。")

    now = datetime.now()
    start_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if start_at <= now:
        start_at += timedelta(days=1)

    return start_at


def schedule_worker(start_at, cooling_minutes, set_command=None):
    """
    指定時刻まで待ってSTARTを送信し、冷却時間が終わったらSTOPを送信します。
    STOPボタンが押された場合はschedule_cancel_eventで中断します。
    """
    end_at = start_at + timedelta(minutes=cooling_minutes)

    with state_lock:
        app_state["pc_state"] = "SCHEDULED"
        app_state["schedule_state"] = "開始待ち"
        app_state["scheduled_start"] = start_at.strftime("%Y-%m-%d %H:%M:%S")
        app_state["scheduled_end"] = end_at.strftime("%Y-%m-%d %H:%M:%S")

    seconds_until_start = max(0, (start_at - datetime.now()).total_seconds())
    if schedule_cancel_event.wait(seconds_until_start):
        with state_lock:
            app_state["schedule_state"] = "キャンセル済み"
        return

    try:
        if set_command:
            send_command(set_command)
            time.sleep(0.2)
        send_command("START")
        with state_lock:
            app_state["pc_state"] = "CONNECTED"
            app_state["schedule_state"] = "START送信済み・STATUS待ち"
            app_state["waiting_for_status"] = True
    except Exception as exc:
        set_message(f"START送信に失敗しました: {exc}", warning=f"START送信に失敗しました: {exc}")
        update_pc_state("ERROR")
        return

    seconds_until_stop = max(0, cooling_minutes * 60)
    if schedule_cancel_event.wait(seconds_until_stop):
        with state_lock:
            app_state["schedule_state"] = "停止済み"
        return

    try:
        send_command("STOP")
        with state_lock:
            app_state["pc_state"] = "STOPPED"
            app_state["schedule_state"] = "完了"
    except Exception as exc:
        set_message(f"STOP送信に失敗しました: {exc}", warning=f"STOP送信に失敗しました: {exc}")
        update_pc_state("ERROR")


def json_payload():
    """JSONまたはフォーム送信のどちらでも値を受け取れるようにします。"""
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form.to_dict()


@app.route("/")
def index():
    """Web画面を表示します。HTMLはtemplates/index.htmlに分けています。"""
    return render_template("index.html")


@app.after_request
def add_no_cache_headers(response):
    """開発中に古いHTML/CSS/JSがブラウザに残りにくいようにします。"""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/api/ports")
def api_ports():
    """使用可能なシリアルポート一覧をJSONで返します。"""
    if serial is None:
        return jsonify({
            "ports": [],
            "error": "pyserialがインストールされていません。pip install pyserial を実行してください。",
        }), 500

    return jsonify({"ports": list_serial_ports()})


@app.route("/auto_connect", methods=["POST"])
def auto_connect():
    """接続されているシリアルポートを探し、自動でArduinoへ接続します。"""
    global serial_port, serial_receive_buffer

    if serial is None:
        return jsonify({"error": "pyserialがインストールされていません。pip install pyserial を実行してください。"}), 500

    with state_lock:
        if app_state["connected"]:
            return jsonify({"message": f"すでに {app_state['port']} に接続されています。"})

    ports = list_serial_ports()
    port_name = choose_auto_connect_port(ports)
    if port_name is None:
        return jsonify({"error": "接続できるシリアルポートが見つかりませんでした。"}), 404

    try:
        with serial_lock:
            if serial_port is not None and serial_port.is_open:
                serial_port.close()

            serial_port = serial.Serial(port_name, BAUD_RATE, timeout=1)
            serial_receive_buffer = ""

        reset_connection_observability()
        ensure_reader_thread()
        add_debug("OPEN", f"{port_name} / {BAUD_RATE}bps")

        with state_lock:
            app_state["pc_state"] = "CONNECTED"
            app_state["connected"] = True
            app_state["port"] = port_name
            app_state["warning"] = ""

        return jsonify({"message": f"{port_name} に自動接続しました。", "port": port_name})
    except Exception as exc:
        with state_lock:
            app_state["pc_state"] = "DISCONNECTED"
            app_state["connected"] = False
            app_state["warning"] = f"自動接続エラー: {exc}"

        return jsonify({"error": f"自動接続に失敗しました: {exc}"}), 500


@app.route("/connect", methods=["POST"])
def connect():
    global serial_port, serial_receive_buffer

    if serial is None:
        return jsonify({"error": "pyserialがインストールされていません。pip install pyserial を実行してください。"}), 500

    data = json_payload()
    port_name = (data.get("port") or "").strip()
    if not port_name:
        return jsonify({"error": "シリアルポート名を入力してください。"}), 400

    try:
        with serial_lock:
            if serial_port is not None and serial_port.is_open:
                serial_port.close()

            serial_port = serial.Serial(port_name, BAUD_RATE, timeout=1)
            serial_receive_buffer = ""

        reset_connection_observability()
        ensure_reader_thread()
        add_debug("OPEN", f"{port_name} / {BAUD_RATE}bps")

        with state_lock:
            app_state["pc_state"] = "CONNECTED"
            app_state["connected"] = True
            app_state["port"] = port_name
            app_state["warning"] = ""

        return jsonify({"message": f"{port_name} に接続しました。"})
    except Exception as exc:
        with state_lock:
            app_state["pc_state"] = "DISCONNECTED"
            app_state["connected"] = False
            app_state["warning"] = f"接続エラー: {exc}"

        return jsonify({"error": f"シリアルポートを開けませんでした: {exc}"}), 500


@app.route("/disconnect", methods=["POST"])
def disconnect():
    """Web画面の切断ボタンからシリアル接続を確実に閉じます。"""
    close_serial_connection()
    add_debug("CLOSE", "シリアル接続を切断しました。")
    return jsonify({"message": "シリアル接続を切断しました。"})


@app.route("/send_settings", methods=["POST"])
def send_settings():
    data = json_payload()

    try:
        command, target, minimum, maximum = make_set_command_from_payload(data)
        configure_csv_file(data.get("csv_file"))
        send_command(command)

        with state_lock:
            app_state["target_temp"] = target
            app_state["warning"] = ""

        return jsonify({"message": "設定をArduinoへ送信しました。"})
    except ValueError as exc:
        return error_response(str(exc), 400)
    except Exception as exc:
        return jsonify({"error": f"設定送信に失敗しました: {exc}"}), 500


@app.route("/start_now", methods=["POST"])
def start_now():
    data = json_payload()

    try:
        with state_lock:
            arduino_state = app_state["arduino_state"]
            pc_state = app_state["pc_state"]

        if arduino_state == "ERROR" or pc_state == "ERROR":
            return error_response("ArduinoがERROR状態です。RESETしてWAITに戻してから開始してください。", 409)

        set_command, target, minimum, maximum = make_set_command_from_payload(data)
        configure_csv_file(data.get("csv_file"))
        schedule_cancel_event.set()
        send_command(set_command)
        time.sleep(0.2)
        send_command("START")

        with state_lock:
            app_state["pc_state"] = "CONNECTED"
            app_state["target_temp"] = target
            app_state["schedule_state"] = "START送信済み・STATUS待ち"
            app_state["scheduled_start"] = now_text()
            app_state["scheduled_end"] = ""
            app_state["warning"] = ""
            app_state["waiting_for_status"] = True

        return jsonify({"message": f"{set_command} と START を送信しました。STATUS待ちです。"})
    except ValueError as exc:
        return error_response(str(exc), 400)
    except Exception as exc:
        return jsonify({"error": f"START送信に失敗しました: {exc}"}), 500


@app.route("/schedule_start", methods=["POST"])
def schedule_start():
    global schedule_thread

    data = json_payload()

    try:
        set_command, target, minimum, maximum = make_set_command_from_payload(data)
        start_at = parse_start_datetime(data.get("start_time"))
        cooling_minutes = int(data.get("cooling_minutes"))
        if cooling_minutes <= 0:
            raise ValueError("冷却時間は1分以上で入力してください。")

        configure_csv_file(data.get("csv_file"))

        schedule_cancel_event.set()
        time.sleep(0.05)
        schedule_cancel_event.clear()

        schedule_thread = threading.Thread(
            target=schedule_worker,
            args=(start_at, cooling_minutes, set_command),
            daemon=True,
        )
        schedule_thread.start()

        with state_lock:
            app_state["target_temp"] = target

        return jsonify({
            "message": f"{start_at.strftime('%Y-%m-%d %H:%M:%S')} にSETとSTARTを送信するよう予約しました。"
        })
    except ValueError as exc:
        return error_response(str(exc), 400)
    except Exception as exc:
        return jsonify({"error": f"スケジュール設定に失敗しました: {exc}"}), 500


@app.route("/stop", methods=["POST"])
def stop():
    try:
        schedule_cancel_event.set()
        send_command("STOP")

        with state_lock:
            app_state["pc_state"] = "STOPPED"
            app_state["schedule_state"] = "停止済み"
            app_state["warning"] = ""
            app_state["waiting_for_status"] = False

        return jsonify({"message": "STOPを送信しました。"})
    except Exception as exc:
        return jsonify({"error": f"STOP送信に失敗しました: {exc}"}), 500


@app.route("/set_target", methods=["POST"])
def set_target():
    data = json_payload()

    try:
        target = validate_temperature_value(data.get("target_temp"), "目標温度")
        send_command(f"SET_TARGET,{target:.1f}")

        with state_lock:
            app_state["target_temp"] = target
            app_state["warning"] = ""

        return jsonify({"message": "目標温度変更を送信しました。"})
    except ValueError as exc:
        return error_response(str(exc), 400)
    except Exception as exc:
        return jsonify({"error": f"目標温度変更に失敗しました: {exc}"}), 500


@app.route("/vib_on", methods=["POST"])
def vib_on():
    try:
        send_command("VIB_ON")
        return jsonify({"message": "VIB_ONを送信しました。"})
    except Exception as exc:
        return jsonify({"error": f"VIB_ON送信に失敗しました: {exc}"}), 500


@app.route("/vib_off", methods=["POST"])
def vib_off():
    try:
        send_command("VIB_OFF")
        return jsonify({"message": "VIB_OFFを送信しました。"})
    except Exception as exc:
        return jsonify({"error": f"VIB_OFF送信に失敗しました: {exc}"}), 500


@app.route("/reset", methods=["POST"])
def reset():
    try:
        schedule_cancel_event.set()
        send_command("RESET")

        with state_lock:
            app_state["pc_state"] = "CONNECTED"
            app_state["schedule_state"] = "未設定"
            app_state["scheduled_start"] = ""
            app_state["scheduled_end"] = ""
            app_state["error"] = "NONE"
            app_state["warning"] = ""
            app_state["waiting_for_status"] = False

        return jsonify({"message": "RESETを送信しました。"})
    except Exception as exc:
        return jsonify({"error": f"RESET送信に失敗しました: {exc}"}), 500


@app.route("/api/status")
def api_status():
    with state_lock:
        return jsonify(dict(app_state))


@app.route("/api/history")
def api_history():
    with state_lock:
        return jsonify(history[-HISTORY_LIMIT:])


@app.route("/download_csv")
def download_csv():
    with state_lock:
        path = app_state["csv_file"]

    if not path or not os.path.exists(path):
        return jsonify({"error": "出力できるCSVファイルがまだありません。"}), 404

    return send_file(path, as_attachment=True, download_name=os.path.basename(path))


@app.route("/api/debug")
def api_debug():
    with state_lock:
        return jsonify({
            "last_sent_command": app_state["last_sent_command"],
            "last_sent_time": app_state["last_sent_time"],
            "last_raw_received": app_state["last_raw_received"],
            "last_raw_received_time": app_state["last_raw_received_time"],
            "status_count": app_state["status_count"],
            "parse_error_count": app_state["parse_error_count"],
            "waiting_for_status": app_state["waiting_for_status"],
            "last_ack": app_state["last_ack"],
            "last_command_error": app_state["last_command_error"],
            "log": list(debug_log[-DEBUG_LOG_LIMIT:]),
        })


if __name__ == "__main__":
    print("Arduino温度制御Webサーバーを起動します。")
    print(f"ブラウザで http://{SERVER_HOST}:{SERVER_PORT} を開いてください。")
    app.run(host=SERVER_HOST, port=SERVER_PORT, debug=False, threaded=True)
