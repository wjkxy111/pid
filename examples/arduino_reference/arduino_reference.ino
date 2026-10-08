/*
 * PID Lab JSON-lines protocol reference for Arduino-compatible boards.
 * Replace readPlantOutput() and writeActuator() with hardware-specific code.
 * Keep one complete JSON object per line. Use a hardware timer in production.
 */

float kp = 0.0f, ki = 0.0f, kd = 0.0f, filterN = 0.0f;
float sampleTime = 0.02f, amplitude = 1.0f, durationSeconds = 8.0f;
unsigned long experimentStartMs = 0, lastSampleUs = 0;
bool experimentRunning = false;
bool validationRunning = false, validationAwaitingDecision = false;
String signalName = "step";
String experimentRequestId = "", validationRequestId = "";
float validationSetpoint = 0.0f, validationActuatorLimit = 1.0f;
float validationEmergencyOvershoot = 50.0f;
float validationIntegral = 0.0f, validationDerivative = 0.0f, validationPreviousError = 0.0f;
float rollbackKp = 0.0f, rollbackKi = 0.0f, rollbackKd = 0.0f, rollbackN = 0.0f;
unsigned long validationDecisionDeadlineMs = 0;
const float DEVICE_ACTUATOR_LIMIT = 100.0f;  // TODO: replace with a justified hardware limit
const float DEVICE_SETPOINT_LIMIT = 10000.0f; // TODO: replace with a justified sensor-unit limit
const float DEVICE_EXCITATION_LIMIT = 100.0f; // TODO: replace with a justified open-loop limit

float readPlantOutput() {
  return 0.0f;  // TODO: sensor/encoder value
}

void writeActuator(float value) {
  (void)value;  // TODO: clamp and write PWM/DAC/current command
}

float numberField(const String &line, const char *name, float fallback) {
  String key = String("\"") + name + "\":";
  int start = line.indexOf(key);
  if (start < 0) return fallback;
  start += key.length();
  return line.substring(start).toFloat();
}

String stringField(const String &line, const char *name, const String &fallback) {
  String key = String("\"") + name + "\":\"";
  int start = line.indexOf(key);
  if (start < 0) return fallback;
  start += key.length();
  int end = line.indexOf('"', start);
  return end < 0 ? fallback : line.substring(start, end);
}

