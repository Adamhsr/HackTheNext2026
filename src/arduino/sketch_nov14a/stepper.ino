#include <Stepper.h>

const int stepsPerRevolution = 2048;

Stepper myStepper(stepsPerRevolution, 8, 10, 9, 11);

void stepperSetup() {
  myStepper.setSpeed(15);
}

void stepperRotate(float revelotions) {
  myStepper.step((int) (revelotions * stepsPerRevolution));
}