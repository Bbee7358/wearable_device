from flask import Flask, jsonify, render_template_string, request
from datetime import datetime, timedelta
import csv
import os
import re
import threading
import time

try:
    import serial
except ImportError:
    serial = None


app = Flask(__name__)

# Arduinoとの通信速度です。Arduino側のSerial.begin(115200)と合わせます。
BAUD_RATE = 115200

# 温度入力の許容範囲です。
TEMP_MIN = 10.0
TEMP_MAX = 37.0

# グラフに返す履歴件数です。CSVには全件保存します。
HISTORY_LIMIT = 100


# 複数スレッドから同時に状態を書き換えるため、Lockで保護します。
state_lock = threading.Lock()
serial_lock = threading.Lock()
csv_lock = threading.Lock()

serial_port = None
reader_thread = None
reader_running = False

schedule_thread = None
schedule_cancel_event = threading.Event()

history = []

csv_file_path = None
csv_file_initialized = False


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
}


HTML = """
<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Arduino 温度制御</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f6f8;
      --panel: #ffffff;
      --line: #d8dee6;
      --text: #1f2933;
      --muted: #5f6f82;
      --accent: #1b7f79;
      --accent-dark: #12615c;
      --danger: #c62828;
      --ok: #1f7a3f;
    }
    * {
      box-sizing: border-box;
    }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    header {
      padding: 20px 24px;
      background: #263238;
      color: white;
    }
    header h1 {
      margin: 0;
      font-size: 24px;
      font-weight: 700;
      letter-spacing: 0;
    }
    main {
      width: min(1280px, calc(100% - 32px));
      margin: 18px auto 40px;
      display: grid;
      gap: 16px;
    }
    .grid {
      display: grid;
      grid-template-columns: 360px 1fr;
      gap: 16px;
      align-items: start;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
    }
    h2 {
      margin: 0 0 14px;
      font-size: 18px;
    }
    label {
      display: block;
      margin: 12px 0 5px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 600;
    }
    input {
      width: 100%;
      height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 10px;
      font-size: 15px;
      background: white;
      color: var(--text);
    }
    .button-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-top: 14px;
    }
    button {
      min-height: 38px;
      border: 0;
      border-radius: 6px;
      padding: 8px 10px;
      color: white;
      background: var(--accent);
      font-weight: 700;
      cursor: pointer;
      line-height: 1.2;
    }
    button:hover {
      background: var(--accent-dark);
    }
    button.secondary {
      background: #546e7a;
    }
    button.danger {
      background: var(--danger);
    }
    .status-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }
    .status-item {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      min-height: 66px;
      background: #fafbfc;
    }
    .status-label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }
    .status-value {
      margin-top: 6px;
      font-size: 18px;
      font-weight: 700;
      overflow-wrap: anywhere;
    }
    .message {
      min-height: 28px;
      margin-top: 10px;
      color: var(--ok);
      font-weight: 700;
    }
    .warning {
      display: none;
      border: 1px solid #ef9a9a;
      background: #ffebee;
      color: var(--danger);
      border-radius: 8px;
      padding: 14px;
      font-size: 18px;
      font-weight: 800;
    }
    .chart-wrap {
      display: grid;
      grid-template-columns: 1fr;
      gap: 16px;
    }
    canvas {
      width: 100%;
      max-height: 320px;
    }
    @media (max-width: 900px) {
      .grid {
        grid-template-columns: 1fr;
      }
      .status-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }
    @media (max-width: 520px) {
      main {
        width: min(100% - 20px, 1280px);
      }
      .button-grid,
      .status-grid {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <header>
    <h1>Arduino 温度制御</h1>
  </header>

  <main>
    <div id="warning" class="warning"></div>

    <div class="grid">
      <section>
        <h2>設定</h2>

        <label for="port">シリアルポート名</label>
        <input id="port" value="/dev/tty.usbmodemXXXX" autocomplete="off">

        <label for="targetTemp">目標温度 ℃</label>
        <input id="targetTemp" type="number" min="10" max="37" step="0.1" value="25.0">

        <label for="minTemp">最低温度 ℃</label>
        <input id="minTemp" type="number" min="10" max="37" step="0.1" value="10.0">

        <label for="maxTemp">最高温度 ℃</label>
        <input id="maxTemp" type="number" min="10" max="37" step="0.1" value="37.0">

        <label for="startTime">開始時刻 HH:MM</label>
        <input id="startTime" type="time">

        <label for="coolingMinutes">冷却時間 分</label>
        <input id="coolingMinutes" type="number" min="1" step="1" value="10">

        <label for="csvFile">CSVファイル名</label>
        <input id="csvFile" value="experiment_01.csv" autocomplete="off">

        <div class="button-grid">
          <button onclick="connectSerial()">接続</button>
          <button onclick="sendSettings()">設定送信</button>
          <button onclick="scheduleStart()">スケジュール開始</button>
          <button onclick="startNow()">今すぐ開始</button>
          <button class="danger" onclick="stopControl()">停止</button>
          <button onclick="setTarget()">目標温度変更</button>
          <button class="secondary" onclick="sendSimple('/vib_on')">振動ON</button>
          <button class="secondary" onclick="sendSimple('/vib_off')">振動OFF</button>
          <button class="danger" onclick="sendSimple('/reset')">RESET</button>
        </div>

        <div id="message" class="message"></div>
      </section>

      <section>
        <h2>最新状態</h2>
        <div class="status-grid">
          <div class="status-item"><div class="status-label">接続状態</div><div id="connected" class="status-value">-</div></div>
          <div class="status-item"><div class="status-label">現在温度</div><div id="currentTemp" class="status-value">-</div></div>
          <div class="status-item"><div class="status-label">目標温度</div><div id="targetTempStatus" class="status-value">-</div></div>
          <div class="status-item"><div class="status-label">心拍数</div><div id="heartRate" class="status-value">-</div></div>
          <div class="status-item"><div class="status-label">ペルチェPWM値</div><div id="pwmValue" class="status-value">-</div></div>
          <div class="status-item"><div class="status-label">Arduinoの状態</div><div id="arduinoState" class="status-value">-</div></div>
          <div class="status-item"><div class="status-label">エラー内容</div><div id="error" class="status-value">-</div></div>
          <div class="status-item"><div class="status-label">最終受信時刻</div><div id="lastReceived" class="status-value">-</div></div>
          <div class="status-item"><div class="status-label">ログ保存中</div><div id="logging" class="status-value">-</div></div>
          <div class="status-item"><div class="status-label">スケジュール状態</div><div id="scheduleState" class="status-value">-</div></div>
          <div class="status-item"><div class="status-label">冷却開始予定時刻</div><div id="scheduledStart" class="status-value">-</div></div>
          <div class="status-item"><div class="status-label">冷却終了予定時刻</div><div id="scheduledEnd" class="status-value">-</div></div>
        </div>
      </section>
    </div>

    <section class="chart-wrap">
      <h2>温度変化グラフ</h2>
      <canvas id="temperatureChart"></canvas>
    </section>

    <section class="chart-wrap">
      <h2>ペルチェPWMグラフ</h2>
      <canvas id="pwmChart"></canvas>
    </section>
  </main>

  <script>
    const temperatureChart = new Chart(document.getElementById("temperatureChart"), {
      type: "line",
      data: {
        labels: [],
        datasets: [
          {
            label: "現在温度 ℃",
            data: [],
            borderColor: "#d84315",
            backgroundColor: "rgba(216, 67, 21, 0.12)",
            tension: 0.2
          },
          {
            label: "目標温度 ℃",
            data: [],
            borderColor: "#1565c0",
            backgroundColor: "rgba(21, 101, 192, 0.12)",
            tension: 0.2
          }
        ]
      },
      options: {
        responsive: true,
        animation: false,
        scales: {
          y: {
            min: 8,
            max: 40,
            title: { display: true, text: "温度 ℃" }
          }
        }
      }
    });

    const pwmChart = new Chart(document.getElementById("pwmChart"), {
      type: "line",
      data: {
        labels: [],
        datasets: [
          {
            label: "ペルチェPWM値",
            data: [],
            borderColor: "#2e7d32",
            backgroundColor: "rgba(46, 125, 50, 0.12)",
            tension: 0.2
          }
        ]
      },
      options: {
        responsive: true,
        animation: false,
        scales: {
          y: {
            min: 0,
            max: 255,
            title: { display: true, text: "PWM値" }
          }
        }
      }
    });

    function formData(extra = {}) {
      return {
        port: document.getElementById("port").value,
        target_temp: document.getElementById("targetTemp").value,
        min_temp: document.getElementById("minTemp").value,
        max_temp: document.getElementById("maxTemp").value,
        start_time: document.getElementById("startTime").value,
        cooling_minutes: document.getElementById("coolingMinutes").value,
        csv_file: document.getElementById("csvFile").value,
        ...extra
      };
    }

    async function postJson(url, payload = {}) {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const data = await response.json();
      document.getElementById("message").textContent = data.message || data.error || "";
      if (!response.ok) {
        document.getElementById("message").style.color = "#c62828";
      } else {
        document.getElementById("message").style.color = "#1f7a3f";
      }
      return data;
    }

    function connectSerial() {
      postJson("/connect", formData());
    }

    function sendSettings() {
      postJson("/send_settings", formData());
    }

    function scheduleStart() {
      postJson("/schedule_start", formData());
    }

    function startNow() {
      postJson("/start_now", formData());
    }

    function stopControl() {
      postJson("/stop", {});
    }

    function setTarget() {
      postJson("/set_target", formData());
    }

    function sendSimple(url) {
      postJson(url, {});
    }

    function showValue(value, suffix = "") {
      if (value === null || value === undefined || value === "") {
        return "-";
      }
      return `${value}${suffix}`;
    }

    async function updateStatus() {
      const response = await fetch("/api/status");
      const data = await response.json();

      document.getElementById("connected").textContent = `${data.pc_state} ${data.port ? "(" + data.port + ")" : ""}`;
      document.getElementById("currentTemp").textContent = showValue(data.current_temp, " ℃");
      document.getElementById("targetTempStatus").textContent = showValue(data.target_temp, " ℃");
      document.getElementById("heartRate").textContent = showValue(data.heart_rate, " bpm");
      document.getElementById("pwmValue").textContent = showValue(data.pwm_value);
      document.getElementById("arduinoState").textContent = showValue(data.arduino_state);
      document.getElementById("error").textContent = showValue(data.error);
      document.getElementById("lastReceived").textContent = showValue(data.last_received);
      document.getElementById("logging").textContent = data.logging ? `保存中 (${data.csv_file})` : "停止中";
      document.getElementById("scheduleState").textContent = showValue(data.schedule_state);
      document.getElementById("scheduledStart").textContent = showValue(data.scheduled_start);
      document.getElementById("scheduledEnd").textContent = showValue(data.scheduled_end);

      const warning = document.getElementById("warning");
      if (data.warning) {
        warning.style.display = "block";
        warning.textContent = data.warning;
      } else {
        warning.style.display = "none";
        warning.textContent = "";
      }
    }

    async function updateHistory() {
      const response = await fetch("/api/history");
      const data = await response.json();

      const labels = data.map(row => row.timestamp.split(" ").pop());

      temperatureChart.data.labels = labels;
      temperatureChart.data.datasets[0].data = data.map(row => row.current_temp);
      temperatureChart.data.datasets[1].data = data.map(row => row.target_temp);
      temperatureChart.update();

      pwmChart.data.labels = labels;
      pwmChart.data.datasets[0].data = data.map(row => row.pwm_value);
      pwmChart.update();
    }

    async function refresh() {
      await updateStatus();
      await updateHistory();
    }

    refresh();
    setInterval(refresh, 1000);
  </script>
</body>
</html>
"""