String safeRequestId(const String &value) {
  if (value.length() == 0 || value.length() > 64) return "";
  for (unsigned int i = 0; i < value.length(); ++i) {
    char c = value.charAt(i);
    bool alphaNumeric = (c >= '0' && c <= '9')
      || (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z');
    if (!alphaNumeric && c != '-' && c != '_' && c != '.') return "";
  }
  return value;
}

void printRequestId(const String &requestId) {
  if (requestId.length() == 0) return;
  Serial.print(F(",\"request_id\":\""));
  Serial.print(requestId);
  Serial.print('"');
}

void acknowledge(const char *command, bool ok, const String &requestId = "") {
  Serial.print(F("{\"type\":\"ack\",\"command\":\""));
  Serial.print(command);
  Serial.print(F("\",\"ok\":"));
  Serial.print(ok ? F("true") : F("false"));
  printRequestId(requestId);
  Serial.println('}');
}

void reportPidState(const String &requestId = "") {
  Serial.print(F("{\"type\":\"pid_state\",\"kp\":")); Serial.print(kp, 8);
  Serial.print(F(",\"ki\":")); Serial.print(ki, 8);
  Serial.print(F(",\"kd\":")); Serial.print(kd, 8);
  Serial.print(F(",\"n\":")); Serial.print(filterN, 8);
  printRequestId(requestId);
  Serial.println('}');
}

void reportCapabilities(const String &requestId) {
  Serial.print(F("{\"type\":\"capabilities\",\"device_id\":\"pidlab-arduino-reference\","));
  Serial.print(F("\"device_name\":\"PID Lab Arduino Reference\",\"firmware_version\":\"1.1.0\","));
  Serial.print(F("\"protocol_version\":2,\"supported_commands\":[\"hello\",\"start\",\"stop\","));
  Serial.print(F("\"get_pid\",\"set_pid\",\"validate_pid\",\"accept_pid\",\"abort_validation\"],"));
  Serial.print(F("\"units\":{\"input\":\"device-u\",\"output\":\"device-y\",\"setpoint\":\"device-y\"},"));
  Serial.print(F("\"limits\":{\"excitation_abs_max\":100,\"actuator_abs_max\":100,"));
  Serial.print(F("\"setpoint_abs_max\":10000,\"duration_max\":120,\"sample_time_min\":0.001,"));
  Serial.print(F("\"sample_time_max\":1,\"kp_min\":-1000,\"kp_max\":1000,"));
  Serial.print(F("\"ki_min\":-10000,\"ki_max\":10000,\"kd_min\":-1000,"));
  Serial.print(F("\"kd_max\":1000,\"n_min\":0,\"n_max\":10000}"));
  printRequestId(requestId);
  Serial.println('}');
}

bool validPid(float p, float i, float d, float n) {
  return isfinite(p) && isfinite(i) && isfinite(d) && isfinite(n)
    && fabs(p) <= 1000.0f && fabs(i) <= 10000.0f
    && fabs(d) <= 1000.0f && n >= 0.0f && n <= 10000.0f;
}

void restoreRollbackPid() {
  if (validPid(rollbackKp, rollbackKi, rollbackKd, rollbackN)) {
    kp = rollbackKp;
    ki = rollbackKi;
    kd = rollbackKd;
    filterN = rollbackN;
  }
  validationRunning = false;
  validationAwaitingDecision = false;
  writeActuator(0.0f);
}

void abortValidation() {
  String requestId = validationRequestId;
  restoreRollbackPid();
  acknowledge("validation_abort", false, requestId);
  reportPidState(requestId);
  validationRequestId = "";
}

void handleCommand(const String &line) {
  String type = stringField(line, "type", "");
  String requestId = safeRequestId(stringField(line, "request_id", ""));
  if (type == "hello") {
    reportCapabilities(requestId);
  } else if (type == "start") {
    signalName = stringField(line, "signal", "step");
    amplitude = numberField(line, "amplitude", 1.0f);
    durationSeconds = numberField(line, "duration", 8.0f);
    sampleTime = numberField(line, "sample_time", 0.02f);
    experimentStartMs = millis();
    lastSampleUs = micros();
    validationRunning = false;
    experimentRunning = isfinite(amplitude) && fabs(amplitude) <= DEVICE_EXCITATION_LIMIT
      && isfinite(sampleTime) && sampleTime >= 0.001f && sampleTime <= 1.0f
      && isfinite(durationSeconds) && durationSeconds > 0.0f && durationSeconds <= 120.0f;
    experimentRequestId = experimentRunning ? requestId : "";
    acknowledge("start", experimentRunning, requestId);
  } else if (type == "validate_pid") {
    float requestedSetpoint = numberField(line, "setpoint", 0.0f);
    float requestedDuration = numberField(line, "duration", 8.0f);
    float requestedSampleTime = numberField(line, "sample_time", 0.02f);
    float requestedLimit = fabs(numberField(line, "actuator_limit", 0.0f));
    float requestedEmergency = numberField(line, "emergency_overshoot_percent", 50.0f);
    float nextRollbackKp = numberField(line, "rollback_kp", kp);
    float nextRollbackKi = numberField(line, "rollback_ki", ki);
    float nextRollbackKd = numberField(line, "rollback_kd", kd);
    float nextRollbackN = numberField(line, "rollback_n", filterN);
    bool valid = isfinite(requestedSetpoint) && fabs(requestedSetpoint) > 0.0f
      && fabs(requestedSetpoint) <= DEVICE_SETPOINT_LIMIT
      && isfinite(requestedDuration) && requestedDuration >= 3.0f && requestedDuration <= 120.0f
      && isfinite(requestedSampleTime) && requestedSampleTime >= 0.001f && requestedSampleTime <= 1.0f
      && isfinite(requestedLimit) && requestedLimit > 0.0f && requestedLimit <= DEVICE_ACTUATOR_LIMIT
      && validPid(nextRollbackKp, nextRollbackKi, nextRollbackKd, nextRollbackN);
    if (valid) {
      validationSetpoint = requestedSetpoint;
      durationSeconds = requestedDuration;
      sampleTime = requestedSampleTime;
      validationActuatorLimit = min(requestedLimit, DEVICE_ACTUATOR_LIMIT);
      validationEmergencyOvershoot = constrain(requestedEmergency, 20.0f, 200.0f);
      rollbackKp = nextRollbackKp;
      rollbackKi = nextRollbackKi;
      rollbackKd = nextRollbackKd;
      rollbackN = nextRollbackN;
      validationIntegral = 0.0f;
      validationDerivative = 0.0f;
      validationPreviousError = 0.0f;
      experimentStartMs = millis();
      lastSampleUs = micros();
      experimentRunning = false;
      validationAwaitingDecision = false;
      validationRunning = true;
      validationRequestId = requestId;
    }
    acknowledge("validate_pid", valid, requestId);
  } else if (type == "stop") {
    experimentRunning = false;
    validationRunning = false;
    writeActuator(0.0f);
    acknowledge("stop", true, requestId);
  } else if (type == "abort_validation") {
    if (validationRunning || validationAwaitingDecision) {
      acknowledge("abort_validation", true, requestId);
      abortValidation();
    } else acknowledge("abort_validation", false, requestId);
  } else if (type == "accept_pid") {
    validationAwaitingDecision = false;
    acknowledge("accept_pid", true, requestId);
    validationRequestId = "";
  } else if (type == "get_pid") {
    reportPidState(requestId);
  } else if (type == "set_pid") {
    // Parse into temporary values and only commit the whole set after every
    // field passes the device-specific whitelist.  Replace these example
    // limits with limits justified for your motor and sample period.
    float nextKp = numberField(line, "kp", kp);
    float nextKi = numberField(line, "ki", ki);
    float nextKd = numberField(line, "kd", kd);
    float nextN = numberField(line, "n", filterN);
    bool valid = validPid(nextKp, nextKi, nextKd, nextN);
    if (valid) {
      kp = nextKp;
      ki = nextKi;
      kd = nextKd;
      filterN = nextN;
      validationAwaitingDecision = false;
    }
    acknowledge("set_pid", valid, requestId);
    if (valid) reportPidState(requestId);
  }
}

float excitationAt(float t) {
  if (signalName == "step") return t >= min(0.5f, durationSeconds * 0.1f) ? amplitude : 0.0f;
  if (signalName == "chirp") {
    const float f0 = 0.1f, f1 = 4.0f;
    float rate = (f1 - f0) / durationSeconds;
    return amplitude * sinf(2.0f * PI * (f0 * t + 0.5f * rate * t * t));
  }
  // Deterministic two-level approximation for PRBS.
  unsigned long slot = (unsigned long)(t / max(0.08f, durationSeconds / 80.0f));
  return ((slot * 1103515245UL + 12345UL) & 0x80000000UL) ? amplitude : -amplitude;
}

void setup() {
  Serial.begin(115200);
  Serial.setTimeout(20);
  Serial.println(F("{\"type\":\"status\",\"message\":\"device ready\"}"));
}

void loop() {
  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    line.trim();
    if (line.length() > 0 && line.length() < 4096) handleCommand(line);
  }

  if (validationAwaitingDecision
      && (long)(millis() - validationDecisionDeadlineMs) >= 0) {
    abortValidation();
  }

  if (!experimentRunning && !validationRunning) return;
  unsigned long intervalUs = (unsigned long)(sampleTime * 1000000.0f);
  if ((unsigned long)(micros() - lastSampleUs) < intervalUs) return;
  lastSampleUs += intervalUs;

  float t = (millis() - experimentStartMs) / 1000.0f;
  if (t >= durationSeconds) {
    writeActuator(0.0f);
    if (validationRunning) {
      validationRunning = false;
      validationAwaitingDecision = true;
      validationDecisionDeadlineMs = millis() + 3000UL;
      acknowledge("validation_complete", true, validationRequestId);
    } else {
      experimentRunning = false;
      acknowledge("experiment_complete", true, experimentRequestId);
      experimentRequestId = "";
    }
    return;
  }

  if (validationRunning) {
    float y = readPlantOutput();
    if (!isfinite(y)) {
      abortValidation();
      return;
    }
    float reference = t >= min(0.5f, durationSeconds * 0.1f) ? validationSetpoint : 0.0f;
    float error = reference - y;
    float rawDerivative = (error - validationPreviousError) / sampleTime;
    float alpha = filterN > 0.0f ? min(1.0f, filterN * sampleTime / (1.0f + filterN * sampleTime)) : 1.0f;
    validationDerivative += alpha * (rawDerivative - validationDerivative);
    float proposedIntegral = validationIntegral + error * sampleTime;
    float unsaturated = kp * error + ki * proposedIntegral + kd * validationDerivative;
    float control = constrain(unsaturated, -validationActuatorLimit, validationActuatorLimit);
    if (fabs(unsaturated) <= validationActuatorLimit || error * unsaturated < 0.0f) {
      validationIntegral = proposedIntegral;
    }
    validationPreviousError = error;
    if (!isfinite(control)) {
      abortValidation();
      return;
    }
    if (fabs(reference) > 0.1f * fabs(validationSetpoint)) {
      float direction = validationSetpoint > 0.0f ? 1.0f : -1.0f;
      if (direction * (y - validationSetpoint)
          > fabs(validationSetpoint) * validationEmergencyOvershoot / 100.0f) {
        abortValidation();
        return;
      }
    }
    writeActuator(control);
    Serial.print(F("{\"type\":\"sample\",\"t\":")); Serial.print(t, 6);
    Serial.print(F(",\"u\":")); Serial.print(control, 6);
    Serial.print(F(",\"y\":")); Serial.print(y, 6);
    Serial.print(F(",\"setpoint\":")); Serial.print(reference, 6);
    printRequestId(validationRequestId);
    Serial.println('}');
    return;
  }

  float u = excitationAt(t);
  writeActuator(u);
  float y = readPlantOutput();
  Serial.print(F("{\"type\":\"sample\",\"t\":")); Serial.print(t, 6);
  Serial.print(F(",\"u\":")); Serial.print(u, 6);
  Serial.print(F(",\"y\":")); Serial.print(y, 6);
  Serial.print(F(",\"setpoint\":")); Serial.print(u, 6);
  printRequestId(experimentRequestId);
  Serial.println('}');
}
