#include <Wire.h>
#include <Adafruit_ADS1X15.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#define ONE_WIRE_BUS 2
#define MOSFET_MAGNETIC_PIN 9
#define MOSFET_ELECTROSTATIC_PIN 10
#define VREF 5.0
#define ADC_RESOLUTION 65536.0
#define FILTER_SIZE 15
#define TELEMETRY_INTERVAL 1000
#define SENSOR_READ_INTERVAL 200
#define PWM_RAMP_INTERVAL 10

OneWire oneWire(ONE_WIRE_BUS);
DallasTemperature sensors(&oneWire);
Adafruit_ADS1115 ads;

struct SystemTelemetry {
  float temperatureC;
  float rawTDS;
  float compensatedTDS;
  int targetMagneticPWM;
  int currentMagneticPWM;
  int targetElectrostaticPWM;
  int currentElectrostaticPWM;
  uint32_t systemUptime;
  uint8_t faultCode;
} sysData;

float tdsBuffer[FILTER_SIZE];
uint8_t bufferIndex = 0;
bool bufferFull = false;

uint32_t lastTelemetryTick = 0;
uint32_t lastSensorTick = 0;
uint32_t lastRampTick = 0;

const float calibrationConstant = 0.53;
const float tempCoefficient = 0.02;
const float maxSafeTemperature = 65.0;
const float minSafeTemperature = 2.0;
const float criticalTDSThreshold = 1400.0;

void setup() {
  Serial.begin(115200);
  Wire.setClock(400000);
  
  pinMode(MOSFET_MAGNETIC_PIN, OUTPUT);
  pinMode(MOSFET_ELECTROSTATIC_PIN, OUTPUT);
  analogWrite(MOSFET_MAGNETIC_PIN, 0);
  analogWrite(MOSFET_ELECTROSTATIC_PIN, 0);

  sensors.begin();
  sensors.setResolution(12);

  if (!ads.begin()) {
    while (1) {
      digitalWrite(LED_BUILTIN, HIGH);
      delay(50);
      digitalWrite(LED_BUILTIN, LOW);
      delay(50);
    }
  }
   ads.setGain(GAIN_ONE);
  
  for(int i = 0; i < FILTER_SIZE; i++) {
    tdsBuffer[i] = 0.0;
  }
  
  sysData.currentMagneticPWM = 0;
  sysData.currentElectrostaticPWM = 0;
  sysData.faultCode = 0;
  sysData.temperatureC = 25.0;
}

void sortBuffer(float* buf, int size) {
  for (int i = 0; i < size - 1; i++) {
    for (int j = 0; j < size - i - 1; j++) {
      if (buf[j] > buf[j + 1]) {
        float temp = buf[j];
        buf[j] = buf[j + 1];
        buf[j + 1] = temp;
      }
    }
  }
}

