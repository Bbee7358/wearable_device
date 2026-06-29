#include <Arduino.h>

/*
  Cooling-only Peltier temperature controller

  PC -> Arduino commands:
    SET,targetTemp,minTemp,maxTemp
    START
    STOP
    SET_TARGET,targetTemp
    VIB_ON
    VIB_OFF
    RESET

  Arduino -> PC messages:
    READY
    STATUS,currentTemp,targetTemp,heartRate,pwmValue,state,error
    ERROR,TEMP_OVER
    ERROR,TEMP_UNDER
*/

const int TEMP_SENSOR_PIN = A0;
const int HEART_SENSOR_PIN = A1;
const int PELTIER_PWM_PIN = 5;
const int VIBRATION_PIN = 6;

const unsigned long SERIAL_BAUD_RATE = 115200;
const unsigned long CONTROL_INTERVAL_MS = 500;
const unsigned long STATUS_INTERVAL_MS = 5000;

// Cooling starts only when temperature is clearly above target.
const float COOLING_START_BAND_C = 0.2;

// Initial safety values. PC can overwrite them with SET.
float targetTemp = 25.0;
float minTemp = 10.0;
float maxTemp = 37.0;

// Simple PID gains. Tune these after real sensor and Peltier behavior are known.
float Kp = 35.0;
float Ki = 0.4;
float Kd = 20.0;

float integral = 0.0;
float previousError = 0.0;

int pwmValue = 0;
bool vibrationOn = false;
bool settingsReceived = false;
bool coolingActive = false;

unsigned long lastControlMs = 0;
unsigned long lastStatusMs = 0;

String serialLine = "";

enum ControllerState {
  WAIT,
  RUNNING,
  STOPPED,
  ERROR_STATE
};

enum ErrorCode {
  ERROR_NONE,
  TEMP_UNDER,
  TEMP_OVER
};

ControllerState state = WAIT;
ErrorCode errorCode = ERROR_NONE;

const char *stateToString(ControllerState s) {
  switch (s) {
    case WAIT:
      return "WAIT";
    case RUNNING:
      return "RUNNING";
    case STOPPED:
      return "STOPPED";
    case ERROR_STATE:
      return "ERROR";
  }
  return "UNKNOWN";
}

const char *errorToString(ErrorCode e) {
  switch (e) {
    case ERROR_NONE:
      return "NONE";
    case TEMP_UNDER:
      return "TEMP_UNDER";
    case TEMP_OVER:
      return "TEMP_OVER";
  }
  return "UNKNOWN";
}

float mapFloat(float x, float inMin, float inMax, float outMin, float outMax) {
  return (x - inMin) * (outMax - outMin) / (inMax - inMin) + outMin;
}

float clampFloat(float value, float low, float high) {
  if (value < low) {
    return low;
  }
  if (value > high) {
    return high;
  }
  return value;
}

int clampInt(int value, int low, int high) {
  if (value < low) {
    return low;
  }
  if (value > high) {
    return high;
  }
  return value;
}

// Temporary temperature conversion.
// Replace only this function after the real temperature sensor is chosen.
float readTemperature() {
  int raw = analogRead(TEMP_SENSOR_PIN);
  float tempC = mapFloat((float)raw, 0.0, 1023.0, 10.0, 37.0);
  return tempC;
}

// Temporary heart-rate conversion.
// Replace only this function after the real heart-rate sensor is chosen.
int readHeartRate() {
  int raw = analogRead(HEART_SENSOR_PIN);
  int bpm = (int)mapFloat((float)raw, 0.0, 1023.0, 40.0, 180.0);
  return clampInt(bpm, 40, 180);
}

void setPeltierPwm(int value) {
  pwmValue = clampInt(value, 0, 255);
  analogWrite(PELTIER_PWM_PIN, pwmValue);
}

void setVibration(bool on) {
  vibrationOn = on;
  digitalWrite(VIBRATION_PIN, on ? HIGH : LOW);
}

void resetPid() {
  integral = 0.0;
  previousError = 0.0;
  coolingActive = false;
}

void stopOutputs() {
  setPeltierPwm(0);
  setVibration(false);
  resetPid();
}

void sendError(ErrorCode code) {
  Serial.print("ERROR,");
  Serial.println(errorToString(code));
}

void enterError(ErrorCode code) {
  stopOutputs();
  state = ERROR_STATE;
  errorCode = code;
  sendError(code);
}

void sendStatus() {
  float currentTemp = readTemperature();
  int heartRate = readHeartRate();

  Serial.print("STATUS,");
  Serial.print(currentTemp, 1);
  Serial.print(",");
  Serial.print(targetTemp, 1);
  Serial.print(",");
  Serial.print(heartRate);
  Serial.print(",");
  Serial.print(pwmValue);
  Serial.print(",");
  Serial.print(stateToString(state));
  Serial.print(",");
  Serial.println(errorToString(errorCode));
}

