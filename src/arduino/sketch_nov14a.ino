
void setup() {
  stepperSetup();
  udpSetup();
}

void loop() {
  int motor;
  float revolutions;

  if (udpLoop(motor, revolutions)) {
    if (motor == 0) {
      stepperRotate(revolutions);
    }
  }
}