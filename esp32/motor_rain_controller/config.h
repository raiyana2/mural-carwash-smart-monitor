// config.h: pin numbers and limits. Change these to match your wiring.
#pragma once

// ---------------- Motor ----------------
// This pin goes to the INPUT/SIGNAL pin of a motor driver, MOSFET module or
// relay module, never straight to the motor (a GPIO pin can only supply
// about 20-40 mA at 3.3 V).
#define MOTOR_PIN 25

// true  = the module switches ON when the pin is HIGH (most MOSFET/driver boards)
// false = the module switches ON when the pin is LOW  (many relay modules are "active LOW")
#define MOTOR_ACTIVE_HIGH true

// Longest run the firmware will accept, in seconds (safety limit).
#define MAX_RUN_SECONDS 300

// ---------------- Rain sensor ----------------
// For the common rain sensor modules (FC-37 / YL-83 / MH-RD) with a small
// LM393 comparator board that has AO (analog) and DO (digital) pins.
// Power the module from 3.3 V, NOT 5 V, so AO never goes above 3.3 V.
#define RAIN_ANALOG_PIN  34   // AO -> must be an ADC1 pin (32-39)
#define RAIN_DIGITAL_PIN 35   // DO -> LOW when wet on most boards (threshold set by the board's knob)
#define RAIN_SAMPLES     10   // readings averaged per request (smooths out noise)

// ---------------- Serial link to the Raspberry Pi ----------------
#define SERIAL_BAUD 115200