float calculateMedianFilteredTDS(float newVal) {
  tdsBuffer[bufferIndex] = newVal;
  bufferIndex++;
  if (bufferIndex >= FILTER_SIZE) {
    bufferIndex = 0;
    bufferFull = true;
  }
  
  int elements = bufferFull ? FILTER_SIZE : bufferIndex;
  if (elements == 0) return 0.0;
  
  float sortedBuffer[FILTER_SIZE];
  for (int i = 0; i < elements; i++) {
    sortedBuffer[i] = tdsBuffer[i];
  }
  
  sortBuffer(sortedBuffer, elements);
  if (elements % 2 == 0) {
    return (sortedBuffer[elements / 2 - 1] + sortedBuffer[elements / 2]) / 2.0;
  } else {
    return sortedBuffer[elements / 2];
  }
}
void sampleSensorMatrix() {
  sensors.requestTemperatures();
  float temp = sensors.getTempCByIndex(0);
  if (temp != DEVICE_DISCONNECTED_C) {
    sysData.temperatureC = temp;
  }
  int16_t adc0 = ads.readADC_SingleEnded(0);
  float voltage = (adc0 * VREF) / ADC_RESOLUTION;
  float raw = (voltage * calibrationConstant) * 1000.0;
 
  sysData.rawTDS = calculateMedianFilteredTDS(raw);
  sysData.compensatedTDS = sysData.rawTDS / (1.0 + tempCoefficient * (sysData.temperatureC - 25.0));
}
void computeDynamicModulation() {
  if (sysData.temperatureC >= maxSafeTemperature || sysData.temperatureC <= minSafeTemperature) {
    sysData.faultCode = 1;
    sysData.targetMagneticPWM = 0;
    sysData.targetElectrostaticPWM = 0;
    return;
  }
  if (sysData.compensatedTDS >= criticalTDSThreshold) {
    sysData.faultCode = 2;
    sysData.targetMagneticPWM = 255;
    sysData.targetElectrostaticPWM = 0;
    return;
  }
  sysData.faultCode = 0;

  if (sysData.compensatedTDS > 1000.0) {
    sysData.targetMagneticPWM = 255;
    sysData.targetElectrostaticPWM = map(sysData.compensatedTDS, 1000, criticalTDSThreshold, 100, 0);
  } else if (sysData.compensatedTDS > 600.0) {
    sysData.targetMagneticPWM = map(sysData.compensatedTDS, 600, 1000, 150, 255);
    sysData.targetElectrostaticPWM = map(sysData.compensatedTDS, 600, 1000, 200, 100);
  } else if (sysData.compensatedTDS > 250.0) {
    sysData.targetMagneticPWM = map(sysData.compensatedTDS, 250, 600, 50, 150);
    sysData.targetElectrostaticPWM = map(sysData.compensatedTDS, 250, 600, 255, 200);
  } else if (sysData.compensatedTDS > 50.0) {
    sysData.targetMagneticPWM = 0;
    sysData.targetElectrostaticPWM = map(sysData.compensatedTDS, 50, 250, 100, 255);
  } else {
    sysData.targetMagneticPWM = 0;
    sysData.targetElectrostaticPWM = 0;
  }
void executeSoftRamping() {
  if (sysData.currentMagneticPWM < sysData.targetMagneticPWM) {
    sysData.currentMagneticPWM++;
  } else if (sysData.currentMagneticPWM > sysData.targetMagneticPWM) {
    sysData.currentMagneticPWM--;
  }
  if (sysData.currentElectrostaticPWM < sysData.targetElectrostaticPWM) {
    sysData.currentElectrostaticPWM++;
  } else if (sysData.currentElectrostaticPWM > sysData.targetElectrostaticPWM) {
    sysData.currentElectrostaticPWM--;
  }
  analogWrite(MOSFET_MAGNETIC_PIN, sysData.currentMagneticPWM);
  analogWrite(MOSFET_ELECTROSTATIC_PIN, sysData.currentElectrostaticPWM);
}

void transmitSerializedTelemetry() {
  sysData.systemUptime = millis();
  
  Serial.print(F("{\"uptime_ms\":"));
  Serial.print(sysData.systemUptime);
  Serial.print(F(",\"temp_c\":"));
  Serial.print(sysData.temperatureC, 3);
  Serial.print(F(",\"raw_tds\":"));
  Serial.print(sysData.rawTDS, 3);
  Serial.print(F(",\"comp_tds\":"));
  Serial.print(sysData.compensatedTDS, 3);
  Serial.print(F(",\"mag_pwm_tgt\":"));
  Serial.print(sysData.targetMagneticPWM);
  Serial.print(F(",\"mag_pwm_cur\":"));
  Serial.print(sysData.currentMagneticPWM);
  Serial.print(F(",\"elec_pwm_tgt\":"));
  Serial.print(sysData.targetElectrostaticPWM);
  Serial.print(F(",\"elec_pwm_cur\":"));
  Serial.print(sysData.currentElectrostaticPWM);
  Serial.print(F(",\"fault_code\":"));
  Serial.print(sysData.faultCode);
  Serial.println(F("}"));
}

void loop() {
  uint32_t currentTick = millis();

  if (currentTick - lastSensorTick >= SENSOR_READ_INTERVAL) {
    lastSensorTick = currentTick;
    sampleSensorMatrix();
    computeDynamicModulation();
  }

  if (currentTick - lastRampTick >= PWM_RAMP_INTERVAL) {
    lastRampTick = currentTick;
    executeSoftRamping();
  }

  if (currentTick - lastTelemetryTick >= TELEMETRY_INTERVAL) {
    lastTelemetryTick = currentTick;
    transmitSerializedTelemetry();
  }
}
