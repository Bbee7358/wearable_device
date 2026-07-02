let temperatureChart = null;
let pwmChart = null;
let targetUpdateTimer = null;
let targetUpdateInFlight = false;
let latestCurrentTemp = null;
let markerDragActive = false;

let serialPort = null;
let serialReader = null;
let serialWriter = null;
let serialOutputClosed = null;
let readLoopActive = false;
let autoConnectInFlight = false;
let receiveBuffer = "";
let loggingEnabled = false;
let csvRows = [];
let historyCount = 0;
let scheduleTimer = null;
let scheduleState = null;

const BAUD_RATE = 115200;
const TARGET_UPDATE_DELAY_MS = 450;
const SCHEDULE_STEP_MS = 20_000;
const HISTORY_LIMIT = 720;
const CSV_HEADER = ["timestamp", "peltier_temp", "heart_rate"];
const GAUGE_MIN_TEMP = 10;
const GAUGE_MAX_TEMP = 37;
const GAUGE_GEOMETRY = {
  cx: 160,
  cy: 186,
  radius: 108,
  strokeWidth: 56,
  markerLength: 30,
  markerWidth: 34,
  markerGap: 5
};

const appState = {
  pc_state: "DISCONNECTED",
  connected: false,
  port: "",
  current_temp: null,
  target_temp: 25,
  heart_rate: null,
  pwm_value: null,
  arduino_state: "",
  error: "NONE",
  last_received: "",
  logging: false,
  schedule_state: "未設定",
  scheduled_start: "",
  scheduled_arrival: "",
  scheduled_end: "",
  ramp_minutes: "",
  message: "",
  warning: "",
  status_count: 0,
  parse_error_count: 0,
  waiting_for_status: false
};

function setText(id, text) {
  const element = document.getElementById(id);
  if (element) {
    element.textContent = text;
  }
}

function setMessage(message, isError = false) {
  appState.message = message;
  const element = document.getElementById("message");
  if (element) {
    element.textContent = message;
    element.style.color = isError ? "#F55353" : "#143F6B";
  }
}

function setWarning(message = "") {
  appState.warning = message;
  const element = document.getElementById("warning");
  if (!element) {
    return;
  }
  element.textContent = message;
  element.style.display = message ? "block" : "none";
}

function initializeCharts() {
  if (!window.Chart) {
    setWarning("Chart.jsを読み込めませんでした。");
    return;
  }

  const commonOptions = {
    responsive: true,
    animation: false,
    maintainAspectRatio: false,
    interaction: { intersect: false, mode: "index" },
    plugins: {
      legend: {
        labels: {
          boxWidth: 10,
          color: "#143F6B",
          font: { weight: "700" }
        }
      }
    }
  };

  temperatureChart = new Chart(document.getElementById("temperatureChart"), {
    type: "line",
    data: {
      labels: [],
      datasets: [
        {
          label: "現在温度 ℃",
          data: [],
          borderColor: "#F55353",
          backgroundColor: "rgba(245, 83, 83, 0.12)",
          pointRadius: 0,
          borderWidth: 2,
          tension: 0.25
        },
        {
          label: "目標温度 ℃",
          data: [],
          borderColor: "#143F6B",
          backgroundColor: "rgba(20, 63, 107, 0.10)",
          pointRadius: 0,
          borderWidth: 2,
          tension: 0
        }
      ]
    },
    options: {
      ...commonOptions,
      scales: {
        x: { ticks: { maxTicksLimit: 12, color: "#60717f" } },
        y: {
          min: 8,
          max: 40,
          title: { display: true, text: "温度 ℃", color: "#143F6B" },
          ticks: { color: "#60717f" }
        }
      }
    }
  });

  pwmChart = new Chart(document.getElementById("pwmChart"), {
    type: "line",
    data: {
      labels: [],
      datasets: [
        {
          label: "ペルチェPWM値",
          data: [],
          borderColor: "#FEB139",
          backgroundColor: "rgba(254, 177, 57, 0.18)",
          pointRadius: 0,
          borderWidth: 2,
          tension: 0.2,
          fill: true
        }
      ]
    },
    options: {
      ...commonOptions,
      scales: {
        x: { ticks: { maxTicksLimit: 12, color: "#60717f" } },
        y: {
          min: 0,
          max: 255,
          title: { display: true, text: "PWM値", color: "#143F6B" },
          ticks: { color: "#60717f" }
        }
      }
    }
  });
}

