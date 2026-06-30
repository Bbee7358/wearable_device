let autoConnectTried = false;
let manualDisconnect = false;
let reconnectInFlight = false;
let lastReconnectAttemptMs = 0;
let temperatureChart = null;
let pwmChart = null;
let targetUpdateTimer = null;
let targetUpdateInFlight = false;
let latestCurrentTemp = null;
let markerDragActive = false;

const RECONNECT_INTERVAL_MS = 6000;
const TARGET_UPDATE_DELAY_MS = 450;
const GAUGE_MIN_TEMP = 10;
const GAUGE_MAX_TEMP = 37;
const GAUGE_GEOMETRY = {
  cx: 160,
  cy: 186,
  radius: 108,
  strokeWidth: 56,
  markerLength: 30,
  markerWidth: 34,
  markerGap: 5,
  labelSafeTop: 58,
  labelSafeBottom: 212,
  currentLabelRadiusOffset: 0
};

function setText(id, text) {
  const element = document.getElementById(id);
  if (element) {
    element.textContent = text;
  }
}

function initializeCharts() {
  if (!window.Chart) {
    const message = document.getElementById("message");
    message.textContent = "Chart.jsを読み込めませんでした。";
    message.style.color = "#F55353";
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

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    cache: "no-store",
    ...options
  });
  const text = await response.text();

  if (!text) {
    throw new Error(`${url} から空の応答が返りました。`);
  }

  return {
    response,
    data: JSON.parse(text)
  };
}

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
  const { response, data } = await fetchJson(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });

  const message = document.getElementById("message");
  message.textContent = data.message || data.error || "";
  message.style.color = response.ok ? "#143F6B" : "#F55353";
  return data;
}

async function loadSerialPorts(tryAutoConnect = false) {
  const select = document.getElementById("port");
  const portInfo = document.getElementById("portInfo");
  const currentSelection = select.value;

  try {
    const { data } = await fetchJson("/api/ports");
    const ports = data.ports || [];

    select.innerHTML = "";

    if (ports.length === 0) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "ポートなし";
      select.appendChild(option);
      portInfo.textContent = data.error || "ArduinoをUSB接続してください。";
      return;
    }

    for (const port of ports) {
      const option = document.createElement("option");
      option.value = port.device;
      option.textContent = `${port.device} - ${port.description}`;
      select.appendChild(option);
    }

    if (currentSelection && ports.some(port => port.device === currentSelection)) {
      select.value = currentSelection;
    }

    portInfo.textContent = `${ports.length}件のポートを検出しました。`;

    if (tryAutoConnect && !autoConnectTried && !manualDisconnect) {
      autoConnectTried = true;
      await attemptAutoReconnect(true);
    }
  } catch (error) {
    portInfo.textContent = "ポート一覧の取得に失敗しました。";
  }
}

async function attemptAutoReconnect(force = false) {
  const now = Date.now();
  if (manualDisconnect || reconnectInFlight) {
    return;
  }
  if (!force && now - lastReconnectAttemptMs < RECONNECT_INTERVAL_MS) {
    return;
  }

  reconnectInFlight = true;
  lastReconnectAttemptMs = now;
  document.getElementById("autoReconnectState").textContent = "再接続試行中";

  try {
    const result = await postJson("/auto_connect", {});
    if (result.port) {
      document.getElementById("port").value = result.port;
    }
  } catch (error) {
    document.getElementById("autoReconnectState").textContent = "自動再接続 ON";
  } finally {
    reconnectInFlight = false;
  }
}

async function connectSerial() {
  manualDisconnect = false;
  await postJson("/connect", formData());
  await updateStatus();
}

async function disconnectSerial() {
  manualDisconnect = true;
  await postJson("/disconnect", {});
  await updateStatus();
}

async function sendSettings() {
  await postJson("/send_settings", formData());
  await updateStatus();
}

async function scheduleStart() {
  await postJson("/schedule_start", formData());
  await updateStatus();
}