void checkSafety() {
  if (state == ERROR_STATE) {
    return;
  }

  float currentTemp = readTemperature();

  if (currentTemp < minTemp) {
    enterError(TEMP_UNDER);
  } else if (currentTemp > maxTemp) {
    enterError(TEMP_OVER);
  }
}

void updatePidControl() {
  if (state != RUNNING) {
    return;
  }

  unsigned long now = millis();
  if (now - lastControlMs < CONTROL_INTERVAL_MS) {
    return;
  }

  float dt = (now - lastControlMs) / 1000.0;
  lastControlMs = now;

  float currentTemp = readTemperature();
  float coolingError = currentTemp - targetTemp;

  // Cooling-only logic:
  // If temperature is already at or below target, never heat. Stop cooling.
  if (currentTemp <= targetTemp) {
    setPeltierPwm(0);
    resetPid();
    return;
  }

  // Deadband prevents tiny ON/OFF switching near the target temperature.
  if (!coolingActive && currentTemp <= targetTemp + COOLING_START_BAND_C) {
    setPeltierPwm(0);
    resetPid();
    return;
  }

  coolingActive = true;

  integral += coolingError * dt;
  integral = clampFloat(integral, 0.0, 255.0 / max(Ki, 0.001));

  float derivative = 0.0;
  if (dt > 0.0) {
    derivative = (coolingError - previousError) / dt;
  }
  previousError = coolingError;

  float output = Kp * coolingError + Ki * integral + Kd * derivative;
  setPeltierPwm((int)clampFloat(output, 0.0, 255.0));
}

String getCsvField(String text, int index) {
  int start = 0;

  for (int i = 0; i < index; i++) {
    int comma = text.indexOf(',', start);
    if (comma < 0) {
      return "";
    }
    start = comma + 1;
  }

  int end = text.indexOf(',', start);
  if (end < 0) {
    end = text.length();
  }

  String field = text.substring(start, end);
  field.trim();
  return field;
}

void handleCommand(String command) {
  command.trim();
  if (command.length() == 0) {
    return;
  }

  String name = getCsvField(command, 0);

  if (name == "SET") {
    targetTemp = getCsvField(command, 1).toFloat();
    minTemp = getCsvField(command, 2).toFloat();
    maxTemp = getCsvField(command, 3).toFloat();
    settingsReceived = true;
    errorCode = ERROR_NONE;

    if (state != RUNNING && state != ERROR_STATE) {
      state = WAIT;
    }

    Serial.println("OK,SET");
  } else if (name == "START") {
    if (state != ERROR_STATE && settingsReceived) {
      errorCode = ERROR_NONE;
      resetPid();
      lastControlMs = millis();
      state = RUNNING;
      Serial.println("OK,START");
    } else if (!settingsReceived) {
      Serial.println("ERROR,NO_SETTINGS");
    }
  } else if (name == "STOP") {
    if (state != ERROR_STATE) {
      stopOutputs();
      state = STOPPED;
      errorCode = ERROR_NONE;
      Serial.println("OK,STOP");
    }
  } else if (name == "SET_TARGET") {
    targetTemp = getCsvField(command, 1).toFloat();
    resetPid();
    Serial.println("OK,SET_TARGET");
  } else if (name == "VIB_ON") {
    if (state != ERROR_STATE) {
      setVibration(true);
      Serial.println("OK,VIB_ON");
    }
  } else if (name == "VIB_OFF") {
    setVibration(false);
    Serial.println("OK,VIB_OFF");
  } else if (name == "RESET") {
    stopOutputs();
    state = WAIT;
    errorCode = ERROR_NONE;
    Serial.println("OK,RESET");
  } else {
    Serial.print("ERROR,UNKNOWN_COMMAND,");
    Serial.println(command);
  }
}

void readSerialCommands() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();

    if (c == '\n' || c == '\r') {
      if (serialLine.length() > 0) {
        handleCommand(serialLine);
        serialLine = "";
      }
    } else {
      serialLine += c;

      // Prevent unlimited String growth if invalid data arrives.
      if (serialLine.length() > 80) {
        serialLine = "";
        Serial.println("ERROR,COMMAND_TOO_LONG");
      }
    }
  }
}

void setup() {
  pinMode(PELTIER_PWM_PIN, OUTPUT);
  pinMode(VIBRATION_PIN, OUTPUT);
  pinMode(TEMP_SENSOR_PIN, INPUT);
  pinMode(HEART_SENSOR_PIN, INPUT);

  stopOutputs();

  Serial.begin(SERIAL_BAUD_RATE);
  Serial.println("READY");
}

void loop() {
  readSerialCommands();
  checkSafety();
  updatePidControl();

  unsigned long now = millis();
  if (now - lastStatusMs >= STATUS_INTERVAL_MS) {
    lastStatusMs = now;
    sendStatus();
  }
}
