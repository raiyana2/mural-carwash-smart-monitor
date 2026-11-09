// config.h: pin numbers and limits for motor_rain_controller.ino. Change these to match your wiring.
#pragma once

// ---------------- Servo (positional, e.g. MS18 / SG90 / MG996R) ----------------
// Signal wire (orange / yellow / white) goes to this pin.
// Power the servo from its OWN 5 V supply, and connect that supply's GND to the ESP32's GND.
#define SERVO_PIN 25

#define SERVO_REST_ANGLE   0     // where the servo goes at power-up
                                 // (a timed hold returns to whatever angle it started from)
#define SERVO_ACTIVE_ANGLE 180   // where motor_on moves it, unless the Pi sends an angle

// Pulse widths in microseconds for 0° and 180°. 544-2400 is the ESP32Servo
// library default. If the servo buzzes or strains at the ends, narrow the
// range (e.g. 1000-2000).
#define SERVO_MIN_US 544
#define SERVO_MAX_US 2400

// Longest timed hold (motor_on) the firmware will accept, in seconds (safety limit).
#define MAX_RUN_SECONDS 300

// ---------------- Rain sensor ----------------
// For the common rain sensor modules (FC-37 / YL-83 / MH-RD) with a small
// LM393 comparator board that has AO (analog) and DO (digital) pins.
// Power the module from 3.3 V, NOT 5 V, so AO never goes above 3.3 V.
#define RAIN_ANALOG_PIN  34   // AO -> must be an ADC1 pin (32-39)
#define RAIN_DIGITAL_PIN 39   // DO -> LOW when wet on most boards (threshold set by the board's knob)
#define RAIN_SAMPLES     10   // readings averaged per request (smooths out noise)

// ---------------- Serial link to the Raspberry Pi ----------------
#define SERIAL_BAUD 115200