async function startNow() {
  await postJson("/start_now", formData());
  await updateStatus();
}

async function stopControl() {
  await postJson("/stop", {});
  await updateStatus();
}

async function setTarget() {
  await postJson("/set_target", formData());
  await updateStatus();
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
  await postJson(url, {});
  await updateStatus();
}

function downloadCsv() {
  window.location.href = "/download_csv";
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
  gauge.style.setProperty("--current-label-x", `${currentLabelPoint.x}px`);
  gauge.style.setProperty("--current-label-y", `${currentLabelPoint.y}px`);
  gauge.style.setProperty("--target-label-x", `${targetLabelPoint.x}px`);
  gauge.style.setProperty("--target-label-y", `${targetLabelPoint.y}px`);
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
  if (!data.connected) {
    return manualDisconnect ? "切断中です。" : "Arduinoを探しています。自動再接続を試みます。";
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

async function updateStatus() {
  let data;
  try {
    const result = await fetchJson("/api/status");
    data = result.data;
  } catch (error) {
    const message = document.getElementById("message");
    message.textContent = `状態取得エラー: ${error.message}`;
    message.style.color = "#F55353";
    return;
  }

  setText("connected", `${data.pc_state}${data.port ? " / " + data.port.split("/").pop() : ""}`);
  latestCurrentTemp = data.current_temp;
  setText("currentTemp", showValue(data.current_temp, " ℃"));
  setText("targetTempStatus", showValue(data.target_temp, " ℃"));
  updateTemperatureGauge(data.current_temp, data.target_temp);
  setText("heartRate", showValue(data.heart_rate, " bpm"));
  setText("pwmValue", showValue(data.pwm_value));
  setText("arduinoState", showValue(data.arduino_state));
  setText("error", showValue(data.error));
  setText("lastReceived", showValue(data.last_received));
  setText("logging", data.logging ? "保存中: 時刻・温度・心拍数" : "時刻・温度・心拍数");
  setText("scheduleState", showValue(data.schedule_state));
  setText("scheduledStart", showValue(data.scheduled_start));
  setText("scheduledEnd", showValue(data.scheduled_end));
  setText("statusCount", showValue(data.status_count));
  setText("parseErrorCount", showValue(data.parse_error_count));
  setText("waitingForStatus", data.waiting_for_status ? "待機中" : "なし");
  setText("stateSummary", describeState(data));
  setText("autoReconnectState", manualDisconnect ? "自動再接続 停止" : "自動再接続 ON");

  const message = document.getElementById("message");
  if (data.warning) {
    message.textContent = data.warning;
    message.style.color = "#F55353";
  } else if (data.message) {
    message.textContent = data.message;
    message.style.color = "#143F6B";
  }

  const select = document.getElementById("port");
  if (data.port && select.value !== data.port) {
    const exists = Array.from(select.options).some(option => option.value === data.port);
    if (exists) {
      select.value = data.port;
    }
  }

  const warning = document.getElementById("warning");
  if (data.warning) {
    warning.style.display = "block";
    warning.textContent = data.warning;
  } else {
    warning.style.display = "none";
    warning.textContent = "";
  }

  if (!data.connected && !manualDisconnect) {
    await attemptAutoReconnect(false);
  }
}

async function updateDebug() {
}

async function updateHistory() {
  if (!temperatureChart || !pwmChart) {
    return;
  }

  let data;
  try {
    const result = await fetchJson("/api/history");
    data = result.data;
  } catch (error) {
    return;
  }

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

initializeCharts();
setupGaugeDrag();
loadSerialPorts(true);
refresh();
document.getElementById("targetTemp")?.addEventListener("input", event => {
  updateTemperatureGauge(latestCurrentTemp, event.target.value);
  scheduleTargetUpdate();
});
document.getElementById("targetTemp")?.addEventListener("change", scheduleTargetUpdate);
setInterval(refresh, 1000);