def now_text():
    """CSVや画面表示で使う現在時刻文字列を返します。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def set_message(message, warning=None):
    """Web画面に表示する通常メッセージと警告を更新します。"""
    with state_lock:
        app_state["message"] = message
        if warning is not None:
            app_state["warning"] = warning


def update_pc_state(pc_state):
    """PC側で管理する状態を更新します。"""
    with state_lock:
        app_state["pc_state"] = pc_state


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
        csv_file_initialized = os.path.exists(csv_file_path) and os.path.getsize(csv_file_path) > 0

    with state_lock:
        app_state["csv_file"] = csv_file_path
        app_state["logging"] = True

    return csv_file_path


def append_csv(row):
    """STATUSを受信するたびにCSVへ1行追記します。"""
    global csv_file_path, csv_file_initialized

    if csv_file_path is None:
        configure_csv_file("")

    with csv_lock:
        needs_header = not csv_file_initialized
        with open(csv_file_path, "a", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            if needs_header:
                writer.writerow([
                    "timestamp",
                    "current_temp",
                    "target_temp",
                    "heart_rate",
                    "pwm_value",
                    "state",
                    "error",
                ])
                csv_file_initialized = True

            writer.writerow([
                row["timestamp"],
                row["current_temp"],
                row["target_temp"],
                row["heart_rate"],
                row["pwm_value"],
                row["state"],
                row["error"],
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
    """最低温度 < 目標温度 < 最高温度の関係を確認します。"""
    target = validate_temperature_value(target_temp, "目標温度")
    minimum = validate_temperature_value(min_temp, "最低温度")
    maximum = validate_temperature_value(max_temp, "最高温度")

    if not (minimum < target < maximum):
        raise ValueError("最低温度 < 目標温度 < 最高温度 になるように設定してください。")

    return target, minimum, maximum


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


def parse_status(line):
    """
    Arduinoから来たSTATUS行を解析します。
    形式: STATUS,currentTemp,targetTemp,heartRate,pwmValue,state,error
    """
    parts = line.strip().split(",")
    if len(parts) != 7 or parts[0] != "STATUS":
        raise ValueError("STATUSの形式が正しくありません。")

    return {
        "timestamp": now_text(),
        "current_temp": float(parts[1]),
        "target_temp": float(parts[2]),
        "heart_rate": int(float(parts[3])),
        "pwm_value": int(float(parts[4])),
        "state": parts[5],
        "error": parts[6],
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
        app_state["warning"] = "" if row["error"] == "NONE" else f"Arduino警告: {row['error']}"

        if row["error"] != "NONE":
            app_state["pc_state"] = "ERROR"
        elif row["state"] == "RUNNING":
            app_state["pc_state"] = "RUNNING"
        elif app_state["pc_state"] not in ("SCHEDULED", "DISCONNECTED"):
            app_state["pc_state"] = "CONNECTED"

        history.append(row)

    append_csv(row)


def handle_error(line):
    """ERROR行を受け取ったとき、画面に赤い警告を出します。"""
    error_text = line.strip()
    with state_lock:
        app_state["pc_state"] = "ERROR"
        app_state["error"] = error_text
        app_state["warning"] = f"Arduino警告: {error_text}"
        app_state["last_received"] = now_text()


def serial_reader_loop():
    """別スレッドでArduinoからの受信を監視し続けます。"""
    global reader_running

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

            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            if line == "READY":
                with state_lock:
                    app_state["pc_state"] = "CONNECTED"
                    app_state["connected"] = True
                    app_state["arduino_state"] = "READY"
                    app_state["last_received"] = now_text()
                    app_state["warning"] = ""
            elif line.startswith("STATUS,"):
                handle_status(line)
            elif line.startswith("ERROR,"):
                handle_error(line)
            else:
                set_message(f"未対応の受信データ: {line}")

        except Exception as exc:
            set_message(f"シリアル受信エラー: {exc}", warning=f"シリアル受信エラー: {exc}")
            time.sleep(1)


def ensure_reader_thread():
    """シリアル受信用スレッドを必要に応じて起動します。"""
    global reader_thread, reader_running

    if reader_thread is not None and reader_thread.is_alive():
        return

    reader_running = True
    reader_thread = threading.Thread(target=serial_reader_loop, daemon=True)
    reader_thread.start()


def parse_start_datetime(start_time_text):
    """
    HH:MM形式の開始時刻をdatetimeに変換します。
    すでに今日の時刻を過ぎている場合は、翌日の同じ時刻にします。
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