function formData(extra = {}) {
  return {
    target_temp: document.getElementById("targetTemp").value,
    min_temp: document.getElementById("minTemp").value,
    max_temp: document.getElementById("maxTemp").value,
    arrival_time: document.getElementById("arrivalTime")?.value,
    ramp_minutes: document.getElementById("rampMinutes")?.value,
    hold_minutes: document.getElementById("holdMinutes")?.value,
    csv_file: document.getElementById("csvFile").value,
    ...extra
  };
}

function assertWebSerialSupported() {
  if (!("serial" in navigator)) {
    throw new Error("このブラウザはWeb Serial APIに対応していません。ChromeまたはEdgeでHTTPSのURLを開いてください。");
  }
}

function validateTemperature(value, name) {
  const number = Number.parseFloat(value);
  if (!Number.isFinite(number)) {
    throw new Error(`${name}は数値で入力してください。`);
  }
  if (number < GAUGE_MIN_TEMP || number > GAUGE_MAX_TEMP) {
    throw new Error(`${name}は${GAUGE_MIN_TEMP.toFixed(1)}〜${GAUGE_MAX_TEMP.toFixed(1)}℃で入力してください。`);
  }
  return Math.round(number * 10) / 10;
}

function getSettings() {
  const data = formData();
  return {
    target: validateTemperature(data.target_temp, "目標温度"),
    min: validateTemperature(data.min_temp, "最低温度"),
    max: validateTemperature(data.max_temp, "最高温度")
  };
}

function ensureConnected() {
  if (!serialPort || !serialWriter) {
    throw new Error("Arduinoに接続してください。");
  }
}

async function sendCommand(command) {
  ensureConnected();
  await serialWriter.write(`${command}\n`);
  setMessage(`送信: ${command}`);
}

function describeSerialPort(port) {
  if (!port || !port.getInfo) {
    return "Web Serial";
  }

  const info = port.getInfo();
  const parts = [];
  if (info.usbVendorId !== undefined) {
    parts.push(`VID ${info.usbVendorId.toString(16).padStart(4, "0")}`);
  }
  if (info.usbProductId !== undefined) {
    parts.push(`PID ${info.usbProductId.toString(16).padStart(4, "0")}`);
  }
  return parts.length > 0 ? parts.join(" / ") : "Web Serial";
}

async function openSerialPort(port, message) {
  if (serialPort) {
    setMessage("すでに接続しています。");
    return;
  }

  serialPort = port;
  await serialPort.open({ baudRate: BAUD_RATE });

  const textEncoder = new TextEncoderStream();
  serialOutputClosed = textEncoder.readable.pipeTo(serialPort.writable);
  serialWriter = textEncoder.writable.getWriter();

  readLoopActive = true;
  receiveBuffer = "";
  appState.connected = true;
  appState.pc_state = "CONNECTED";
  appState.port = describeSerialPort(serialPort);
  setWarning("");
  updateStatus();
  readSerialLoop();
  setMessage(message);
}

async function autoConnectSerial() {
  try {
    assertWebSerialSupported();
    if (serialPort || autoConnectInFlight) {
      return;
    }

    autoConnectInFlight = true;
    setText("serialStatusText", "自動接続中...");
    const ports = await navigator.serial.getPorts();
    if (ports.length === 0) {
      setMessage("初回は接続ボタンからArduinoを選択してください。次回から自動接続します。");
      return;
    }

    await openSerialPort(ports[0], "許可済みポートに自動接続しました。");
  } catch (error) {
    await closeSerialPort();
    setWarning(error.message);
    setMessage(error.message, true);
  } finally {
    autoConnectInFlight = false;
    updateStatus();
  }
}

async function connectSerial() {
  try {
    assertWebSerialSupported();
    if (serialPort) {
      setMessage("すでに接続しています。");
      return;
    }

    const port = await navigator.serial.requestPort();
    await openSerialPort(port, "Arduinoに接続しました。次回から自動接続します。");
  } catch (error) {
    await closeSerialPort();
    setWarning(error.message);
    setMessage(error.message, true);
  }
}

async function closeSerialPort() {
  readLoopActive = false;
  cancelSchedule(false);

  try {
    if (serialReader) {
      await serialReader.cancel();
      serialReader.releaseLock();
    }
  } catch (error) {
    // Reader may already be closed by the browser.
  }
  serialReader = null;

  try {
    if (serialWriter) {
      await serialWriter.close();
      serialWriter.releaseLock();
    }
  } catch (error) {
    // Writer may already be closed by the browser.
  }
  serialWriter = null;

  try {
    if (serialOutputClosed) {
      await serialOutputClosed.catch(() => {});
    }
  } catch (error) {
    // Ignore stream shutdown noise.
  }
  serialOutputClosed = null;

  try {
    if (serialPort) {
      await serialPort.close();
    }
  } catch (error) {
    // Closing an already closed port is harmless for this UI.
  }
  serialPort = null;

  appState.connected = false;
  appState.pc_state = "DISCONNECTED";
  appState.port = "";
  appState.arduino_state = "";
  appState.waiting_for_status = false;
}

