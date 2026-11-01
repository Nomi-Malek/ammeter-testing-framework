from Ammeters.base_ammeter import AmmeterEmulatorBase, logger
from src.utils.Utils import generate_random_float


class CircutorAmmeter(AmmeterEmulatorBase):
    @property
    def get_current_command(self) -> bytes:
        # Define the command to get the current from CIRCUTOR
        return b'MEASURE_CIRCUTOR -get_measurement -current'

    def measure_current(self) -> float:
        num_samples = 10
        # Time step (0.001s - 0.01s) and voltage values (0.1V - 1V)
        time_step = generate_random_float(0.001, 0.01)
        voltages = [generate_random_float(0.1, 1.0) for _ in range(num_samples)]

        current = sum(v * time_step for v in voltages)
        logger.debug(f"CIRCUTOR Ammeter - Voltages: {voltages}, Time Step: {time_step}s, Current: {current}A")
        return current