def schedule_worker(start_at, cooling_minutes):
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
        send_command("START")
        with state_lock:
            app_state["pc_state"] = "RUNNING"
            app_state["schedule_state"] = "冷却中"
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
    return render_template_string(HTML)


@app.route("/connect", methods=["POST"])
def connect():
    global serial_port

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

        ensure_reader_thread()

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


@app.route("/send_settings", methods=["POST"])
def send_settings():
    data = json_payload()

    try:
        target, minimum, maximum = validate_temperature_set(
            data.get("target_temp"),
            data.get("min_temp"),
            data.get("max_temp"),
        )
        configure_csv_file(data.get("csv_file"))
        send_command(f"SET,{target:.1f},{minimum:.1f},{maximum:.1f}")

        with state_lock:
            app_state["target_temp"] = target
            app_state["warning"] = ""

        return jsonify({"message": "設定をArduinoへ送信しました。"})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"設定送信に失敗しました: {exc}"}), 500


@app.route("/start_now", methods=["POST"])
def start_now():
    data = json_payload()

    try:
        configure_csv_file(data.get("csv_file"))
        schedule_cancel_event.set()
        send_command("START")

        with state_lock:
            app_state["pc_state"] = "RUNNING"
            app_state["schedule_state"] = "手動開始"
            app_state["scheduled_start"] = now_text()
            app_state["scheduled_end"] = ""
            app_state["warning"] = ""

        return jsonify({"message": "STARTを送信しました。"})
    except Exception as exc:
        return jsonify({"error": f"START送信に失敗しました: {exc}"}), 500


@app.route("/schedule_start", methods=["POST"])
def schedule_start():
    global schedule_thread

    data = json_payload()

    try:
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
            args=(start_at, cooling_minutes),
            daemon=True,
        )
        schedule_thread.start()

        return jsonify({
            "message": f"{start_at.strftime('%Y-%m-%d %H:%M:%S')} にSTARTを送信するよう予約しました。"
        })
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
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
        return jsonify({"error": str(exc)}), 400
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


if __name__ == "__main__":
    print("Arduino温度制御Webサーバーを起動します。")
    print("ブラウザで http://127.0.0.1:5000 を開いてください。")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
