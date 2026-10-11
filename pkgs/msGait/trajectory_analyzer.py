class TrajectoryAnalyzer:
    """Analizador de trayectorias. NO IMPLEMENTADO.

    Interfaz reservada: todavía no existe ningún cálculo de trayectorias. Los
    métodos lanzan ``NotImplementedError`` en lugar de devolver la entrada.
    """

    def __init__(self, data_manager):
        """Guarda el gestor de datos que usará la futura implementación.

        Args:
            data_manager: Instancia de ``DataManager``.
        """
        self.data_manager = data_manager

    def analyze_trajectory(self, movement_data):
        """Determina la trayectoria de cada CodeID. No implementado.

        Args:
            movement_data (dict): Movimientos por CodeID.

        Raises:
            NotImplementedError: Siempre.
        """
        raise NotImplementedError("TrajectoryAnalyzer.analyze_trajectory no está implementado.")

    def calculate_trajectory(self, movements):
        """Calcula una trayectoria. No implementado.

        Args:
            movements: Movimientos de un CodeID.

        Raises:
            NotImplementedError: Siempre.
        """
        raise NotImplementedError("TrajectoryAnalyzer.calculate_trajectory no está implementado.")