async function disconnectSerial() {
  await closeSerialPort();
  setMessage("切断しました。");
  updateStatus();
}

async function readSerialLoop() {
  while (serialPort && serialPort.readable && readLoopActive) {
    const decoder = new TextDecoderStream();
    const readableClosed = serialPort.readable.pipeTo(decoder.writable);
    serialReader = decoder.readable.getReader();

    try {
      while (readLoopActive) {
        const { value, done } = await serialReader.read();
        if (done) {
          break;
        }
        if (value) {
          handleSerialChunk(value);
        }
      }
    } catch (error) {
      if (readLoopActive) {
        setWarning(`受信エラー: ${error.message}`);
      }
    } finally {
      try {
        serialReader.releaseLock();
      } catch (error) {
        // Ignore stale locks.
      }
      serialReader = null;
    }

    try {
      await readableClosed.catch(() => {});
    } catch (error) {
      // Ignore stream shutdown noise.
    }
  }
}

function handleSerialChunk(chunk) {
  receiveBuffer += chunk;
  const lines = receiveBuffer.split(/\r?\n/);
  receiveBuffer = lines.pop() || "";

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (line) {
      handleSerialLine(line);
    }
  }
}

function handleSerialLine(line) {
  appState.last_received = timestampText();

  if (line === "READY") {
    appState.arduino_state = "READY";
    setMessage("Arduino READYを受信しました。");
    updateStatus();
    return;
  }

  if (line.startsWith("OK,")) {
    setMessage(`受信: ${line}`);
    updateStatus();
    return;
  }

  if (line.startsWith("ERROR,")) {
    const error = line.split(",")[1] || "UNKNOWN";
    appState.error = error;
    appState.arduino_state = error === "NONE" ? appState.arduino_state : "ERROR";
    setWarning(`Arduinoエラー: ${line}`);
    setMessage(`Arduinoエラー: ${line}`, true);
    updateStatus();
    return;
  }

  if (!line.startsWith("STATUS,")) {
    appState.parse_error_count += 1;
    return;
  }

  const fields = line.split(",");
  if (fields.length < 7) {
    appState.parse_error_count += 1;
    return;
  }

  const currentTemp = Number.parseFloat(fields[1]);
  const targetTemp = Number.parseFloat(fields[2]);
  const heartRate = Number.parseInt(fields[3], 10);
  const pwmValue = Number.parseInt(fields[4], 10);

  appState.current_temp = Number.isFinite(currentTemp) ? currentTemp : appState.current_temp;
  appState.target_temp = Number.isFinite(targetTemp) ? targetTemp : appState.target_temp;
  appState.heart_rate = Number.isFinite(heartRate) ? heartRate : appState.heart_rate;
  appState.pwm_value = Number.isFinite(pwmValue) ? pwmValue : appState.pwm_value;
  appState.arduino_state = fields[5] || "";
  appState.error = fields[6] || "NONE";
  appState.status_count += 1;
  appState.waiting_for_status = false;
  latestCurrentTemp = appState.current_temp;

  if (loggingEnabled) {
    csvRows.push([
      dateTimeText(),
      appState.current_temp,
      appState.heart_rate
    ]);
  }

  appendCharts(appState);
  updateStatus();
}

async function sendSettings() {
  try {
    const settings = getSettings();
    await sendCommand(`SET,${settings.target.toFixed(1)},${settings.min.toFixed(1)},${settings.max.toFixed(1)}`);
    appState.target_temp = settings.target;
    updateTemperatureGauge(latestCurrentTemp, settings.target);
    updateStatus();
  } catch (error) {
    setWarning(error.message);
  }
}

async function startNow() {
  try {
    const settings = getSettings();
    cancelSchedule(false);
    csvRows = [];
    loggingEnabled = true;
    appState.logging = true;
    appState.waiting_for_status = true;
    appState.target_temp = settings.target;
    await sendCommand(`SET,${settings.target.toFixed(1)},${settings.min.toFixed(1)},${settings.max.toFixed(1)}`);
    await sendCommand("START");
    setWarning("");
    updateStatus();
  } catch (error) {
    appState.waiting_for_status = false;
    setWarning(error.message);
    setMessage(error.message, true);
    updateStatus();
  }
}

