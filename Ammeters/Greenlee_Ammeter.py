from Ammeters.base_ammeter import AmmeterEmulatorBase, logger
from src.utils.Utils import generate_random_float


class GreenleeAmmeter(AmmeterEmulatorBase):
    @property
    def get_current_command(self) -> bytes:
        # Define the command to get the current from Greenlee
        return b'MEASURE_GREENLEE -get_measurement'

    def measure_current(self) -> float:
        # Random voltage (1V - 10V) and resistance (0.1Ω - 100Ω)
        voltage = generate_random_float(1.0, 10.0)
        resistance = generate_random_float(0.1, 100.0)
        current = voltage / resistance
        logger.debug(f"Greenlee Ammeter - Voltage: {voltage}V, Resistance: {resistance}Ω, Current: {current}A")
        return current
