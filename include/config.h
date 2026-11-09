#pragma once

// ---------------- Servo Motor ----------------
#define MOTOR_PIN 27

// ---------------- Rain Sensor ----------------
#define RAIN_ANALOG_PIN  34   
#define RAIN_DIGITAL_PIN 39   
#define RAIN_SAMPLES     10   

// ---------------- Ultrasonic Sensor (HC-SR04) ----------------
#define ULTRASONIC_TRIG_PIN 15   
#define ULTRASONIC_ECHO_PIN 33   

// ---------------- Push Button ----------------
#define BUTTON_PIN       32   // Physical push button on GPIO 32

// ---------------- I2C LCD 1602 Shield ----------------
#define LCD_SDA          22   // Feather V2 default SDA
#define LCD_SCL          20   // Feather V2 default SCL
#define LCD_ADDRESS      0x38 // Updated I2C address discovered by scanner
#define LCD_COLS         16
#define LCD_ROWS         2

// ---------------- Serial Link to the Raspberry Pi ----------------
#define SERIAL_BAUD 115200