async function stopControl() {
  try {
    cancelSchedule(false);
    await sendCommand("STOP");
    loggingEnabled = false;
    appState.logging = false;
    updateStatus();
  } catch (error) {
    setWarning(error.message);
  }
}

async function setTarget() {
  try {
    const target = validateTemperature(document.getElementById("targetTemp").value, "目標温度");
    await sendCommand(`SET_TARGET,${target.toFixed(1)}`);
    appState.target_temp = target;
    updateStatus();
  } catch (error) {
    setWarning(error.message);
  }
}

function isValidTargetTemperature(value) {
  const number = Number.parseFloat(value);
  return Number.isFinite(number) && number >= GAUGE_MIN_TEMP && number <= GAUGE_MAX_TEMP;
}

function scheduleTargetUpdate() {
  const targetInput = document.getElementById("targetTemp");
  if (!targetInput || !isValidTargetTemperature(targetInput.value)) {
    return;
  }

  window.clearTimeout(targetUpdateTimer);
  targetUpdateTimer = window.setTimeout(async () => {
    if (targetUpdateInFlight) {
      scheduleTargetUpdate();
      return;
    }

    targetUpdateInFlight = true;
    try {
      await setTarget();
    } finally {
      targetUpdateInFlight = false;
    }
  }, TARGET_UPDATE_DELAY_MS);
}

async function sendSimple(url) {
  const commandByUrl = {
    "/reset": "RESET",
    "/vib_on": "VIB_ON",
    "/vib_off": "VIB_OFF"
  };

  try {
    const command = commandByUrl[url];
    if (!command) {
      throw new Error(`未対応の操作です: ${url}`);
    }
    await sendCommand(command);
    if (command === "RESET") {
      appState.error = "NONE";
      appState.arduino_state = "WAIT";
      setWarning("");
    }
    updateStatus();
  } catch (error) {
    setWarning(error.message);
  }
}

