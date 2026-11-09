// config.h: pin numbers and limits for the firmware in src/main.cpp. Change these to match your wiring.
#pragma once

// ---------------- Servo (positional, e.g. MS18 / SG90 / MG996R) ----------------
// Signal wire (orange / yellow / white) goes to this pin.
// Power the servo from its OWN 5 V supply, and connect that supply's GND to the ESP32's GND.
#define SERVO_PIN 27

#define SERVO_REST_ANGLE   0     // where the servo goes at power-up
                                 // (a timed hold returns to whatever angle it started from)
#define SERVO_ACTIVE_ANGLE 180   // where motor_on moves it, unless the Pi sends an angle

// Pulse widths in microseconds for 0° and 180°. 544-2400 is the ESP32Servo
// library default. If the servo buzzes or strains at the ends, narrow the
// range (e.g. 1000-2000).
#define SERVO_MIN_US 544
#define SERVO_MAX_US 2400

// Longest timed run (motor_on) the firmware will accept, in seconds (safety limit).
// One hour: the longest wash a customer can buy.
#define MAX_RUN_SECONDS 3600

// ---------------- Rain sensor ----------------
// For the common rain sensor modules (FC-37 / YL-83 / MH-RD) with a small
// LM393 comparator board that has AO (analog) and DO (digital) pins.
// Power the module from 3.3 V, NOT 5 V, so AO never goes above 3.3 V.
#define RAIN_ANALOG_PIN  34   // AO -> must be an ADC1 pin (32-39)
#define RAIN_DIGITAL_PIN 39   // DO -> LOW when wet on most boards (threshold set by the board's knob)
#define RAIN_SAMPLES     10   // readings averaged per request (smooths out noise)

// ---------------- Ultrasonic distance sensor (HC-SR04) ----------------
// Measures the distance to the nearest object; the Pi uses it to tell whether a car is in the bay.
// The HC-SR04 runs on 5 V and its ECHO pin puts out 5 V, which is too much for the ESP32:
// put a voltage divider between ECHO and the ESP32 pin (for example 1 kΩ from ECHO to the
// pin and 2 kΩ from the pin to GND), or use a 3.3 V version such as the HC-SR04P.
#define ULTRASONIC_TRIG_PIN   15
#define ULTRASONIC_ECHO_PIN   33
#define ULTRASONIC_TIMEOUT_US 30000   // no echo within this long = nothing in range (about 5 m)

// ---------------- Push button ----------------
// Wire one leg to this pin and the other to GND. The internal pull-up holds the pin HIGH
// until the button is pressed. Each press is sent to the Pi as a "button" event.
#define BUTTON_PIN         32
#define BUTTON_DEBOUNCE_MS 40

// ---------------- I2C LCD 1602 (16 x 2 characters) ----------------
#define LCD_SDA     22      // Feather ESP32 V2 SDA
#define LCD_SCL     20      // Feather ESP32 V2 SCL
#define LCD_ADDRESS 0x38    // found with an I2C scanner; other common addresses are 0x27 and 0x3F
#define LCD_COLS    16
#define LCD_ROWS    2

// ---------------- Serial link to the Raspberry Pi ----------------
#define SERIAL_BAUD 115200