function sanitizeCsvFilename(filename) {
  const safeName = (filename || "").trim().replace(/[/\\?%*:|"<>]/g, "_") || `experiment_${Date.now()}.csv`;
  return safeName.toLowerCase().endsWith(".csv") ? safeName : `${safeName}.csv`;
}

function downloadCsv() {
  const filename = sanitizeCsvFilename(document.getElementById("csvFile").value);
  const lines = [
    CSV_HEADER.join(","),
    ...csvRows.map(row => row.map(value => `"${String(value ?? "").replace(/"/g, '""')}"`).join(","))
  ];
  const blob = new Blob([`${lines.join("\n")}\n`], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function defaultArrivalTime() {
  const date = new Date(Date.now() + 30 * 60 * 1000);
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

function updateCurrentTimeLabel() {
  const label = document.getElementById("currentTimeLabel");
  if (!label) {
    return;
  }

  const now = new Date();
  label.textContent = `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}:${String(now.getSeconds()).padStart(2, "0")}`;
}

function showValue(value, suffix = "") {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  return `${value}${suffix}`;
}

function toTemperatureNumber(value, fallback = GAUGE_MIN_TEMP) {
  const number = Number.parseFloat(value);
  return Number.isFinite(number) ? number : fallback;
}

function temperatureToPercent(value) {
  const temperature = toTemperatureNumber(value);
  const percent = ((temperature - GAUGE_MIN_TEMP) / (GAUGE_MAX_TEMP - GAUGE_MIN_TEMP)) * 100;
  return Math.min(100, Math.max(0, percent));
}

function temperatureToAngle(value) {
  return Math.PI - (temperatureToPercent(value) / 100) * Math.PI;
}

function angleToTemperature(angle) {
  const clampedAngle = Math.min(Math.PI, Math.max(0, angle));
  const percent = (Math.PI - clampedAngle) / Math.PI;
  const temperature = GAUGE_MIN_TEMP + percent * (GAUGE_MAX_TEMP - GAUGE_MIN_TEMP);
  return Math.round(temperature * 10) / 10;
}

function formatInputTemperature(value) {
  const rounded = Math.round(value * 10) / 10;
  return rounded.toFixed(1);
}

function pointOnGauge(angle, radiusOffset = 0) {
  const radius = GAUGE_GEOMETRY.radius + radiusOffset;
  return {
    x: GAUGE_GEOMETRY.cx + Math.cos(angle) * radius,
    y: GAUGE_GEOMETRY.cy - Math.sin(angle) * radius
  };
}

function arcPath(startAngle, endAngle) {
  const start = pointOnGauge(startAngle);
  const end = pointOnGauge(endAngle);
  const sweepDegrees = Math.abs((startAngle - endAngle) * 180 / Math.PI);
  const largeArc = sweepDegrees > 180 ? 1 : 0;

  return [
    `M ${start.x.toFixed(2)} ${start.y.toFixed(2)}`,
    `A ${GAUGE_GEOMETRY.radius} ${GAUGE_GEOMETRY.radius} 0 ${largeArc} 1 ${end.x.toFixed(2)} ${end.y.toFixed(2)}`
  ].join(" ");
}

function markerPointsForAngle(angle) {
  const normal = { x: Math.cos(angle), y: -Math.sin(angle) };
  const tangent = { x: -normal.y, y: normal.x };
  const tip = pointOnGauge(angle, (GAUGE_GEOMETRY.strokeWidth / 2) + 2);
  const baseCenter = {
    x: tip.x + normal.x * (GAUGE_GEOMETRY.markerLength + GAUGE_GEOMETRY.markerGap),
    y: tip.y + normal.y * (GAUGE_GEOMETRY.markerLength + GAUGE_GEOMETRY.markerGap)
  };
  const halfWidth = GAUGE_GEOMETRY.markerWidth / 2;
  const left = {
    x: baseCenter.x + tangent.x * halfWidth,
    y: baseCenter.y + tangent.y * halfWidth
  };
  const right = {
    x: baseCenter.x - tangent.x * halfWidth,
    y: baseCenter.y - tangent.y * halfWidth
  };

  return [tip, left, right]
    .map(point => `${point.x.toFixed(2)},${point.y.toFixed(2)}`)
    .join(" ");
}

function markerHitPointForAngle(angle) {
  const normal = { x: Math.cos(angle), y: -Math.sin(angle) };
  const tip = pointOnGauge(angle, (GAUGE_GEOMETRY.strokeWidth / 2) + 2);
  return {
    x: tip.x + normal.x * (GAUGE_GEOMETRY.markerGap + GAUGE_GEOMETRY.markerLength * 0.62),
    y: tip.y + normal.y * (GAUGE_GEOMETRY.markerGap + GAUGE_GEOMETRY.markerLength * 0.62)
  };
}

function angleFromSvgPoint(point) {
  const rawAngle = Math.atan2(GAUGE_GEOMETRY.cy - point.y, point.x - GAUGE_GEOMETRY.cx);
  return Math.min(Math.PI, Math.max(0, rawAngle));
}

function svgPointFromPointerEvent(event) {
  const svg = document.querySelector(".gauge-svg");
  if (!svg) {
    return null;
  }

  const rect = svg.getBoundingClientRect();
  const viewBox = svg.viewBox.baseVal;
  return {
    x: ((event.clientX - rect.left) / rect.width) * viewBox.width + viewBox.x,
    y: ((event.clientY - rect.top) / rect.height) * viewBox.height + viewBox.y
  };
}

function setTargetFromPointer(event) {
  const targetInput = document.getElementById("targetTemp");
  const point = svgPointFromPointerEvent(event);
  if (!targetInput || !point) {
    return;
  }

  const temperature = angleToTemperature(angleFromSvgPoint(point));
  targetInput.value = formatInputTemperature(temperature);
  updateTemperatureGauge(latestCurrentTemp, targetInput.value);
  scheduleTargetUpdate();
}

function formatGaugeTemperature(value) {
  const temperature = toTemperatureNumber(value, null);
  if (temperature === null) {
    return "-";
  }
  const rounded = Math.round(temperature * 10) / 10;
  return `${rounded.toFixed(1)}℃`;
}

function updateTemperatureGauge(currentTemp, targetTemp) {
  const gauge = document.getElementById("temperatureGauge");
  const track = document.getElementById("gaugeTrack");
  const currentArc = document.getElementById("currentTempArc");
  const currentLabel = document.getElementById("gaugeCurrentTemp");
  const marker = document.getElementById("targetMarker");
  const markerHit = document.getElementById("targetMarkerHit");
  const targetInput = document.getElementById("targetTemp");

  if (!gauge || !track || !currentArc || !currentLabel || !marker || !markerHit || !targetInput) {
    return;
  }

  const currentPercent = temperatureToPercent(currentTemp);
  const currentAngle = temperatureToAngle(currentTemp);
  const targetValue = targetInput === document.activeElement || targetTemp === null || targetTemp === undefined
    ? targetInput.value
    : targetTemp;
  const targetAngle = temperatureToAngle(targetValue);
  const currentLabelPoint = {
    x: GAUGE_GEOMETRY.cx,
    y: GAUGE_GEOMETRY.cy - GAUGE_GEOMETRY.radius + GAUGE_GEOMETRY.strokeWidth * 0.1
  };
  const targetLabelPoint = {
    x: GAUGE_GEOMETRY.cx,
    y: GAUGE_GEOMETRY.cy
  };

  track.setAttribute("d", arcPath(Math.PI, 0));
  currentArc.setAttribute("d", currentPercent <= 0 ? "" : arcPath(Math.PI, currentAngle));
  marker.setAttribute("points", markerPointsForAngle(targetAngle));
  const hitPoint = markerHitPointForAngle(targetAngle);
  markerHit.setAttribute("cx", hitPoint.x.toFixed(2));
  markerHit.setAttribute("cy", hitPoint.y.toFixed(2));
  currentLabel.textContent = formatGaugeTemperature(currentTemp);
  if (targetInput !== document.activeElement) {
    targetInput.value = formatInputTemperature(toTemperatureNumber(targetValue, GAUGE_MIN_TEMP));
  }
  const gaugeScale = gauge.clientWidth / 320;
  gauge.style.setProperty("--gauge-scale", gaugeScale.toFixed(4));
  gauge.style.setProperty("--current-label-x", `${currentLabelPoint.x * gaugeScale}px`);
  gauge.style.setProperty("--current-label-y", `${currentLabelPoint.y * gaugeScale}px`);
  gauge.style.setProperty("--current-label-width", `${240 * gaugeScale}px`);
  gauge.style.setProperty("--target-label-x", `${targetLabelPoint.x * gaugeScale}px`);
  gauge.style.setProperty("--target-label-y", `${targetLabelPoint.y * gaugeScale}px`);
}

function setupGaugeDrag() {
  const marker = document.getElementById("targetMarker");
  const markerHit = document.getElementById("targetMarkerHit");
  const svg = document.querySelector(".gauge-svg");
  if (!marker || !markerHit || !svg) {
    return;
  }

  const startDrag = event => {
    event.preventDefault();
    markerDragActive = true;
    if (event.pointerId !== undefined && event.currentTarget.setPointerCapture) {
      event.currentTarget.setPointerCapture(event.pointerId);
    }
    setTargetFromPointer(event);
  };

  const moveDrag = event => {
    if (markerDragActive) {
      event.preventDefault();
      setTargetFromPointer(event);
    }
  };

  const endDrag = event => {
    markerDragActive = false;
    if (event.pointerId !== undefined && event.currentTarget.releasePointerCapture) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    scheduleTargetUpdate();
  };

  const cancelDrag = event => {
    markerDragActive = false;
    if (event.pointerId !== undefined && event.currentTarget.releasePointerCapture) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  };

  for (const handle of [marker, markerHit]) {
    handle.addEventListener("pointerdown", startDrag);
    handle.addEventListener("mousedown", startDrag);
  }

  document.addEventListener("pointermove", moveDrag);
  document.addEventListener("mousemove", moveDrag);
  document.addEventListener("pointerup", endDrag);
  document.addEventListener("mouseup", endDrag);
  document.addEventListener("pointercancel", cancelDrag);
}

function describeState(data) {
  if (!("serial" in navigator)) {
    return "Web Serial APIに対応したChromeまたはEdgeで開いてください。";
  }
  if (!data.connected) {
    return "未接続です。接続ボタンからArduinoのUSBシリアルを選択してください。";
  }
  if (data.arduino_state === "ERROR" || data.pc_state === "ERROR") {
    const error = data.error && data.error !== "NONE" ? data.error : "RESET_REQUIRED";
    return `安全停止中です。${error} を確認し、RESETしてから再開してください。`;
  }
  if (data.waiting_for_status) {
    return "START送信後、ArduinoからのSTATUSを待っています。";
  }
  if (data.arduino_state === "RUNNING") {
    if ((data.pwm_value || 0) > 0) {
      return "冷却中です。目標温度へ近づけるためペルチェ出力を調整しています。";
    }
    return "制御中です。現在は目標付近のため冷却を弱めています。";
  }
  if (data.arduino_state === "WAIT") {
    return "待機中です。設定後に今すぐ開始できます。";
  }
  if (data.arduino_state === "STOPPED") {
    return "停止中です。開始すると制御を再開します。";
  }
  if (data.arduino_state === "READY") {
    return "Arduinoを認識しました。STATUS受信待ちです。";
  }
  return "状態を確認しています。";
}

function updateStatus() {
  const data = appState;
  setText("connected", `${data.pc_state}${data.port ? " / " + data.port : ""}`);
  setText("serialStatusText", data.connected ? `${data.pc_state} / ${data.arduino_state || "受信待ち"}` : "未接続");
  setText("autoReconnectState", "Web Serial");
  latestCurrentTemp = data.current_temp;
  setText("currentTemp", showValue(data.current_temp, " ℃"));
  setText("targetTempStatus", showValue(data.target_temp, " ℃"));
  setText("heartRate", showValue(data.heart_rate, " bpm"));
  setText("pwmValue", showValue(data.pwm_value));
  setText("arduinoState", data.arduino_state || "-");
  setText("error", data.error || "NONE");
  setText("lastReceived", data.last_received || "-");
  setText("logging", data.logging ? "収集中" : "停止中");
  setText("statusCount", data.status_count);
  setText("parseErrorCount", data.parse_error_count);
  setText("waitingForStatus", data.waiting_for_status ? "STATUS待ち" : "-");
  setText("stateSummary", describeState(data));
  setText("scheduleState", data.schedule_state || "未設定");
  setText("scheduleSummary", data.schedule_state || "未設定");
  setText("scheduledStart", data.scheduled_start || "-");
  setText("scheduledArrival", data.scheduled_arrival || "-");
  setText("scheduledEnd", data.scheduled_end || "-");
  setText("rampMinutesStatus", data.ramp_minutes || "-");
  updateTemperatureGauge(data.current_temp, data.target_temp);
}

function appendCharts(data) {
  if (!temperatureChart || !pwmChart) {
    return;
  }

  const label = new Date().toLocaleTimeString("ja-JP", { hour12: false });
  temperatureChart.data.labels.push(label);
  temperatureChart.data.datasets[0].data.push(data.current_temp);
  temperatureChart.data.datasets[1].data.push(data.target_temp);
  pwmChart.data.labels.push(label);
  pwmChart.data.datasets[0].data.push(data.pwm_value);
  historyCount += 1;

  if (historyCount > HISTORY_LIMIT) {
    temperatureChart.data.labels.shift();
    temperatureChart.data.datasets.forEach(dataset => dataset.data.shift());
    pwmChart.data.labels.shift();
    pwmChart.data.datasets[0].data.shift();
    historyCount = HISTORY_LIMIT;
  }

  temperatureChart.update("none");
  pwmChart.update("none");
}

function dateForTimeInput(value) {
  if (!value) {
    throw new Error("到達時刻を入力してください。");
  }
  const [hoursText, minutesText] = value.split(":");
  const hours = Number.parseInt(hoursText, 10);
  const minutes = Number.parseInt(minutesText, 10);
  if (!Number.isFinite(hours) || !Number.isFinite(minutes)) {
    throw new Error("到達時刻を正しく入力してください。");
  }

  const arrival = new Date();
  arrival.setHours(hours, minutes, 0, 0);
  if (arrival.getTime() <= Date.now()) {
    arrival.setDate(arrival.getDate() + 1);
  }
  return arrival;
}

function formatTime(date) {
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

function timestampText() {
  return new Date().toLocaleTimeString("ja-JP", { hour12: false });
}

function dateTimeText() {
  const date = new Date();
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")} ${timestampText()}`;
}

async function scheduleStart() {
  try {
    ensureConnected();
    const settings = getSettings();
    const data = formData();
    const rampMinutes = Math.max(0, Number.parseInt(data.ramp_minutes || "0", 10));
    const holdMinutes = Math.max(0, Number.parseInt(data.hold_minutes || "0", 10));
    const arrival = dateForTimeInput(data.arrival_time);
    const rampStart = new Date(arrival.getTime() - rampMinutes * 60 * 1000);
    const end = new Date(arrival.getTime() + holdMinutes * 60 * 1000);

    cancelSchedule(false);
    csvRows = [];
    loggingEnabled = true;
    appState.logging = true;
    appState.schedule_state = "予約中";
    appState.scheduled_start = formatTime(rampStart);
    appState.scheduled_arrival = formatTime(arrival);
    appState.scheduled_end = holdMinutes > 0 ? formatTime(end) : "-";
    appState.ramp_minutes = String(rampMinutes);
    scheduleState = {
      settings,
      rampMinutes,
      holdMinutes,
      rampStart,
      arrival,
      end,
      startTarget: toTemperatureNumber(latestCurrentTemp, appState.target_temp || settings.target),
      lastSentAt: 0,
      started: false,
      completed: false
    };
    updateStatus();
    tickSchedule();
    scheduleTimer = window.setInterval(tickSchedule, 1000);
  } catch (error) {
    setWarning(error.message);
    setMessage(error.message, true);
  }
}

async function tickSchedule() {
  if (!scheduleState) {
    return;
  }

  const now = Date.now();
  const state = scheduleState;

  try {
    if (now < state.rampStart.getTime()) {
      appState.schedule_state = `${formatTime(state.rampStart)}開始`;
      updateStatus();
      return;
    }

    if (!state.started) {
      state.started = true;
      appState.waiting_for_status = true;
      await sendCommand(`SET,${state.startTarget.toFixed(1)},${state.settings.min.toFixed(1)},${state.settings.max.toFixed(1)}`);
      await sendCommand("START");
    }

    const rampDurationMs = Math.max(1, state.rampMinutes * 60 * 1000);
    const progress = state.rampMinutes === 0
      ? 1
      : Math.min(1, Math.max(0, (now - state.rampStart.getTime()) / rampDurationMs));
    const nextTarget = state.startTarget + (state.settings.target - state.startTarget) * progress;

    if (now - state.lastSentAt >= SCHEDULE_STEP_MS || progress >= 1) {
      state.lastSentAt = now;
      await sendCommand(`SET_TARGET,${nextTarget.toFixed(1)}`);
      appState.target_temp = Math.round(nextTarget * 10) / 10;
    }

    if (progress < 1) {
      appState.schedule_state = "変化中";
      updateStatus();
      return;
    }

    appState.target_temp = state.settings.target;
    if (state.holdMinutes > 0 && now < state.end.getTime()) {
      appState.schedule_state = "維持中";
      updateStatus();
      return;
    }

    appState.schedule_state = "完了";
    state.completed = true;
    window.clearInterval(scheduleTimer);
    scheduleTimer = null;
    scheduleState = null;
    updateStatus();
  } catch (error) {
    setWarning(error.message);
    setMessage(error.message, true);
    cancelSchedule(false);
  }
}

function cancelSchedule(showMessage = true) {
  if (scheduleTimer) {
    window.clearInterval(scheduleTimer);
    scheduleTimer = null;
  }
  scheduleState = null;
  appState.schedule_state = "未設定";
  appState.scheduled_start = "";
  appState.scheduled_arrival = "";
  appState.scheduled_end = "";
  appState.ramp_minutes = "";
  if (showMessage) {
    setMessage("予約を解除しました。");
  }
  updateStatus();
}

function initializeControls() {
  document.getElementById("targetTemp").addEventListener("input", () => {
    updateTemperatureGauge(latestCurrentTemp, document.getElementById("targetTemp").value);
    scheduleTargetUpdate();
  });
  document.getElementById("minTemp").addEventListener("input", updateStatus);
  document.getElementById("maxTemp").addEventListener("input", updateStatus);
  const arrivalTime = document.getElementById("arrivalTime");
  if (arrivalTime && !arrivalTime.value) {
    arrivalTime.value = defaultArrivalTime();
  }
  setupGaugeDrag();
}

window.connectSerial = connectSerial;
window.disconnectSerial = disconnectSerial;
window.autoConnectSerial = autoConnectSerial;
window.sendSettings = sendSettings;
window.scheduleStart = scheduleStart;
window.cancelSchedule = cancelSchedule;
window.startNow = startNow;
window.stopControl = stopControl;
window.sendSimple = sendSimple;
window.downloadCsv = downloadCsv;
window.loadSerialPorts = () => {};

document.addEventListener("DOMContentLoaded", () => {
  initializeCharts();
  initializeControls();
  updateCurrentTimeLabel();
  window.setInterval(updateCurrentTimeLabel, 1000);
  updateStatus();

  if (!("serial" in navigator)) {
    setWarning("Web Serial APIに対応したChromeまたはEdgeでHTTPSのURLを開いてください。");
    return;
  }

  navigator.serial.addEventListener("connect", () => {
    autoConnectSerial();
  });
  navigator.serial.addEventListener("disconnect", event => {
    if (event.target === serialPort) {
      closeSerialPort().then(() => {
        setWarning("Arduinoとの接続が切れました。USB接続を確認してください。");
        updateStatus();
      });
    }
  });
  autoConnectSerial();
});
